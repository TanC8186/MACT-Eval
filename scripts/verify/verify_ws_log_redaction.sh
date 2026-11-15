#!/usr/bin/env bash
# WebSocket 凭据日志脱敏验证（B2，2026-10-28）。
#
#   bash scripts/verify/verify_ws_log_redaction.sh
#
# 用**本机 nginx** 跑真实的 teacher-web 配置，把 /ws/ 反代到真实后端
# （127.0.0.1:8000），然后制造多种 WS 握手（成功 / 无票据 / 重放 / 旧 token），
# 最后读 nginx 的 access.log 与 error.log，断言里面不出现任何凭据。
#
# 与 `test_security_headers.py` 的分工：那个检查**配置文本**里有 safe 日志格式；
# 这个验证**运行后**的日志文件里确实没有凭据。前者会在"格式改了但 location
# 又加回了默认 access_log"时漏掉，后者不会。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

TOOLS="$ROOT/.cache/tools"
NGINX_HOME="$TOOLS/nginx-1.27.4"
VERIFY_PY="${VERIFY_PYTHON:-$ROOT/.venv-verify/Scripts/python.exe}"
[[ -x "$VERIFY_PY" ]] || VERIFY_PY="$ROOT/.venv-verify/bin/python"
if [[ ! -x "$VERIFY_PY" ]]; then
  echo "!! 未找到 .venv-verify" >&2; exit 2
fi
if [[ ! -x "$NGINX_HOME/nginx.exe" ]]; then
  echo "!! 缺少本机 nginx：$NGINX_HOME/nginx.exe" >&2; exit 2
fi

CACHE="$ROOT/.cache"
export TMPDIR="$CACHE/tmp" TEMP="$CACHE/tmp" TMP="$CACHE/tmp"
WORK="$CACHE/tmp/ws-log-verify"
OUT_JSON="$ROOT/docs/reviews/evidence/ws-log-redaction-$(date +%Y%m%dT%H%M).json"
PORT="${WS_PROBE_PORT:-18098}"
BACKEND="${MACT_BACKEND:-http://127.0.0.1:8000}"

# 后端必须活着：本步骤要产生**真实的** 401/403，而不是 nginx 自己生成的 502。
if ! curl -fsS -o /dev/null --max-time 5 "$BACKEND/api/health"; then
  echo "!! 后端不可达：$BACKEND/api/health（先起后端再跑本步骤）" >&2
  exit 2
fi

rm -rf "$WORK"; mkdir -p "$WORK/conf" "$WORK/html" "$WORK/logs" "$WORK/temp"

"$VERIFY_PY" - "$ROOT" "$WORK" "$PORT" "$BACKEND" <<'PYEOF'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
work = pathlib.Path(sys.argv[2])
port = sys.argv[3]
backend = sys.argv[4]

template = (root / "frontend/apps/teacher-web/nginx.conf").read_text(encoding="utf-8")
text = template.replace("${MACT_CSP_MEDIA_ORIGIN}", "http://127.0.0.1:19100")
text = text.replace("http://backend:8000", backend)
text = text.replace("root /usr/share/nginx/html;", f"root {(work / 'html').as_posix()};")
text = text.replace("access_log /var/log/nginx/access.log safe;",
                    f"access_log {(work / 'logs' / 'access.log').as_posix()} safe;")
text = text.replace("error_log /var/log/nginx/ws-error.log crit;",
                    f"error_log {(work / 'logs' / 'ws-error.log').as_posix()} crit;")
text = text.replace("    listen 80;", f"    listen {port};")
(work / "conf" / "server.conf").write_text(text, encoding="utf-8")

(work / "conf" / "mime.types").write_text("types { text/html html; }\n", encoding="utf-8")
(work / "html" / "index.html").write_text("<!doctype html><title>ws-probe</title>",
                                          encoding="utf-8")

(work / "conf" / "nginx.conf").write_text(
    f"""worker_processes 1;
error_log {work.as_posix()}/logs/error.log info;
pid {work.as_posix()}/temp/nginx.pid;
events {{ worker_connections 64; }}
http {{
    include {(work / 'conf' / 'mime.types').as_posix()};
    default_type application/octet-stream;
    client_body_temp_path {work.as_posix()}/temp/client_body;
    proxy_temp_path {work.as_posix()}/temp/proxy;
    fastcgi_temp_path {work.as_posix()}/temp/fastcgi;
    uwsgi_temp_path {work.as_posix()}/temp/uwsgi;
    scgi_temp_path {work.as_posix()}/temp/scgi;
    include {(work / 'conf' / 'server.conf').as_posix()};
}}
""", encoding="utf-8")
print("rendered")
PYEOF

cleanup() {
  "$NGINX_HOME/nginx.exe" -s stop -c "$(cygpath -w "$WORK/conf/nginx.conf")" \
      -p "$(cygpath -w "$WORK")" >/dev/null 2>&1 || true
  sleep 1
  local pidfile="$WORK/temp/nginx.pid"
  if [[ -f "$pidfile" ]]; then
    local pid; pid="$(tr -d '\r\n ' < "$pidfile" 2>/dev/null || true)"
    [[ -n "$pid" ]] && taskkill //F //PID "$pid" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

( "$NGINX_HOME/nginx.exe" -c "$(cygpath -w "$WORK/conf/nginx.conf")" \
    -p "$(cygpath -w "$WORK")" > "$WORK/logs/startup.out" 2>&1 & )
for _ in $(seq 1 40); do
  curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/" && break
  sleep 0.3
done

echo "==> 制造 WS 握手（真实后端，经 nginx）"
"$VERIFY_PY" - "$PORT" "$BACKEND" "$OUT_JSON" "$WORK" <<'PYEOF'
import hashlib
import json
import pathlib
import subprocess
import sys
import time
import urllib.parse
import urllib.request

port, backend, out_json, work = sys.argv[1], sys.argv[2], sys.argv[3], pathlib.Path(sys.argv[4])
PASSWORD = "WsLog-Probe-Pw-1"
suffix = str(int(time.time()))[-8:]


def http(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{backend}/api{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return exc.code, {}


# 建一个一次性教师账号 + 课堂，用来拿合法票据
# username 单独取个变量：末尾的清理要按它精确删（只删本次这个）。
username = f"wslog-{suffix}"
st, body = http("POST", "/auth/register", {
    "username": username, "password": PASSWORD,
    "name": "wslog", "role": "teacher"})
if st != 200:
    sys.exit(f"register failed HTTP {st}: {body}")
token = body["data"]["token"]

st, body = http("POST", "/courses", {
    "name": f"WSLOG-{suffix}", "code": f"WSL{suffix}", "department": "t"}, token)
course = body["data"]["id"]
st, body = http("POST", "/sessions", {
    "courseId": course, "classId": "class-001", "title": f"WSLOG-L-{suffix}"}, token)
lesson = body["data"]["id"]

st, body = http("POST", "/auth/ws-ticket", {"lessonSessionId": lesson}, token)
ticket = (body.get("data") or {}).get("ticket", "")


def ws_handshake(query: str) -> int:
    """发一个真实 WS 升级请求，返回 HTTP 状态（101/401/403）。"""
    url = f"http://127.0.0.1:{port}/ws/classroom?{query}"
    proc = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8",
         "-H", "Connection: Upgrade", "-H", "Upgrade: websocket",
         "-H", "Sec-WebSocket-Version: 13",
         "-H", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
         "-H", "Origin: http://localhost:5173", url],
        capture_output=True, text=True)
    return int(proc.stdout.strip() or 0)


trials = []
trials.append(("valid-ticket", ws_handshake(f"lessonId={lesson}&ticket={ticket}")))
trials.append(("anonymous", ws_handshake(f"lessonId={lesson}")))
trials.append(("replayed-ticket", ws_handshake(f"lessonId={lesson}&ticket={ticket}")))
trials.append(("legacy-token-query", ws_handshake(f"lessonId={lesson}&token={token}")))
trials.append(("forged-ticket", ws_handshake(f"lessonId={lesson}&ticket={'0' * 32}")))

# 给 nginx 一点时间刷盘
time.sleep(1.5)

access = (work / "logs" / "access.log").read_text(encoding="utf-8", errors="replace") \
    if (work / "logs" / "access.log").exists() else ""
error = (work / "logs" / "error.log").read_text(encoding="utf-8", errors="replace") \
    if (work / "logs" / "error.log").exists() else ""

# 凭据的特征串。用**真实值**检查，而不是形状匹配：形状匹配会漏掉"恰好
# 不像 JWT 的泄漏"，而真实值检查的问题只是需要先知道值——这里我们自己造的。
secret_probes = {
    "jwt-access-token": token,
    "ws-ticket": ticket,
}
findings = []
for label, value in secret_probes.items():
    if not value:
        continue
    for logname, text in (("access.log", access), ("error.log", error)):
        if value in text:
            findings.append({"log": logname, "secret": label,
                             "detail": "完整值出现在日志中"})
# 签名查询串与查询参数
for logname, text in (("access.log", access), ("error.log", error)):
    for needle in ("ticket=", "token=", "?lessonId=", "X-Amz-Signature"):
        if needle in text:
            findings.append({"log": logname, "secret": needle,
                             "detail": f"日志中出现 {needle}"})

# /ws/ 是否进了 access.log
ws_lines = [l for l in access.splitlines() if "/ws/" in l]

# **交付前提**：合法票据必须真的拿到 101。否则说明 nginx 没起来、
# 反代没通、或后端拒绝了一切——此时日志当然是空的，"无泄漏"毫无意义。
# 本脚本第一版就栽在这里：5 个 trial 全返回 HTTP 0，脚本仍然 exit 0。
# 一个只在"什么都没发生"时通过的检查，比没有检查更危险。
served = dict(trials).get("valid-ticket", 0) == 101

payload = {
    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "harnessServed": served,
    "scope": "本机 nginx 1.27.4 + 真实后端 :8000；不含容器/镜像验证",
    "port": int(port),
    "backend": backend,
    "trials": [{"name": n, "http": c} for n, c in trials],
    "accessLogLines": len(access.splitlines()),
    "errorLogLines": len(error.splitlines()),
    "wsLinesInAccessLog": ws_lines[:5],
    "findings": findings,
    "passed": served and not findings and not ws_lines,
}
pathlib.Path(out_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

print(f"  access.log {len(access.splitlines())} 行，error.log {len(error.splitlines())} 行")
for name, code in trials:
    print(f"  {name:<20} HTTP {code}")
if not served:
    print("  !! 合法票据没有拿到 101：nginx 未服务或反代未通，"
          "本次'无泄漏'结论无效", file=sys.stderr)
if ws_lines:
    print(f"  !! /ws/ 出现在 access.log：{ws_lines[0][:120]}")
for f in findings:
    print(f"  !! {f['log']}: {f['secret']} — {f['detail']}")

# 清理本脚本注册的一次性账号及其课程/课堂。
# 不做这步的话每次运行都在 rig 库里留一个 `wslog-*` 教师（实测曾遗留多个），
# 而它们会出现在按教师过滤的列表接口结果里，让后续计数断言随运行次数漂移。
def _purge():
    sql = (
        "delete from lesson_sessions where teacher_id in "
        f"(select id from users where phone='{username}');"
        "delete from courses where teacher_id in "
        f"(select id from users where phone='{username}');"
        f"delete from users where phone='{username}';"
    )
    try:
        r = subprocess.run(
            ["docker", "exec", "mact-ui-postgres", "psql", "-U", "macteval",
             "-d", "macteval_ui", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"  !! 清理账号失败（不影响结论）：{r.stderr.strip()[:200]}",
                  file=sys.stderr)
    except Exception as exc:
        print(f"  !! 清理账号异常：{exc}", file=sys.stderr)

_purge()
sys.exit(0 if payload["passed"] else 1)
PYEOF
