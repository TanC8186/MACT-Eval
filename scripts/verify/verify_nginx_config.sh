#!/usr/bin/env bash
# 用真实 nginx 二进制验证 teacher-web 的配置（B1，2026-10-28）。
#
#   bash scripts/verify/verify_nginx_config.sh
#
# **这是本机 nginx，不是容器里的 nginx。** 两者的差别必须说清楚：
#   * 能验证：配置语法（`nginx -t`）、模板渲染后的指令是否合法、
#     静态文件的响应头是否真的下发、/api/ 与 /ws/ 的反代块是否被解析。
#   * 不能验证：容器内 `/usr/share/nginx/html` 的路径、Dockerfile 的 COPY、
#     nginx:alpine 的实际版本行为、非 root 运行。
# 后者需要 `docker build`，而 Docker 数据盘在 C 盘、任务书要求阻塞。
#
# 为什么要下载本机 nginx：配置正确与否是安全头能否生效的前提。只做静态文本
# 检查无法发现"指令写在不允许的上下文"、"变量未定义"、"正则捕获非法"这类
# 只有解析器才知道的问题。下载落在 `.cache/tools/`（E 盘），不写 C 盘。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

TOOLS="$ROOT/.cache/tools"
NGINX_HOME="$TOOLS/nginx-1.27.4"

# 解释器：优先用显式传入的，其次项目 venv。后者缺失时拒绝继续——
# 用系统 python 会让"渲染结果"取决于某台机器装了什么。
VERIFY_PY="${VERIFY_PYTHON:-$ROOT/.venv-verify/Scripts/python.exe}"
[[ -x "$VERIFY_PY" ]] || VERIFY_PY="$ROOT/.venv-verify/bin/python"
if [[ ! -x "$VERIFY_PY" ]]; then
  echo "!! 未找到 .venv-verify（bash scripts/verify/setup_verify_env.sh）" >&2
  exit 2
fi
OUT_DIR="$ROOT/docs/reviews/evidence"
STAMP="$(date +%Y%m%dT%H%M)"
WORK="$ROOT/.cache/tmp/nginx-verify"
LOG_JSON="$OUT_DIR/nginx-verify-$STAMP.json"

CACHE="$ROOT/.cache"
export TMPDIR="$CACHE/tmp" TEMP="$CACHE/tmp" TMP="$CACHE/tmp"
mkdir -p "$TMPDIR" "$OUT_DIR"

if [[ ! -x "$NGINX_HOME/nginx.exe" ]]; then
  echo "!! 缺少本机 nginx：$NGINX_HOME/nginx.exe" >&2
  echo "   下载：curl -sSL -o .cache/tools/nginx.zip https://nginx.org/download/nginx-1.27.4.zip" >&2
  echo "         cd .cache/tools && unzip -oq nginx.zip" >&2
  exit 2
fi

echo "==> nginx: $("$NGINX_HOME/nginx.exe" -v 2>&1)"

rm -rf "$WORK"
mkdir -p "$WORK/conf" "$WORK/html/assets" "$WORK/logs" "$WORK/temp"

# ---------------------------------------------------------------------------
# 1. 渲染模板（模拟容器里的 envsubst）
# ---------------------------------------------------------------------------
# 用 Python 而不是 envsubst：Windows 上没有 GNU envsubst，而替换语义很简单
# ——只替换"环境里存在的"变量，这与 nginx 官方的 20-envsubst 脚本一致。
"$VERIFY_PY" - "$ROOT" "$WORK" <<'PYEOF'
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
work = pathlib.Path(sys.argv[2])

template = (root / "frontend/apps/teacher-web/nginx.conf").read_text(encoding="utf-8")
env = {"MACT_CSP_MEDIA_ORIGIN": os.environ.get("MACT_CSP_MEDIA_ORIGIN",
                                               "http://127.0.0.1:19100")}
rendered = template
for key, value in env.items():
    rendered = rendered.replace("${" + key + "}", value)

# 只检查**非注释**行：本配置的注释里引用了 `${VAR}` 作为说明文字
# （"img/media/connect 里的 ${MACT_CSP_MEDIA_ORIGIN}"），把它当成未替换变量
# 是假阳性。envsubst 本身也确实会替换注释里的变量，但注释被替换与否
# 对配置行为没有影响，所以这不是"放松检查"。
code_lines = [ln for ln in rendered.splitlines() if not ln.strip().startswith("#")]
joined = chr(10).join(code_lines)
leftover = [t for t in joined.split("${")[1:]]
if leftover:
    sys.exit(f"渲染后仍有未替换变量：{[t.split('}')[0] for t in leftover]}")

# 容器里这份文件被渲染到 /etc/nginx/conf.d/default.conf。本机验证时把它包进
# 一个最小的 http{server} 外层——模板本身只有 server{} 块。
mime = (work / "conf" / "mime.types")
mime.write_text(
    "types { text/html html; text/css css; application/javascript js;"
    " image/svg+xml svg; image/png png; image/jpeg jpg; }\n", encoding="utf-8")

# 反代目标改成 127.0.0.1:9（discard 端口，必然连接失败）。这是**故意的**：
# 本步骤只验证配置能被解析、静态响应头能下发；上游不可达时 nginx 返回 502，
# 而 502 同样会带 server 级的 add_header（因为我们用了 always）——那正好是
# "错误响应也要有安全头"这条断言的实证。
rendered = rendered.replace("http://backend:8000", "http://127.0.0.1:9")

server_conf = work / "conf" / "server.conf"
server_conf.write_text(rendered, encoding="utf-8")

(work / "conf" / "nginx.conf").write_text(
    f"""worker_processes 1;
error_log {work.as_posix()}/logs/error.log warn;
pid {work.as_posix()}/temp/nginx.pid;
events {{ worker_connections 64; }}
http {{
    include {mime.as_posix()};
    default_type application/octet-stream;
    access_log {work.as_posix()}/logs/access.log;
    client_body_temp_path {work.as_posix()}/temp/client_body;
    proxy_temp_path {work.as_posix()}/temp/proxy;
    fastcgi_temp_path {work.as_posix()}/temp/fastcgi;
    uwsgi_temp_path {work.as_posix()}/temp/uwsgi;
    scgi_temp_path {work.as_posix()}/temp/scgi;
    include {server_conf.as_posix()};
}}
""", encoding="utf-8")

# 静态根目录：nginx.conf 里的 root 是容器的 /usr/share/nginx/html。
# 本机验证时把它换成本地目录，其余不变。
text = server_conf.read_text(encoding="utf-8")
text = text.replace("root /usr/share/nginx/html;",
                    f"root {(work / 'html').as_posix()};")
text = text.replace("access_log /var/log/nginx/access.log safe;",
                    f"access_log {(work / 'logs' / 'access.log').as_posix()} safe;")
# error_log 也是容器路径。不重写的话 nginx -t 会因为
# CreateFile("E:/var/log/nginx/ws-error.log") 失败——那是**harness**的问题，
# 不是配置的问题（容器里 /var/log/nginx 存在）。
text = text.replace("error_log /var/log/nginx/ws-error.log crit;",
                    f"error_log {(work / 'logs' / 'ws-error.log').as_posix()} crit;")
server_conf.write_text(text, encoding="utf-8")

(work / "html" / "index.html").write_text("<!doctype html><title>probe</title>",
                                          encoding="utf-8")
(work / "html" / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
print("rendered ok")
PYEOF

echo "==> 渲染完成：$WORK/conf/server.conf"

# ---------------------------------------------------------------------------
# 2. nginx -t
# ---------------------------------------------------------------------------
set +e
TEST_OUT="$("$NGINX_HOME/nginx.exe" -t -c "$(cygpath -w "$WORK/conf/nginx.conf")" -p "$(cygpath -w "$WORK")" 2>&1)"
TEST_RC=$?
set -e
echo "$TEST_OUT"
echo "==> nginx -t exit=$TEST_RC"

if [[ "$TEST_RC" -ne 0 ]]; then
  echo "!! 配置未通过 nginx -t；不继续抓响应头（那只会得到无意义的连接失败）" >&2
  "$VERIFY_PY" - "$LOG_JSON" "$TEST_OUT" "$TEST_RC" <<'PYEOF'
import json, sys, time
json.dump({"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "nginxTest": {"exit": int(sys.argv[3]), "output": sys.argv[2]},
           "passed": False,
           "note": "配置未通过 nginx -t，未抓取响应头"},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYEOF
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. 起 nginx 抓响应头
# ---------------------------------------------------------------------------
PORT="${NGINX_PROBE_PORT:-18099}"
# 端口固定会与既有服务冲突时换一个（只在本步骤内生效）。
if ! "$VERIFY_PY" -c "
import socket, sys
s = socket.socket(); s.settimeout(0.4)
rc = s.connect_ex(('127.0.0.1', int(sys.argv[1]))); s.close()
sys.exit(0 if rc != 0 else 1)" "$PORT"; then
  PORT=$((PORT + 1))
fi
echo "==> 探测端口 $PORT"
sed -i.bak "s/    listen 80;/    listen $PORT;/" "$WORK/conf/server.conf"

cleanup() {
  "$NGINX_HOME/nginx.exe" -s stop -c "$(cygpath -w "$WORK/conf/nginx.conf")"       -p "$(cygpath -w "$WORK")" >/dev/null 2>&1 || true
  sleep 1
  # 只杀**本步骤**起的那两个进程（master + worker）。用 PID 文件而不是
  # `taskkill /IM nginx.exe`：后者会连带杀掉机器上其它 nginx（如果有）。
  local pidfile="$WORK/temp/nginx.pid"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile" 2>/dev/null | tr -d '
 ')"
    [[ -n "$pid" ]] && taskkill //F //PID "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$pidfile"
}
trap cleanup EXIT

# 必须**脱离**启动：Windows 版 nginx.exe 在前台会一直占住调用它的 shell，
# 直接调 `nginx.exe ...` 会让整个脚本挂住（实测 timeout 300s 后被杀）。
( "$NGINX_HOME/nginx.exe" -c "$(cygpath -w "$WORK/conf/nginx.conf")"     -p "$(cygpath -w "$WORK")" > "$WORK/logs/startup.out" 2>&1 & )
for _ in $(seq 1 40); do
  if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then break; fi
  sleep 0.3
done

"$VERIFY_PY" - "$PORT" "$LOG_JSON" "$WORK" <<'PYEOF'
import json
import os
import pathlib
import subprocess
import sys
import time

port = sys.argv[1]
out_path = sys.argv[2]
work = pathlib.Path(sys.argv[3])

REQUIRED = {
    "content-security-policy": ["default-src 'self'", "frame-ancestors 'none'",
                                "object-src 'none'"],
    "x-content-type-options": ["nosniff"],
    "x-frame-options": ["DENY"],
    "referrer-policy": ["no-referrer"],
    "permissions-policy": ["camera=()", "microphone=()"],
}

# 注意 `/no-such-file.txt` 返回 **200**，不是 404：`location /` 的
# `try_files $uri $uri/ /index.html` 会把未知路径回落到 SPA 入口，这是
# 单页应用的正确行为。真正的 404 只能由 `/assets/` 的 `try_files $uri =404`
# 产生——所以 404 探针必须打 /assets/ 下的不存在文件。
PROBES = [
    ("index-200", "/"),
    ("asset-200", "/assets/app.js"),
    ("spa-fallback-200", "/reports"),
    ("asset-404", "/assets/missing.js"),
    ("api-502", "/api/health"),
    ("ws-502", "/ws/classroom?lessonId=x"),
]


def curl_headers(path: str):
    proc = subprocess.run(
        ["curl", "-s", "-D", "-", "-o", os.devnull, "--max-time", "5",
         f"http://127.0.0.1:{port}{path}"],
        capture_output=True, text=True)
    raw = proc.stdout
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    status = lines[0].split()[1] if lines and " " in lines[0] else "?"
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers


results = []
failures = []
for name, path in PROBES:
    try:
        status, headers = curl_headers(path)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{name}: 请求失败 {type(exc).__name__}")
        continue
    checks = []
    for header, needles in REQUIRED.items():
        value = headers.get(header)
        ok = value is not None and all(n in value for n in needles)
        checks.append({"header": header, "ok": ok,
                       "value": (value[:160] if value else None)})
        if not ok:
            failures.append(f"{name} ({path}) HTTP {status}: {header} "
                            f"={'缺失' if value is None else value[:80]}")
    results.append({"probe": name, "path": path, "status": status,
                    "headers": {k: v for k, v in headers.items()
                                if k.startswith(("content-security", "x-", "referrer",
                                                 "permissions", "cache-control"))},
                    "checks": checks,
                    "passed": all(c["ok"] for c in checks)})

# 访问日志里不得出现查询串（SEC-02）
access = (work / "logs" / "access.log")
log_text = access.read_text(encoding="utf-8", errors="replace") if access.exists() else ""
query_leaks = [l for l in log_text.splitlines()
               if "?lessonId=" in l or "token=" in l or "ticket=" in l]
if query_leaks:
    failures.append(f"access.log 记录了查询串（{len(query_leaks)} 行）："
                    f"{query_leaks[0][:120]}")
ws_logged = [l for l in log_text.splitlines() if "/ws/" in l]
if ws_logged:
    failures.append(f"/ws/ 的请求进了 access.log（{len(ws_logged)} 行）："
                    f"{ws_logged[0][:120]}")

payload = {
    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "scope": "本机 nginx 1.27.4 验证渲染后的 server 块；不含容器/镜像验证",
    "port": port,
    "probes": results,
    "accessLogSample": log_text.splitlines()[-6:],
    "failures": failures,
    "passed": not failures,
}
pathlib.Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

for item in results:
    print(f"  {'PASS' if item['passed'] else 'FAIL'} {item['probe']:<16} "
          f"HTTP {item['status']}")
if failures:
    print(f"\n{len(failures)} 项不满足：", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)
print(f"\n全部通过；结果写入 {out_path}")
PYEOF
