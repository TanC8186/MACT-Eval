#!/usr/bin/env python
"""双密钥 JWT 轮换：**真实 Spring Boot 进程**验证（TASK A / 任务书 §B4.3）。

    python -I scripts/verify/verify_jwt_rotation_live.py --json <out.json>

## 为什么需要这个脚本

`JwtTokenProviderMultiKeyTest` 用 `ApplicationContextRunner` 断言配置绑定，但
`docs/reviews/2026-10-29-closure-implementation-review.md` §4 把「Spring Boot 整文件
加载时 `@Value` 的解析」列为未验证。单测证明的是"给定这两个属性，行为是这样"；
它没有证明"用 `MACT_JWT_SECRETS` 环境变量真的能把应用起起来并认旧令牌"。

本脚本起**真实 jar**（独立端口、独立进程），分三个阶段：

  A. 只配 key1 → 登录拿令牌 T1 → 停进程
  B. 配 key1+key2（key2 在首位）→ 重启 → 用 T1 访问受保护接口
     → **必须仍为 200**（这就是"在线用户不掉线"的全部含义）
  C. 再起一个进程，`retired-at` 设成过去时刻 → **必须启动失败**，且错误文本点名
     要删旧键（而不是笼统的 "application failed to start"）

## 边界

* 独立端口（默认 18009），不碰 8000 上跑着的后端。
* 连的是隔离 rig 的库/Redis/MinIO，只读、只建一个带唯一后缀的教师账号。
* 每个阶段结束都杀进程；失败也杀（`try/finally`）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JAR = os.path.join(ROOT, "backend", "target", "mact-eval-backend-1.0.0.jar")

#: 两把 ≥32 字节的测试密钥。它们是**本脚本当次生成的一次性值**，不写进任何文件。
KEY_1 = "rotKey1-" + uuid.uuid4().hex
KEY_2 = "rotKey2-" + uuid.uuid4().hex

#: 隔离 rig 的**一次性本地凭据**，与 `browser_rig.py` / `ui_rig_fault_drill.py`
#: 里同一组值同源。它们不是生产密钥，也不可能是：
#:   * 数据库/Redis/MinIO 只绑 127.0.0.1 的 rig 端口（15534/16380/19100）；
#:   * rig 容器名被 `assert_isolated()` 限制为 `mact-ui-*`；
#:   * 这些值在仓库里是**明文的开发默认值**，生产部署必须用 `MACT_*` 环境变量覆盖
#:     （`application.yml` 的 `${MACT_MINIO_SECRET_KEY:macteval_secret}` 就是这个模式）。
#: 写在同一处而不是散落各文件，是为了让"哪些是测试值"一眼可辨。
BASE_ENV = {
    "MACT_DB_URL": "jdbc:postgresql://127.0.0.1:15534/macteval_ui",
    "MACT_DB_USERNAME": "macteval",
    "MACT_DB_PASSWORD": "macteval",
    "MACT_REDIS_HOST": "127.0.0.1",
    "MACT_REDIS_PORT": "16380",
    "MACT_MINIO_ENDPOINT": "http://127.0.0.1:19100",
    "MACT_MINIO_ACCESS_KEY": "uirig",
    "MACT_MINIO_SECRET_KEY": "uirig_secret",
    "MACT_SERVICE_TOKEN": "ui-rig-service-token-2026-10-26",
}

LOG_DIR = os.path.join(ROOT, ".cache", "tmp", "jwt-rotation-live")


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def expect(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  PASS {name}" + (f" — {detail}" if detail else ""))
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"  FAIL {name} — {detail}")


def http(base: str, method: str, path: str, token: str | None = None,
         body: dict | None = None, timeout: int = 20) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, (json.loads(raw) if raw.strip() else {})
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


class Backend:
    """起一个真实 jar 进程，等它就绪或按预期失败。"""

    def __init__(self, port: int, extra_env: dict, log_name: str) -> None:
        self.port = port
        self.extra_env = extra_env
        self.log_path = os.path.join(LOG_DIR, log_name)
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/api"

    def start(self) -> None:
        os.makedirs(LOG_DIR, exist_ok=True)
        env = dict(os.environ)
        env.update(BASE_ENV)
        env.update(self.extra_env)
        log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            ["java", "-jar", JAR, f"--server.port={self.port}"],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)

    def wait_ready(self, seconds: int = 120) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
        return False

    def wait_exit(self, seconds: int = 120) -> int | None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            code = self.proc.poll() if self.proc is not None else None
            if code is not None:
                return code
            time.sleep(1)
        return None

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=15)

    def log_tail(self, lines: int = 40) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:])
        except OSError:
            return ""


def port_free(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.3)
    try:
        sock.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18009)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    if not os.path.exists(JAR):
        print(f"!! 缺少 jar：{JAR}（先 mvn -f backend/pom.xml package -DskipTests）",
              file=sys.stderr)
        return 2
    if not port_free(args.port):
        print(f"!! 端口 {args.port} 被占用", file=sys.stderr)
        return 2

    checks = Checks()
    suffix = uuid.uuid4().hex[:8]
    username = f"jwtrot-{suffix}"
    password = "JwtRot-Test-Pw-9"

    phase_a = Backend(args.port, {"MACT_JWT_SECRETS": KEY_1}, "phase-a.log")
    phase_b = Backend(args.port, {"MACT_JWT_SECRETS": f"{KEY_2},{KEY_1}"}, "phase-b.log")
    phase_c = Backend(args.port, {"MACT_JWT_SECRETS": f"{KEY_2},{KEY_1}",
                                  "MACT_JWT_RETIRED_AT": "2020-01-01T00:00:00Z"}, "phase-c.log")

    try:
        # ---------- A：只配 key1 ----------
        print("==> A. 只配 MACT_JWT_SECRETS=<key1>")
        phase_a.start()
        if not phase_a.wait_ready():
            print("!! A 阶段未能启动：\n" + phase_a.log_tail(), file=sys.stderr)
            return 2
        checks.expect("单值 secrets 能启动", True, f"port={args.port}")
        st, body = http(phase_a.base, "POST", "/auth/register", body={
            "username": username, "password": password,
            "name": "轮换验证教师", "role": "teacher"})
        checks.expect("注册教师 → 200", st == 200, f"HTTP {st}")
        if st != 200:
            print(body, file=sys.stderr)
            return 2
        token_t1 = body["data"]["token"]
        st, _ = http(phase_a.base, "GET", "/reports/page", token=token_t1)
        checks.expect("T1 在原进程可用", st == 200, f"HTTP {st}")
        phase_a.stop()

        # ---------- B：key2 在首位，key1 在第二位 ----------
        print("==> B. 改为 MACT_JWT_SECRETS=<key2>,<key1> 后重启")
        phase_b.start()
        if not phase_b.wait_ready():
            print("!! B 阶段未能启动：\n" + phase_b.log_tail(), file=sys.stderr)
            return 2
        st, _ = http(phase_b.base, "GET", "/reports/page", token=token_t1)
        checks.expect("**旧令牌 T1 在新进程仍被接受**（零停机轮换的核心）", st == 200,
                      f"HTTP {st}")
        st, body = http(phase_b.base, "POST", "/auth/login",
                        body={"username": username, "password": password})
        checks.expect("用新键签发的登录仍成功", st == 200, f"HTTP {st}")
        token_t2 = body["data"]["token"] if st == 200 else None
        if token_t2:
            checks.expect("T2 与 T1 不同（确实用了新键签发）", token_t2 != token_t1,
                          "两个 token 不相等")
            st, _ = http(phase_b.base, "GET", "/reports/page", token=token_t2)
            checks.expect("T2 在本进程可用", st == 200, f"HTTP {st}")
        # 用一把完全没配过的密钥签的令牌必须被拒
        forged = key_not_configured_token()
        st, _ = http(phase_b.base, "GET", "/reports/page", token=forged)
        checks.expect("未配置密钥签的令牌被拒（401）", st == 401, f"HTTP {st}")
        phase_b.stop()

        # ---------- C：retired-at 已过期 ----------
        print("==> C. MACT_JWT_RETIRED_AT=2020-01-01（已过期）")
        phase_c.start()
        exit_code = phase_c.wait_exit(180)
        checks.expect("启动失败（非零退出）", exit_code not in (None, 0),
                      f"exit={exit_code}")
        log = phase_c.log_tail(200)
        checks.expect("错误文本点名 mact.jwt.retired-at", "retired-at" in log,
                      "日志含 retired-at")
        checks.expect("错误文本要求删除旧键", "Delete the old key" in log or "delete" in log.lower(),
                      "日志给出可执行动作")
        checks.expect("**没有**降级为正常启动", exit_code != 0, f"exit={exit_code}")
        phase_c.stop()
    finally:
        for backend in (phase_a, phase_b, phase_c):
            backend.stop()
        _cleanup_account(username)

    total = checks.passed + len(checks.failed)
    print(f"\n{checks.passed}/{total} checks passed, {len(checks.failed)} failed")
    for item in checks.failed:
        print(" -", item)

    if args.json:
        payload = {
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "taskbookRef": "§B4.3 双密钥过渡 / TASK A",
            "why": "单测用 ApplicationContextRunner；本脚本用真实 jar 进程验证"
                   "环境变量绑定、旧令牌跨重启存活、retired-at 过期启动失败",
            "phases": {
                "A": "只配 key1，取令牌 T1",
                "B": "key2 在首位 + key1 在第二位，T1 仍 200",
                "C": "retired-at 在过去 → 拒绝启动",
            },
            "checks": {"passed": checks.passed, "total": total, "failures": checks.failed},
            "passed": not checks.failed,
            "logs": LOG_DIR,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")

    return 1 if checks.failed else 0


def _cleanup_account(username: str) -> None:
    """删掉本脚本注册的教师账号。

    首版没有这步，跑完在 rig 库里留了一个 `jwtrot-*` 教师（已手工清掉）。
    一个每次运行都留一个账号的验收脚本，跑几十次就会把一个"干净基线"变成
    "几百个垃圾账号"，也会污染后续 listReports 之类的计数断言。
    只按本次生成的用户名精确删除；删除失败只打印，不影响结论。
    """
    try:
        subprocess.run(
            ["docker", "exec", "mact-ui-postgres", "psql", "-U", "macteval",
             "-d", "macteval_ui", "-v", "ON_ERROR_STOP=1", "-c",
             f"delete from users where phone='{username}'"],
            capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"!! 清理测试账号失败：{exc}；残留 username={username}", file=sys.stderr)


def key_not_configured_token() -> str:
    """用一把从未配置过的密钥签一个结构合法但签名对不上的令牌。"""
    import base64
    import hashlib
    import hmac

    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    now = int(time.time())
    claims = json.dumps({"sub": "nobody", "iat": now, "exp": now + 600}).encode()
    payload = base64.urlsafe_b64encode(claims).rstrip(b"=").decode()
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(b"never-configured-key-" + uuid.uuid4().hex.encode(),
                   signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


if __name__ == "__main__":
    sys.exit(main())
