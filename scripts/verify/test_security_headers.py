#!/usr/bin/env python
"""安全响应头与 WebSocket 凭据的静态契约检查（SEC-01 / SEC-02，2026-10-28）。

    python -I scripts/verify/test_security_headers.py
    python -I scripts/verify/test_security_headers.py --self-test

**为什么需要静态检查而不是只靠 curl**：真实响应头需要在容器里跑 nginx 才能抓，
而本轮 Docker 数据根仍在 C 盘、任务书要求阻塞任何 docker build/up。静态检查
不能替代实测，但它能在 CI 里守住"配置被改回去"这一类回归——那是概率最高、
也最难被人工评审发现的失败。实测部分见交付报告的未验证清单，不在这里假装。

本文件检查四件事：

1. nginx 模板里每个 `location` 都带齐安全头。nginx 的 `add_header` 继承是
   **全有或全无**：子块只要出现一条 add_header，父块的整套就全部失效。
   这是最容易被写错、且浏览器侧只表现为"某些页面缺头"的地方。
2. CSP 不含宽泛来源（单独的 `*`），且不因为"想省事"而放行 `unsafe-inline`
   到 script-src（构建产物里没有内联脚本，放行它没有依据）。
3. WebSocket 凭据不再出现在查询串：前端源码、`ws_auth.py`、nginx 日志格式
   三处同时检查——只改一处等于没改。
4. 后端 SecurityConfig 显式配置这些头，而不是依赖框架默认值。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class Failure:
    check: str
    detail: str


# ---------------------------------------------------------------------------
# nginx 模板解析（够用即可：只切 server/location 块与 add_header 行）
# ---------------------------------------------------------------------------
def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def nginx_blocks(text: str) -> dict[str, list[str]]:
    """返回 {location 名字或 'server': [该块内直接出现的行]}。

    只做一层嵌套：本项目的 nginx.conf 没有更深的 location 嵌套，
    写成通用解析器反而会在括号出现在字符串里时出错。
    """
    blocks: dict[str, list[str]] = {"server": []}
    current = "server"
    depth = 0
    for raw in text.splitlines():
        line = raw.strip()
        # 去掉 `#` 注释（本项目的 CSP 里没有 `#`，注释都在整行）
        code = "" if line.startswith("#") else line.split("#", 1)[0].strip()
        match = re.match(r"location\s+([^\s{]+)\s*\{", code)
        if match:
            current = match.group(1)
            blocks[current] = []
            depth += 1
            continue
        if code.startswith("server") and code.endswith("{"):
            current = "server"
            blocks.setdefault("server", [])
            depth += 1
            continue
        if code == "}":
            depth -= 1
            if depth <= 1:
                current = "server"
            continue
        if code:
            blocks.setdefault(current, []).append(code)
    return blocks


def strip_js_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释。

    注释里会**引用**旧写法作为反例（"以前写的是 ?token=..."），把它当成
    实际使用去报警是假阳性。检查必须针对代码，不是文档。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def headers_in(lines: list[str]) -> dict[str, str]:
    """抽出 add_header 名 → 值（同名的取最后一次，与 nginx 行为一致）。

    值有两种写法：带引号的字面量（多数头）与**不带引号的变量**
    （`add_header Content-Security-Policy $mact_csp always;`）。
    只认引号写法会把这三种"变量引用"判成"缺少该头"，于是检查器会
    反过来逼人写回字面量副本——正好与"策略只写一份"的目标相反。
    """
    out: dict[str, str] = {}
    quoted = re.compile(r'add_header\s+([A-Za-z0-9-]+)\s+"([^"]*)"\s*(always)?')
    bare = re.compile(r'add_header\s+([A-Za-z0-9-]+)\s+(\$[A-Za-z0-9_]+)\s*(always)?')
    for line in lines:
        match = quoted.match(line) or bare.match(line)
        if match:
            out[match.group(1).lower()] = match.group(2)
    return out


REQUIRED_HEADERS = [
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
]


def check_nginx() -> list[Failure]:
    failures: list[Failure] = []
    text = read("frontend/apps/teacher-web/nginx.conf")
    blocks = nginx_blocks(text)

    # 1) 变量定义必须唯一。
    #    `set $mact_csp "..."` 只应出现一次：策略一旦有第二份副本，
    #    两处就会各自漂移，而浏览器对多条 CSP 取交集，实际策略谁也不是。
    csp_defs = re.findall(r'^\s*set\s+\$mact_csp\s+"([^"]*)"', text, re.M)
    if len(csp_defs) != 1:
        failures.append(Failure(
            "nginx csp",
            f"$mact_csp 应恰好定义一次，实际 {len(csp_defs)} 次"
            "（多份副本会漂移；各 location 应引用同一个变量）"))
    perms_defs = re.findall(r'^\s*set\s+\$mact_permissions\s+"([^"]*)"', text, re.M)
    if len(perms_defs) != 1:
        failures.append(Failure(
            "nginx permissions",
            f"$mact_permissions 应恰好定义一次，实际 {len(perms_defs)} 次"))

    csp = csp_defs[0] if csp_defs else ""

    # 2) 每个 add_header Content-Security-Policy 都必须引用变量，
    #    不能又贴一份字面量。
    for name, lines in blocks.items():
        for line in lines:
            match = re.match(r'add_header\s+Content-Security-Policy\s+"', line)
            if match:
                failures.append(Failure(
                    f"nginx {name}",
                    "add_header 直接写了 CSP 字面量；应引用 $mact_csp，"
                    "否则每一处都可能与定义漂移"))
            if re.match(r'add_header\s+Permissions-Policy\s+"', line):
                failures.append(Failure(
                    f"nginx {name}",
                    "add_header 直接写了 Permissions-Policy 字面量；应引用 $mact_permissions"))

    # 3) 头继承：子块里只要出现 add_header，就必须带齐整套。
    server_headers = headers_in(blocks.get("server", []))
    server_missing = [h for h in REQUIRED_HEADERS if h not in server_headers]
    if server_missing:
        failures.append(Failure("nginx server", f"server 级缺少安全头 {server_missing}"))
    for name, lines in blocks.items():
        if name == "server" or not lines:
            continue
        present = headers_in(lines)
        if not present:
            continue  # 全部继承自 server，正确
        missing = [h for h in REQUIRED_HEADERS if h not in present]
        if missing:
            failures.append(Failure(
                f"nginx {name}",
                f"该 location 自己写了 add_header，于是不再继承 server 级，"
                f"但缺少 {missing}"))

    # 4) 每条 add_header 都必须带 always。
    for name, lines in blocks.items():
        for line in lines:
            if line.startswith("add_header") and "always" not in line:
                failures.append(Failure(f"nginx {name}", f"add_header 缺少 always: {line[:80]}"))

    # 5) CSP 语义（针对唯一那份定义）
    if not csp:
        failures.append(Failure("nginx csp", "没有可解析的 $mact_csp 定义"))
        return failures
    for directive in ("default-src 'self'", "object-src 'none'",
                      "frame-ancestors 'none'", "base-uri 'self'"):
        if directive not in csp:
            failures.append(Failure("nginx csp", f"CSP 缺少 {directive}"))
    script_src = re.search(r"script-src([^;]*)", csp)
    if script_src:
        value = script_src.group(1)
        if "'unsafe-inline'" in value:
            failures.append(Failure("nginx csp", "script-src 含 'unsafe-inline'（构建产物不需要）"))
        if "'unsafe-eval'" not in value:
            # ECharts 运行时 new Function；去掉它图表不渲染。
            # 这是一个**已知放宽**，不是遗漏——见 nginx.conf 的说明。
            failures.append(Failure(
                "nginx csp", "script-src 缺 'unsafe-eval'（ECharts 需要；"
                             "若要收紧，必须先在真实浏览器里验证图表仍渲染）"))
    style_src = re.search(r"style-src([^;]*)", csp)
    if style_src and "'unsafe-inline'" not in style_src.group(1):
        failures.append(Failure("nginx csp", "style-src 缺 'unsafe-inline'（Vue/antd/ECharts 需要）"))
    if re.search(r"(?<![\w'*])\*(?![\w'*])", csp):
        failures.append(Failure("nginx csp", "CSP 含裸通配符 *，等于放弃限制"))
    if "${MACT_CSP_MEDIA_ORIGIN}" not in csp:
        failures.append(Failure("nginx csp", "媒体源未使用 ${MACT_CSP_MEDIA_ORIGIN}"))
    for directive in ("img-src", "media-src", "connect-src"):
        match = re.search(directive + r"([^;]*)", csp)
        if not match:
            failures.append(Failure("nginx csp", f"CSP 缺少 {directive}"))
            continue
        value = match.group(1)
        if directive != "connect-src" and "blob:" not in value:
            failures.append(Failure("nginx csp", f"{directive} 缺 blob:（本地预览需要）"))
        if "${MACT_CSP_MEDIA_ORIGIN}" not in value:
            failures.append(Failure("nginx csp", f"{directive} 缺 MinIO 源"))

    # 6) 日志格式不得含查询串（SEC-02）
    if "log_format safe" not in text:
        failures.append(Failure("nginx log", "没有定义 safe 日志格式"))
    safe_format = re.search(r"log_format\s+safe\s+(.*?);", text, re.S)
    if safe_format:
        fmt = safe_format.group(1)
        # 用词边界匹配：$request_method $uri 是安全的，而 $request 不是。
        for forbidden in ("request", "request_uri", "args", "query_string"):
            if re.search(r"\$" + forbidden + r"(?![A-Za-z0-9_])", fmt):
                failures.append(Failure(
                    "nginx log", f"safe 格式含 ${forbidden}（会记录查询串，凭据随之落盘）"))
        if "$uri" not in fmt:
            failures.append(Failure("nginx log", "safe 格式不含 $uri，排障时缺少路径信息"))

    # 7) /ws/ 必须同时关掉 access_log **和** 收紧 error_log。
    #    只关 access_log 不够：error 级日志在上游不可达时打印含查询串的
    #    request 行与 upstream URL（2026-10-28 实测），票据会明文进 error.log。
    ws_lines = "\n".join(blocks.get("/ws/", []))
    if "access_log off" not in ws_lines:
        failures.append(Failure("nginx /ws/", "/ws/ 未关闭访问日志"))
    if not re.search(r"error_log\s+\S+\s+crit\s*;", ws_lines):
        failures.append(Failure(
            "nginx /ws/",
            "/ws/ 的 error_log 未收紧到 crit：上游不可达时 nginx 会在 error 级"
            "日志里打印含查询串的 request 与 upstream URL（实测），票据随之落盘"))
    if "error_log /dev/null" in ws_lines:
        failures.append(Failure(
            "nginx /ws/",
            "error_log 不能用 /dev/null：Windows 上会被当相对路径 E:/dev/null，"
            "nginx 启动失败（实测 emerg CreateFile failed）"))

    # 8) /api/ 必须**不** hide 后端 CSP。
    #    这是本轮有意改掉的决定：hide 之后浏览器只剩页面策略，而 JSON 响应
    #    需要的是后端的 lockdown 策略（default-src 'none'）。两条同时到达时
    #    浏览器取交集 = 锁定策略，正是想要的语义。
    api_lines = blocks.get("/api/", [])
    for line in api_lines:
        if line.startswith("proxy_hide_header Content-Security-Policy"):
            failures.append(Failure(
                "nginx /api/",
                "hide 掉后端 CSP 会让 JSON 响应只剩页面策略（更弱）；"
                "应让两条同时到达，由浏览器取交集得到 default-src 'none'"))
    if any(line.startswith("add_header") for line in api_lines):
        failures.append(Failure(
            "nginx /api/",
            "/api/ 写了 add_header 会导致不再继承 server 级；"
            "它应保持零 add_header"))

    return failures


def check_nginx_structure() -> list[Failure]:
    """结构检查：括号配平 + 每条指令以 ; 或 { 结尾。

    **这不是 `nginx -t` 的替代。** 它只覆盖两类最常见的启动失败（漏分号、
    括号不配平），且无法发现未知指令、错误的上下文、变量未定义等。真正的
    语法验证需要跑一次容器；本轮因磁盘门禁阻塞 docker，没有做——见交付报告的
    未验证清单。
    """
    failures: list[Failure] = []
    text = read("frontend/apps/teacher-web/nginx.conf")

    # 逐行扫描时必须跟踪引号状态：nginx 允许一条指令跨多行（`log_format`
    # 就是这么写的），判定完成的标准是"第一个引号外的 ;"。只按行尾判断会把
    # 合法的多行指令全部报错。
    depth = 0
    in_quote = False
    pending_start = 0
    pending = ""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        code = line.split("#", 1)[0].strip()
        if not code:
            continue

        carried_over = bool(pending)  # 上一行起就有未结束的指令
        terminated = False
        for ch in code:
            if ch == "'":
                in_quote = not in_quote
            elif not in_quote:
                if ch == ";":
                    terminated = True
                    carried_over = False
                elif ch == "{":
                    if carried_over:
                        # 上文那条指令没写 ;，却直接跟了一个开块。
                        # 这正是 `add_header ... always` 漏分号的形态；
                        # 若把 { 当成终止符，这个真实错误就被吞掉了
                        # （本文件的自测曾因此漏掉该变异）。
                        failures.append(Failure(
                            "nginx syntax",
                            f"第 {lineno} 行开启块，但第 {pending_start} 行起的指令"
                            f"没有以 ; 结束：{pending[:70]}"))
                        pending = ""
                        carried_over = False
                    depth += 1
                    terminated = True
                elif ch == "}":
                    depth -= 1
                    terminated = True
                    if depth < 0:
                        failures.append(Failure(
                            "nginx syntax", f"第 {lineno} 行括号提前闭合"))
                        depth = 0
        if not pending:
            pending_start = lineno
        pending += (" " if pending else "") + code
        if terminated and not in_quote:
            # 一条语句里出现两个同名指令 = 上一条漏了 ;。nginx 会把它合并成
            # 一个指令并在参数个数上失败，报错信息与"漏分号"没有明显关系；
            # 在这里指出来，读的人才知道真正该改哪一行。
            for name in ("add_header", "set", "proxy_set_header",
                         "proxy_hide_header", "try_files"):
                if pending.count(name + " ") > 1:
                    failures.append(Failure(
                        "nginx syntax",
                        f"第 {pending_start} 行起的语句里 {name} 出现 "
                        f"{pending.count(name + ' ')} 次（上一条指令可能漏了 ;）："
                        f"{pending[:90]}"))
                    break
            pending = ""
            continue
        if terminated and in_quote:
            # 引号在此行仍未闭合：可能是跨行字符串（本项目没有），
            # 报出来而不是静默接受。
            failures.append(Failure(
                "nginx syntax", f"第 {lineno} 行结束时引号未闭合：{code[:70]}"))
            in_quote = False
            pending = ""
            continue
    if pending and not in_quote:
        failures.append(Failure(
            "nginx syntax",
            f"第 {pending_start} 行起的指令没有以 ; 或 {{ 结束：{pending[:70]}"))
    if in_quote:
        failures.append(Failure("nginx syntax", "文件结束时引号仍未闭合"))
    if depth != 0:
        failures.append(Failure("nginx syntax", f"括号不平衡，剩余深度 {depth}"))

    # set 指令必须出现在 server 块内（http 级 set 的变量在 location 中不可用于
    # add_header 的自定义值——实际是不允许的，nginx 会报 unknown variable）。
    server_start = text.find("server {")
    if server_start < 0:
        failures.append(Failure("nginx syntax", "没有 server 块"))
    else:
        for match in re.finditer(r"^\s*set\s+\$(\w+)", text, re.M):
            if match.start() < server_start:
                failures.append(Failure(
                    "nginx syntax",
                    f"set ${match.group(1)} 出现在 server 块之前"))
    return failures


def check_rendered_csp() -> list[Failure]:
    """模拟 envsubst 渲染，检查渲染后的 CSP 是否仍然合法。

    nginx 官方的 20-envsubst-on-templates.sh 只替换在容器环境里**存在**的
    ${VAR}。因此这里也用"存在的才替换"的语义：未提供的变量保持原样，
    若最终 CSP 里仍残留 `${...}`，说明 compose/Dockerfile 没有该变量，
    容器里会把它替换成**空字符串**——于是 CSP 出现空来源项。
    浏览器对空来源的处理是丢弃整条指令，不是报错，所以这个缺陷在运行期
    几乎不可能被主动发现。
    """
    failures: list[Failure] = []
    text = read("frontend/apps/teacher-web/nginx.conf")
    env = {"MACT_CSP_MEDIA_ORIGIN": "http://localhost:9000"}

    def render(raw: str, provided: dict[str, str]) -> str:
        out = raw
        for key, value in provided.items():
            out = out.replace("${" + key + "}", value)
        return out

    # 只检查**指令行**，不看注释：注释里会引用 `${VAR}` 作为说明文字，
    # 把它当成"未定义变量"是假阳性。
    code_lines = [line for line in text.splitlines() if not line.strip().startswith("#")]
    rendered = render("\n".join(code_lines), env)
    if "${" in rendered:
        leftover = sorted(set(re.findall(r"\$\{([A-Za-z0-9_]+)\}", rendered)))
        failures.append(Failure(
            "envsubst render",
            f"渲染后仍残留未定义变量 {leftover}；容器里它们会变成空字符串，"
            "CSP 因此出现空来源项（浏览器丢弃整条指令，不报错）"))

    csp_defs = re.findall(r'^\s*set\s+\$mact_csp\s+"([^"]*)"', rendered, re.M)
    if len(csp_defs) != 1:
        failures.append(Failure("envsubst render", "渲染后 $mact_csp 定义数量不为 1"))
        return failures
    csp = csp_defs[0]

    # 逐条指令检查空来源项：`img-src 'self'  data:` 这种双空格表示有一个空项。
    for directive in csp.split(";"):
        directive = directive.strip()
        if not directive:
            continue
        parts = directive.split()
        if len(parts) > 1 and any(part == "" for part in parts):
            failures.append(Failure("envsubst render", f"指令含空来源：{directive}"))
    for name in ("img-src", "media-src", "connect-src"):
        match = re.search(name + r"([^;]*)", csp)
        if match and "http://localhost:9000" not in match.group(1):
            failures.append(Failure(
                "envsubst render",
                f"{name} 渲染后不含注入的 MinIO 源（说明变量名拼错或被改名）"))
    return failures


def check_backend_config() -> list[Failure]:
    failures: list[Failure] = []
    text = read("backend/src/main/java/com/macteval/security/SecurityConfig.java")
    for token, why in [
        ("contentSecurityPolicy", "未显式配置 CSP"),
        ("frameOptions", "未显式配置 X-Frame-Options"),
        ("referrerPolicy", "未显式配置 Referrer-Policy"),
        ("permissionsPolicy", "未显式配置 Permissions-Policy"),
        ("contentTypeOptions", "未显式配置 nosniff"),
    ]:
        if token not in text:
            failures.append(Failure("SecurityConfig", f"{why}（{token}）"))
    if "default-src 'none'" not in text:
        failures.append(Failure("SecurityConfig", "API CSP 不是 lockdown 形态（缺 default-src 'none'）"))
    return failures


def check_ws_credentials() -> list[Failure]:
    """WebSocket 凭据三处必须同时不再走查询串。"""
    failures: list[Failure] = []

    socket_raw = read("frontend/apps/teacher-web/src/composables/useClassroomSocket.ts")
    socket = strip_js_comments(socket_raw)
    if re.search(r"/ws/classroom\?lessonId=.*&token=", socket):
        failures.append(Failure("frontend socket", "仍把 token 拼进 WebSocket 查询串"))
    if "&ticket=" not in socket:
        failures.append(Failure("frontend socket", "建连未使用 &ticket="))
    if "ws-ticket" not in socket:
        failures.append(Failure("frontend socket", "没有调用 /auth/ws-ticket"))
    if "fetchWsTicket" not in socket:
        failures.append(Failure("frontend socket", "缺少换票函数（可能是回退到旧写法了）"))

    interceptor = read("backend/src/main/java/com/macteval/config/JwtHandshakeInterceptor.java")
    if "legacy-token-enabled" not in interceptor:
        failures.append(Failure("handshake", "旧 ?token= 路径没有开关"))
    # 检查**实际调用**而不是类名出现：类名会出现在 import、javadoc、构造参数里，
    # 把票据分支删掉之后那些地方全都在，"类名存在"就成了恒真断言。
    # （这个变异正是被自测抓出来的：第一版只搜 "WsTicketStore"，删掉分支仍然全绿。）
    if "ticketStore.consume(ticket)" not in interceptor:
        failures.append(Failure("handshake", "握手没有真正消费票据（缺少 ticketStore.consume）"))
    if "consumed.lessonSessionId()" not in interceptor:
        failures.append(Failure("handshake", "票据未与 lessonId 绑定校验"))

    ws_test = read("scripts/verify/ws_auth.py")
    if "&token=" in ws_test.replace('&ticket=', ''):
        # 允许出现"断言旧写法被拒绝"的那一处，但不应再有正向使用。
        usages = [line for line in ws_test.splitlines()
                  if "&token=" in line and "401" not in line and "legacy" not in line.lower()]
        if usages:
            failures.append(Failure("ws_auth.py", f"仍在正向使用 ?token=（{len(usages)} 处）"))

    return failures


def check_dockerfile_template() -> list[Failure]:
    failures: list[Failure] = []
    text = read("frontend/apps/teacher-web/Dockerfile")
    # 必须检查 COPY 的**目标路径**，而不是整篇文本里出现过 /etc/nginx/templates/：
    # 注释里也会提到这个路径，用子串搜索会把"改了注释"当成"配好了"。
    if not re.search(r"^COPY\s+\S+\s+/etc/nginx/templates/\S+\.template\s*$",
                     text, re.M):
        failures.append(Failure(
            "Dockerfile",
            "nginx.conf 未 COPY 到 /etc/nginx/templates/*.template"
            "（CSP 里的 MinIO 源无法注入）"))
    if "MACT_CSP_MEDIA_ORIGIN" not in text:
        failures.append(Failure("Dockerfile", "缺少 MACT_CSP_MEDIA_ORIGIN 兜底默认值"))
    return failures


def run_all() -> list[Failure]:
    return (check_nginx() + check_nginx_structure() + check_rendered_csp()
            + check_backend_config() + check_ws_credentials()
            + check_dockerfile_template())


# ---------------------------------------------------------------------------
# 自测：把配置改坏，断言检查器会红
# ---------------------------------------------------------------------------
def self_test() -> int:
    global ROOT
    import tempfile

    samples = {
        "nginx missing header": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace(
                'add_header X-Frame-Options "DENY" always;\n', "", 1),
        ),
        "nginx header without always": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace(
                'add_header X-Frame-Options "DENY" always;',
                'add_header X-Frame-Options "DENY";', 1),
        ),
        "csp wildcard": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace("img-src 'self' data: blob:", "img-src *", 1),
        ),
        "csp unsafe-inline script": (
            "frontend/apps/teacher-web/nginx.conf",
            # 必须替换**所有**出现处：该片段在注释里也有一份（注释正是解释
            # 为什么不加 unsafe-inline 的地方），只替换第一处改的是注释。
            lambda t: t.replace("script-src 'self' 'unsafe-eval'",
                                "script-src 'self' 'unsafe-eval' 'unsafe-inline'"),
        ),
        "log format records query": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace('"$request_method $uri $server_protocol"',
                                '"$request"', 1),
        ),
        "ws access log re-enabled": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace("        access_log off;\n", "", 1),
        ),
        # 变异：把某处的变量引用换回字面量副本。这正是"改一处忘两处"的
        # 具体形态，检查器必须拒绝。
        "nginx duplicates csp literal": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace(
                "        add_header Content-Security-Policy $mact_csp always;\n",
                '        add_header Content-Security-Policy "default-src \'self\'" always;\n',
                1),
        ),
        # 变异：在 /api/ 里 hide 后端 CSP。这会让 JSON 只剩页面策略（更弱），
        # 是本轮刻意改掉的那个决定。
        "api hides backend csp": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace(
                "    location /api/ {\n",
                "    location /api/ {\n        proxy_hide_header Content-Security-Policy;\n",
                1),
        ),
        # 变异：变量名拼错（${MACT_CSP_MEDIA_ORIGIN} -> ${MACT_CSP_MEDIA_ORIGIN_TYPO}）。
        # 渲染后该处会变成空串，CSP 出现空来源项。
        "envsubst typo leaves empty source": (
            "frontend/apps/teacher-web/nginx.conf",
            # 替换**所有**出现处：第一次出现是在注释里（说明文字），
            # 只替换一处等于改的是注释，指令行没动。
            lambda t: t.replace("${MACT_CSP_MEDIA_ORIGIN}", "${MACT_CSP_MEDIA_ORIGIN_TYPO}"),
        ),
        # 变异：漏掉一条 add_header 的分号。
        "nginx missing semicolon": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace('add_header X-Frame-Options "DENY" always;',
                                'add_header X-Frame-Options "DENY" always', 1),
        ),
        # 变异：多一个闭合括号。
        "nginx unbalanced brace": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace("    location /assets/ {",
                                "    location /assets/ {\n    }", 1),
        ),
        # 变异：把 /ws/ 的 error_log 收紧去掉（回退到"只关 access_log"）。
        "ws error_log not tightened": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace("        error_log /var/log/nginx/ws-error.log crit;\n",
                                "", 1),
        ),
        # 变异：定义两份 CSP 变量（第二份与第一份不同）。
        "nginx second csp definition": (
            "frontend/apps/teacher-web/nginx.conf",
            lambda t: t.replace(
                "    add_header Content-Security-Policy $mact_csp always;",
                '    set $mact_csp "default-src \'self\'";\n'
                "    add_header Content-Security-Policy $mact_csp always;",
                1),
        ),
        "frontend reverts to token query": (
            "frontend/apps/teacher-web/src/composables/useClassroomSocket.ts",
            lambda t: t.replace("'&ticket=' + encodeURIComponent(ticket),",
                                "'&token=' + encodeURIComponent(ticket),", 1),
        ),
        "handshake drops ticket branch": (
            "backend/src/main/java/com/macteval/config/JwtHandshakeInterceptor.java",
            # 删掉整个票据分支：模拟"有人后来又改回只用 JWT"。
            lambda t: t.replace("WsTicketStore.Ticket consumed = ticketStore.consume(ticket);",
                                "Object consumed = null;", 1),
        ),
        "security config loses csp": (
            "backend/src/main/java/com/macteval/security/SecurityConfig.java",
            lambda t: t.replace("contentSecurityPolicy", "miscHeader", 1),
        ),
        "dockerfile stops using template dir": (
            "frontend/apps/teacher-web/Dockerfile",
            lambda t: t.replace("/etc/nginx/templates/default.conf.template",
                                "/etc/nginx/conf.d/default.conf", 1),
        ),
    }

    failures: list[str] = []
    originals: dict[str, str] = {}
    digests: dict[str, str] = {}
    try:
        # 每个变异都在真实文件上做，跑完立刻还原——检查器读的是仓库路径，
        # 换成临时目录就测不到真实的相对路径解析。
        for name, (rel, mutate) in samples.items():
            path = ROOT / rel
            if rel not in originals:
                originals[rel] = path.read_text(encoding="utf-8")
                digests[rel] = hashlib.sha256(originals[rel].encode("utf-8")).hexdigest()
            mutated = mutate(originals[rel])
            if mutated == originals[rel]:
                failures.append(f"{name}: 变异没有改变文件内容（替换目标已不存在？）")
                continue
            path.write_text(mutated, encoding="utf-8")
            try:
                found = run_all()
            finally:
                path.write_text(originals[rel], encoding="utf-8")
            if not found:
                failures.append(f"{name}: 变异后检查器仍然全绿")
        # 还原后必须重新全绿
        remaining = run_all()
        if remaining:
            failures.append("还原后仍有失败：" + "; ".join(
                f"{f.check}: {f.detail}" for f in remaining))
    finally:
        for rel, content in originals.items():
            (ROOT / rel).write_text(content, encoding="utf-8")
        # 自测会临时改写真实文件。还原是否真的到位必须逐字节验证——
        # "检查器又绿了"不能证明文件回到了原样（可能改到了检查器不看的地方）。
        for rel, expected in digests.items():
            actual = hashlib.sha256(
                (ROOT / rel).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            if actual != expected:
                failures.append(f"{rel}: 自测后未逐字节还原（sha256 不一致）")

    print(f"==> 安全头/凭据检查器自测：{len(samples)} 个变异")
    for f in failures:
        print(f"  FAIL {f}")
    if failures:
        return 1
    print(f"  全部 {len(samples)} 个变异被检出，还原后全绿")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    failures = run_all()
    print("==> 安全响应头与 WebSocket 凭据（静态契约）")
    for f in failures:
        print(f"  FAIL {f.check}: {f.detail}")
    if failures:
        print(f"\n{len(failures)} 项不通过", file=sys.stderr)
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
