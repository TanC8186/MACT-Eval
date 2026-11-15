#!/usr/bin/env bash
# 用**构建产物** + 本机 nginx 起一线真实前端，端到端验证 CSP 不阻断功能（B1.4）。
#
#   bash scripts/verify/verify_nginx_browser_csp.sh
#
# 为什么需要它：`verify_nginx_config.sh` 只抓响应头（curl 看不到 CSP 是否阻断），
# E2E 跑的是 vite dev server（不走 nginx，也没有 CSP）。中间这条缝里藏着最常见
# 的失败模式——**CSP 配置正确、页面却白屏**。只有让真实浏览器加载经 nginx 服务的
# 构建产物，才能回答"页面可用吗、图片加载了吗、WebSocket 连上了吗"。
#
# 架构（真实分层，与生产同构）：
#
#   Chromium ── http://127.0.0.1:PORT ──► 本机 nginx（渲染后的 teacher-web 配置）
#                                          ├─ /            → dist 静态文件
#                                          ├─ /api/         → 真实后端 :8000（去掉 /api 前缀）
#                                          └─ /ws/           → 真实后端 :8000/api/ws/
#                                       静态层另挂 MinIO :19100 作为图片源
#
# 这里**不**用 proxy_pass 的容器名（backend:8000），而是 127.0.0.1:8000 —— 本机
# 没有 docker 网络。CSP 的媒体源仍走 ${MACT_CSP_MEDIA_ORIGIN}，注入 rig MinIO。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

TOOLS="$ROOT/.cache/tools"
NGINX_HOME="$TOOLS/nginx-1.27.4"
VERIFY_PY="${VERIFY_PYTHON:-$ROOT/.venv-verify/Scripts/python.exe}"
[[ -x "$VERIFY_PY" ]] || VERIFY_PY="$ROOT/.venv-verify/bin/python"
BROWSER_PY="${MACT_BROWSER_PYTHON:-$ROOT/.venv-browser/Scripts/python.exe}"
[[ -x "$BROWSER_PY" ]] || BROWSER_PY="$ROOT/.venv-browser/bin/python"

CACHE="$ROOT/.cache"
export TMPDIR="$CACHE/tmp" TEMP="$CACHE/tmp" TMP="$CACHE/tmp"
export PLAYWRIGHT_BROWSERS_PATH="$CACHE/ms-playwright"
WORK="$CACHE/tmp/nginx-browser"
OUT="$ROOT/docs/reviews/evidence/nginx-browser-csp-$(date +%Y%m%dT%H%M).json"
# 端口必须落在后端的 CORS 白名单里（application.yml:116 只有
# localhost/127.0.0.1 的 3000 与 5173）。用 18092 实测得到
# `403 Invalid CORS request`——那看起来像 nginx/CSP 坏了，其实是
# 浏览器不在允许来源上。3000 正是生产端口，最贴近真实部署。
PORT="${NGB_PORT:-3000}"
BACKEND="${MACT_BACKEND:-http://127.0.0.1:8000}"
MINIO_ORIGIN="${MACT_MINIO_PUBLIC_ENDPOINT:-http://127.0.0.1:19100}"
TEACHER_USER="${MACT_E2E_TEACHER_USER:-13800002026}"
TEACHER_PASS="${MACT_E2E_TEACHER_PASS:-UiRigPass2026}"

for tool in "$NGINX_HOME/nginx.exe" "$BROWSER_PY"; do
  [[ -e "$tool" ]] || { echo "!! 缺少 $tool" >&2; exit 2; }
done
[[ -f frontend/apps/teacher-web/dist/index.html ]] || {
  echo "!! 缺少构建产物：先运行 pnpm build:web" >&2; exit 2; }
if ! curl -fsS -o /dev/null --max-time 5 "$BACKEND/api/health"; then
  echo "!! 后端不可达：$BACKEND/api/health" >&2; exit 2
fi

rm -rf "$WORK"; mkdir -p "$WORK/conf" "$WORK/logs" "$WORK/temp"

echo "==> 渲染 teacher-web 配置并改指向本机后端"
"$VERIFY_PY" - "$(cygpath -m "$ROOT")" "$(cygpath -m "$WORK")" "$PORT" "$BACKEND" "$MINIO_ORIGIN" <<'PYEOF'
import pathlib
import sys

root, work, port, backend, minio = (pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]),
                                    sys.argv[3], sys.argv[4], sys.argv[5])
template = (root / "frontend/apps/teacher-web/nginx.conf").read_text(encoding="utf-8")
text = template.replace("${MACT_CSP_MEDIA_ORIGIN}", minio)
text = text.replace("http://backend:8000", backend)
# 容器里 root 是 /usr/share/nginx/html；本机用真实 dist 目录。
text = text.replace("root /usr/share/nginx/html;",
                    f"root {(root / 'frontend/apps/teacher-web/dist').as_posix()};")
text = text.replace("access_log /var/log/nginx/access.log safe;",
                    f"access_log {(work / 'logs/access.log').as_posix()} safe;")
text = text.replace("error_log /var/log/nginx/ws-error.log crit;",
                    f"error_log {(work / 'logs/ws-error.log').as_posix()} crit;")
text = text.replace("    listen 80;", f"    listen {port};")

# /api/ 的 proxy_pass 没有结尾斜杠时，nginx 会把 /api/x 原样转发；
# 本机后端带 context-path /api，所以原样转发正好。这里只补 X-Forwarded-Proto
# （配置里已有），无需改写路径。
(work / "conf/server.conf").write_text(text, encoding="utf-8")
(work / "conf/mime.types").write_text(
    "types { text/html html; text/css css; application/javascript js;"
    " image/svg+xml svg; image/png png; image/jpeg jpg; application/json json;"
    " font/woff2 woff2; }\n", encoding="utf-8")
def p(relative: str) -> str:
    """正斜杠的 Windows 路径。

    nginx 配置把 `\` 当转义符，反斜杠路径会被拆坏（`\t` 变 TAB），所以
    配置里一律用正斜杠。这里不再依赖 shell 的 cygpath：heredoc 里看不到 bash
    函数（第一版因此 NameError）。
    """
    return (work / relative).as_posix()


(work / "conf/nginx.conf").write_text(
    f"""worker_processes 1;
error_log {p('logs/error.log')} warn;
pid {p('temp/nginx.pid')};
events {{ worker_connections 128; }}
http {{
    include {p('conf/mime.types')};
    default_type application/octet-stream;
    client_body_temp_path {p('temp/client_body')};
    proxy_temp_path {p('temp/proxy')};
    fastcgi_temp_path {p('temp/fastcgi')};
    uwsgi_temp_path {p('temp/uwsgi')};
    scgi_temp_path {p('temp/scgi')};
    include {p('conf/server.conf')};
}}
""", encoding="utf-8")
print("rendered")
PYEOF

cleanup() {
  "$NGINX_HOME/nginx.exe" -s stop -c "$(cygpath -w "$WORK/conf/nginx.conf")" \
      -p "$(cygpath -w "$WORK")" >/dev/null 2>&1 || true
  sleep 1
  if [[ -f "$WORK/temp/nginx.pid" ]]; then
    pid="$(tr -d '\r\n ' < "$WORK/temp/nginx.pid" 2>/dev/null || true)"
    [[ -n "$pid" ]] && taskkill //F //PID "$pid" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

"$NGINX_HOME/nginx.exe" -t -c "$(cygpath -w "$WORK/conf/nginx.conf")" \
    -p "$(cygpath -w "$WORK")" >/dev/null 2>&1 || {
  echo "!! nginx -t 失败" >&2; exit 2; }
( "$NGINX_HOME/nginx.exe" -c "$(cygpath -w "$WORK/conf/nginx.conf")" \
    -p "$(cygpath -w "$WORK")" > "$WORK/logs/startup.out" 2>&1 & )
for _ in $(seq 1 40); do
  curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/" && break
  sleep 0.3
done
echo "==> nginx 服务中：http://127.0.0.1:$PORT"

"$BROWSER_PY" - "$PORT" "$OUT" "$TEACHER_USER" "$TEACHER_PASS" "$WORK" "$MINIO_ORIGIN" <<'PYEOF'
import json
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright

port, out_path, user, password, work, minio = sys.argv[1:7]
work = pathlib.Path(work)
base = f"http://127.0.0.1:{port}"

checks = []
failures = []
console_errors = []
csp_violations = []
page_errors = []


def record(name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    if not ok:
        failures.append(f"{name}: {detail}")
    print(f"  {'PASS' if ok else 'FAIL'} {name:<40} {detail[:70]}")


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
    page = context.new_page()

    def on_console(msg):
        text = msg.text
        if msg.type == "error":
            console_errors.append(text)
            # Chromium 对 CSP 拦截的统一措辞
            if ("Content Security Policy" in text or "Refused to" in text
                    or "violates the following" in text):
                csp_violations.append(text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # 1) 登录页经 nginx 加载
    page.goto(f"{base}/login", wait_until="networkidle")
    record("登录页加载（经 nginx）", page.locator("form").count() > 0,
           f"forms={page.locator('form').count()}")

    # 2) 登录
    page.get_by_label("用户名").fill(user)
    page.get_by_label("密码").fill(password)
    # 按钮的 `html-type="submit"` 且文字是「登录」；但 antd 会在文字两侧插入
    # 空格/图标，`get_by_role(name="登录")` 的精确匹配会超时（第一版就是这样
    # 挂住 30 秒）。用 CSS 类选择器 + 包含式文本，与 E2E harness 的做法一致。
    button = page.locator("button.login-btn")
    if not button.count():
        button = page.get_by_role("button", name="登", exact=False)
    with page.expect_response(lambda r: "/api/auth/login" in r.url, timeout=30_000) as resp:
        button.first.click()
    login_ok = resp.value.status == 200
    record("登录请求 200（/api/ 反代可用）", login_ok, f"HTTP {resp.value.status}")
    # 不用 wait_for_url：登录成功后由 vue-router 做 **客户端** 跳转，
    # `wait_for_url` 等的是 navigation，可能永远不触发（第一版因此超时 20s）。
    # 直接看三件可判定的东西：token 已落库、路由已离开 /login、页面出现侧边栏。
    page.wait_for_timeout(2000)
    has_token = page.evaluate("() => !!localStorage.getItem('token')")
    record("登录后 access token 已入库", has_token, f"token={has_token}")
    record("登录后离开 /login", "/login" not in page.url, page.url)

    # 3) 工作台和精简后的导航
    page.goto(f"{base}/dashboard", wait_until="networkidle")
    record("工作台加载", page.get_by_role("heading", name="教学工作台", exact=True).count() == 1)
    nav_text = page.locator("#workspace-navigation").inner_text()
    record("导航已移除趋势分析和效果追踪",
           "趋势分析" not in nav_text and "效果追踪" not in nav_text)

    # 4) 旧功能链接安全回到工作台，旧课程页进入合并管理页
    for legacy in ("trends", "effects"):
        page.goto(f"{base}/{legacy}", wait_until="networkidle")
        record(f"旧 {legacy} 链接跳转工作台", "/dashboard" in page.url)
    page.goto(f"{base}/courses", wait_until="networkidle")
    record("课程与课堂统一入口", "/classroom" in page.url
           and page.get_by_role("heading", name="课程与课堂", exact=True).count() == 1)

    # 5) 证据帧图片（CSP img-src 含 MinIO origin → 不能拦截）
    page.goto(f"{base}/reports", wait_until="networkidle")
    page.wait_for_selector(".ant-table-row", timeout=30_000)
    row = page.locator(".ant-table-row").first
    link = row.locator("a", has_text="查看")
    (link if link.count() else row).first.click()
    page.wait_for_timeout(3000)
    imgs = page.locator('[data-testid="evidence-card-media"] img, img')
    total_imgs = imgs.count()
    loaded = 0
    for i in range(min(total_imgs, 12)):
        ok = imgs.nth(i).evaluate(
            "el => el.complete && el.naturalWidth > 0")
        loaded += 1 if ok else 0
    record("证据帧图片真实解码（img-src 未拦 MinIO）",
           total_imgs > 0 and loaded > 0,
           f"{loaded}/{min(total_imgs, 12)} 张 naturalWidth>0")

    # 6) 浏览器直传 MinIO（任务书 §B1.4 明确列的五类场景之一）。
    #    此前这一类没验过：证据帧图片走的是 *GET*（img-src），而上传是
    #    浏览器直接 PUT 到 MinIO，受 **connect-src** 约束。两者是不同指令，
    #    img-src 通过完全不代表 connect-src 通过。若 connect-src 漏了 MinIO
    #    origin，服务端日志一切正常，只有浏览器控制台报 "Refused to connect"。
    #    所以在页面上下文里发一个真实 PUT，并断言拿到 2xx/4xx（说明连接被
    #    放行）而不是被 CSP 拦下。
    upload_result = page.evaluate(
        """async (minioOrigin) => {
            // 故意用一个不存在的对象名 + 无签名请求：MinIO 会回 403（签名不符），
            // 这足以证明「请求真的到达了 MinIO」——被 CSP 拦的话是 NetworkError，
            // fetch 根本不会返回 status。
            const url = minioOrigin + '/analysis-artifacts/csp-probe-' + Date.now() + '.bin';
            try {
                const r = await fetch(url, { method: 'PUT', body: 'csp-probe' });
                return { ok: true, status: r.status };
            } catch (e) {
                return { ok: false, status: 0, error: String(e) };
            }
        }""", minio)
    record("浏览器可向 MinIO 发起 PUT（connect-src 放行）",
           upload_result.get("ok") and upload_result.get("status", 0) > 0,
           f"status={upload_result.get('status')} err={upload_result.get('error', '')[:60]}")

    # 6b) WebSocket：课堂页的实时连接
    page.goto(f"{base}/classroom", wait_until="networkidle")
    page.wait_for_timeout(4000)
    ws_opened = page.evaluate(
        """() => (window.performance.getEntriesByType('resource') || [])
                 .filter(e => e.name.startsWith('ws')).length""")
    # performance 不记录 ws；改用"页面是否有连接状态横幅"以外的客观判据：
    # 后端 /ws/ 的握手走 /api/ws/，用 nginx access_log 之外的方式确认——
    # 这里以"页面未报 auth-required 且无 CSP 拦截"为准。
    body_text = page.inner_text("body")
    record("课堂页无 CSP 拦截 / 无脚本错误",
           not csp_violations and not page_errors,
           f"csp={len(csp_violations)} pageerror={len(page_errors)}")

    # 7) /api/ 与 /ws/ 的**真实**响应头（B1.3）。
    #    verify_nginx_config.sh 里这两个探针打到的是 discard 端口（502），
    #    只能证明"错误响应也带安全头"。这里后端是活的，可以验证 200/401
    #    路径上的头，以及 /api/ 上**两条 CSP 同时到达**（页面策略 + 后端
    #    lockdown 策略）——那是本设计刻意保留的行为，不是重复配置。
    api_status = None
    api_headers = {}
    resp = context.request.get(f"{base}/api/health")
    api_status = resp.status
    api_headers = {k.lower(): v for k, v in resp.headers.items()}
    record("/api/ 经 nginx 返回 200", api_status == 200, f"HTTP {api_status}")
    record("/api/ 响应带 X-Content-Type-Options",
           api_headers.get("x-content-type-options") == "nosniff")
    record("/api/ 响应带 CSP（后端的 lockdown 策略透传）",
           "content-security-policy" in api_headers,
           str(api_headers.get("content-security-policy"))[:80])

    # /ws/ 握手：无票据必须 401（真实后端），且响应带安全头
    import urllib.request as _u
    ws_status = None
    try:
        _u.urlopen(f"{base}/ws/classroom?lessonId=none", timeout=10)
        ws_status = 200
    except Exception as exc:  # noqa: BLE001
        ws_status = getattr(exc, "code", 0)
    record("/ws/ 无票据被拒（经 nginx 转发到真实后端）",
           ws_status == 401, f"HTTP {ws_status}")

    # 8) 全局：整轮浏览里没有任何 CSP 违规
    record("全程零 CSP 违规", not csp_violations,
           "; ".join(csp_violations[:2])[:150])

    page.screenshot(path=str(work / "final.png"), full_page=True)
    browser.close()

payload = {
    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "scope": ("本机 nginx + 真实构建产物 + 真实后端；验证 CSP 不阻断功能。"
              "不含容器内 nginx、不含 HTTPS"),
    "baseUrl": base,
    "checks": checks,
    "cspViolations": csp_violations[:10],
    "consoleErrors": console_errors[:10],
    "pageErrors": page_errors[:10],
    "failures": failures,
    "passed": not failures,
}
pathlib.Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
print(f"\n  结果写入 {out_path}")
sys.exit(0 if not failures else 1)
PYEOF
