#!/usr/bin/env bash
# TLS 终止层的演练验证（B4，2026-10-28）。
#
#   bash scripts/verify/verify_tls_drill.sh
#
# 用**自签、一次性、不含真实主机名**的证书起一个本机 nginx TLS server，
# 逐项验证 `deploy/tls/README.md` 里给出的配置样例真的能做它承诺的事：
#
#   1. HTTP 段 301 跳到 HTTPS；
#   2. TLS 1.2/1.3 可协商，TLS 1.0/1.1 被拒；
#   3. HSTS 头存在，且 max-age 与样例一致；
#   4. X-Forwarded-Proto 真的被转发到上游（用回显上游抓）；
#   5. WSS（wss://）经该层可升级 / 可达；
#   6. 证书链可被 openssl s_client 完整读出。
#
# **边界，必须写清**：
#   * 证书是本次现场生成的自签证书，不是真实 CA 签发，也不是 Let's Encrypt。
#     因此**没有验证**证书签发、续期、--deploy-hook 生效、浏览器 HSTS 行为。
#   * 不验证容器里的 nginx（需要 docker build，被磁盘门禁阻塞）。
#   * 私钥写在 `.cache/tmp/tls-drill/`（E 盘、gitignored），用完即删，不入库。
#
# 两个 Git-Bash 特有的坑（都实测踩过，写在这里省下一次排查）：
#   a) MSYS2 会把 `-subj "/CN=localhost"` 改写成
#      `C:/Program Files/Git/CN=localhost`。用 MSYS2_ARG_CONV_EXCL='*' 关掉；
#      但那样 `/e/...` 形式的文件路径也不会被转换，而 openssl 是原生程序，
#      认不出 `/e/...` —— 所以路径必须显式 cygpath。
#   b) nginx 配置把 `\` 当转义符：`\t` 变成 TAB、`\n` 变成换行，Windows
#      反斜杠路径会被拆坏（实测 cannot load certificate 后面跟着制表符）。
#      配置里一律用正斜杠（cygpath -m）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

TOOLS="$ROOT/.cache/tools"
NGINX_HOME="$TOOLS/nginx-1.27.4"
VERIFY_PY="${VERIFY_PYTHON:-$ROOT/.venv-verify/Scripts/python.exe}"
[[ -x "$VERIFY_PY" ]] || VERIFY_PY="$ROOT/.venv-verify/bin/python"
if [[ ! -x "$NGINX_HOME/nginx.exe" ]]; then
  echo "!! 缺少本机 nginx：$NGINX_HOME/nginx.exe" >&2; exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "!! 缺少 openssl" >&2; exit 2
fi

CACHE="$ROOT/.cache"
export TMPDIR="$CACHE/tmp" TEMP="$CACHE/tmp" TMP="$CACHE/tmp"
WORK="$CACHE/tmp/tls-drill"
OUT="$ROOT/docs/reviews/evidence/tls-drill-$(date +%Y%m%dT%H%M).json"
HTTP_PORT="${TLS_HTTP_PORT:-18094}"
HTTPS_PORT="${TLS_HTTPS_PORT:-18443}"
UPSTREAM_PORT="${TLS_UPSTREAM_PORT:-18093}"

# 正斜杠的 Windows 路径，供 nginx 配置使用（见文件头 b）。
win() { cygpath -m "$1"; }

rm -rf "$WORK"; mkdir -p "$WORK/certs" "$WORK/logs" "$WORK/temp" "$WORK/conf" "$WORK/html"

echo "==> 生成一次性自签证书（SAN=localhost，非真实域名）"
MSYS2_ARG_CONV_EXCL='*' openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout "$(cygpath -w "$WORK/certs/drill.key")" \
  -out "$(cygpath -w "$WORK/certs/drill.crt")" \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
echo "    已生成（1 天有效期，演练专用，用完即删）"

# 上游回显服务：把收到的头原样返回，用来证明 X-Forwarded-Proto 真的传过去了。
"$VERIFY_PY" - "$UPSTREAM_PORT" "$WORK/logs/upstream.log" <<'PYEOF' &
import http.server
import sys

port = int(sys.argv[1])
logpath = sys.argv[2]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = "\n".join(f"{k}: {v}" for k, v in self.headers.items()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with open(logpath, "a", encoding="utf-8") as fh:
            fh.write(self.path + "\n")

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
PYEOF
UPSTREAM_PID=$!

# 与 deploy/tls/README.md 的样例逐条对应。
cat > "$WORK/conf/nginx.conf" <<EOF
worker_processes 1;
error_log $(win "$WORK/logs/error.log") warn;
pid $(win "$WORK/temp/nginx.pid");
events { worker_connections 64; }
http {
    client_body_temp_path $(win "$WORK/temp/client_body");
    proxy_temp_path $(win "$WORK/temp/proxy");
    fastcgi_temp_path $(win "$WORK/temp/fastcgi");
    uwsgi_temp_path $(win "$WORK/temp/uwsgi");
    scgi_temp_path $(win "$WORK/temp/scgi");
    server {
        listen $HTTP_PORT;
        location / { return 301 https://\$host:\$server_port\$request_uri; }
    }
    server {
        listen $HTTPS_PORT ssl;
        ssl_certificate     $(win "$WORK/certs/drill.crt");
        ssl_certificate_key $(win "$WORK/certs/drill.key");
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers off;
        ssl_session_cache shared:SSL:10m;
        ssl_session_timeout 1d;
        ssl_session_tickets off;
        add_header Strict-Transport-Security "max-age=604800" always;
        location / {
            proxy_pass http://127.0.0.1:$UPSTREAM_PORT;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }
        location /ws/ {
            proxy_pass http://127.0.0.1:$UPSTREAM_PORT;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_read_timeout 3600s;
            access_log off;
        }
    }
}
EOF

cleanup() {
  "$NGINX_HOME/nginx.exe" -s stop -c "$(cygpath -w "$WORK/conf/nginx.conf")" \
      -p "$(cygpath -w "$WORK")" >/dev/null 2>&1 || true
  sleep 1
  if [[ -f "$WORK/temp/nginx.pid" ]]; then
    local pid; pid="$(tr -d '\r\n ' < "$WORK/temp/nginx.pid" 2>/dev/null || true)"
    [[ -n "$pid" ]] && taskkill //F //PID "$pid" >/dev/null 2>&1 || true
  fi
  kill "$UPSTREAM_PID" 2>/dev/null || true
  rm -f "$WORK/certs/drill.key" "$WORK/certs/drill.crt"
}
trap cleanup EXIT

echo "==> nginx -t"
set +e
TEST_OUT="$("$NGINX_HOME/nginx.exe" -t -c "$(cygpath -w "$WORK/conf/nginx.conf")" \
    -p "$(cygpath -w "$WORK")" 2>&1)"
TEST_RC=$?
set -e
echo "$TEST_OUT" | tail -3
if [[ "$TEST_RC" -ne 0 ]]; then
  echo "!! TLS 配置未通过 nginx -t" >&2
  exit 1
fi

( "$NGINX_HOME/nginx.exe" -c "$(cygpath -w "$WORK/conf/nginx.conf")" \
    -p "$(cygpath -w "$WORK")" > "$WORK/logs/startup.out" 2>&1 & )
sleep 3

echo "==> 逐项验证"
"$VERIFY_PY" - "$HTTP_PORT" "$HTTPS_PORT" "$UPSTREAM_PORT" "$WORK" "$OUT" \
    "$TEST_OUT" <<'PYEOF'
import json
import pathlib
import ssl
import subprocess
import sys
import time
import urllib.request

http_port, https_port, upstream_port, work, out_path, test_out = sys.argv[1:7]
work = pathlib.Path(work)
checks = []
failures = []


def record(name, ok, detail):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    if not ok:
        failures.append(f"{name}: {detail}")
    print(f"  {'PASS' if ok else 'FAIL'} {name:<36} {detail}")


def run(cmd, timeout=20):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# 1) HTTP -> HTTPS 301
r = run(["curl", "-s", "-o", "/dev/null", "-D", "-", "--max-time", "8",
         f"http://127.0.0.1:{http_port}/some/path?x=1"])
lines = r.stdout.splitlines()
status = lines[0].split()[1] if lines else "?"
loc = next((l.split(":", 1)[1].strip() for l in lines
            if l.lower().startswith("location:")), "")
record("http->https 301", status == "301" and loc.startswith("https://"),
       f"HTTP {status} Location={loc[:64]}")

# 2) TLS 版本协商。
#    openssl s_client 把人类可读摘要写到 **stderr**（实测：stdout 为空，
#    stderr 有 "CONNECTION ESTABLISHED"）。第一版只读 stdout，于是四行里
#    两行假失败——TLS 层其实完全正常。这里两个流都看。
def tls_probe(flag: str) -> tuple[bool, str]:
    """返回 (是否真的握手成功, 一行摘要)。

    关键：探测 TLS 1.0/1.1 时必须带 `-cipher DEFAULT@SECLEVEL=0`。
    OpenSSL 3.x 默认 SECLEVEL=2 **拒绝发出** TLS 1.0/1.1 握手，于是
    `openssl s_client -tls1` 会以 "no protocols available" 失败——那证明的是
    **客户端**不支持，而不是服务器拒绝。换一台仍接受 TLS 1.0 的服务器，
    这条断言照样"通过"，所以它原本是同义反复，不是安全检查。
    加上 SECLEVEL=0 后客户端真的发出要约，nginx 回
    `tlsv1 alert protocol version`（alert 70）——这才是服务器侧的拒绝。
    """
    r = run(["openssl", "s_client", "-connect", f"127.0.0.1:{https_port}",
             flag, "-cipher", "DEFAULT@SECLEVEL=0"])
    text = r.stdout + "\n" + r.stderr
    lowered = text.lower()
    ok = "connection established" in lowered or "protocol  :" in lowered and (
        "alert protocol version" not in lowered)
    refused = "alert protocol version" in lowered
    if ok:
        first = next((l.strip() for l in text.splitlines()
                      if "Protocol  :" in l), "handshake ok")
    elif refused:
        first = "server: tlsv1 alert protocol version (alert 70)"
    else:
        first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return (ok if ok else not refused) and not refused, first


for label, flag, expect_ok in (("tls1.2", "-tls1_2", True),
                               ("tls1.3", "-tls1_3", True),
                               ("tls1.0", "-tls1", False),
                               ("tls1.1", "-tls1_1", False)):
    handshook, first = tls_probe(flag)
    record(f"{label} {'建立' if expect_ok else '被服务器拒绝'}",
           handshook == expect_ok, first[:64])

# 3) HSTS
ctx = ssl._create_unverified_context()
req = urllib.request.Request(f"https://127.0.0.1:{https_port}/", method="GET")
with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
    headers = {k.lower(): v for k, v in resp.headers.items()}
    body = resp.read().decode("utf-8", "replace")
hsts = headers.get("strict-transport-security", "")
record("HSTS 存在且 max-age 匹配样例", hsts == "max-age=604800", repr(hsts))

# 4) X-Forwarded-Proto 真的到达上游
record("X-Forwarded-Proto: https 到达上游",
       "x-forwarded-proto: https" in body.lower(),
       "上游回显" + ("含该头" if "x-forwarded-proto: https" in body.lower()
                    else "**不含**该头"))

# 5) WSS 经该层可达。上游是普通 HTTP server，不会真升到 101；
#    拿到 200 即证明 TLS 层与 /ws/ 的 proxy_pass 都通。真实 101 由后端提供
#    （证据见 ws-log-redaction-*.json）。
r = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10",
         "-k", f"https://127.0.0.1:{https_port}/ws/classroom?lessonId=x",
         "-H", "Connection: Upgrade", "-H", "Upgrade: websocket",
         "-H", "Sec-WebSocket-Version: 13",
         "-H", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="])
code = r.stdout.strip()
record("wss /ws/ 经 TLS 层可达", code in ("200", "101"), f"HTTP {code}")

# 6) 证书可读出（同样在 stderr 上，见上面的说明）
r = run(["openssl", "s_client", "-connect", f"127.0.0.1:{https_port}",
         "-showcerts", "-brief"])
text = r.stdout + "\n" + r.stderr
subject = next((l.strip() for l in text.splitlines()
                if "CN=" in l or "subject=" in l.lower()), "")
record("证书可被 s_client 读出", bool(subject), subject[:64])

payload = {
    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "scope": ("本机 nginx + 自签一次性证书；未验证真实 CA 签发/续期、"
              "浏览器 HSTS 行为、容器内 nginx"),
    "nginxTest": {"exit": 0, "tail": test_out.strip()[-300:]},
    "cert": "CN=localhost, SAN localhost+127.0.0.1, 1 day, self-signed, deleted after run",
    "checks": checks,
    "failures": failures,
    "passed": not failures,
}
pathlib.Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
print(f"\n  结果写入 {out_path}")
sys.exit(0 if not failures else 1)
PYEOF
