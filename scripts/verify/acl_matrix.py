#!/usr/bin/env python
"""HTTP 权限矩阵验收（BE01 / FE01 服务端契约）。

用法（需已启动隔离后端）：

    python scripts/verify/acl_matrix.py --base http://localhost:18001/api

脚本只创建带唯一后缀的课程/课堂，不改动既有业务数据；退出码非 0 表示有断言失败。

管理员与学生账号的来源（2026-10-29 修）：原先写死 ``admin/admin123`` 与
``student/123456``，那只在 ``MACT_DEMO_ENABLED=true`` 的 demo 种子库上成立。
隔离 rig 的库里没有这两个账号，脚本会在第一行直接 traceback；而报错对调用者
看来像"权限回归"，其实是造号契约不成立。现在与 ``ws_auth.py`` 统一：
优先读 ``MACT_E2E_ADMIN_*`` / ``MACT_E2E_STUDENT_*``（由 ``rig_accounts.py``
生成），回退到 demo 默认值，两者都不可用时**显式失败并说明要设哪个变量**，
不静默跳过——这些断言里有一半是"学生/管理员视角"的越权检查，跳过等于放弃目标。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, token: str | None = None, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return e.code, payload

    def login(self, username: str, password: str) -> dict:
        status, body = self.call("POST", "/auth/login", body={"username": username, "password": password})
        # 用 SystemExit 而不是 assert：traceback 会被读成"权限回归"，
        # 而真正的原因往往是账号不存在/口令过期。日志里不打印口令。
        if status != 200:
            raise SystemExit(f"登录失败 {username!r}: HTTP {status} {body}")
        return body["data"]

    def register(self, username: str, password: str, name: str) -> dict:
        status, body = self.call("POST", "/auth/register", body={
            "username": username, "password": password, "name": name, "role": "teacher",
        })
        assert status == 200, f"register {username} failed: {status} {body}"
        return body["data"]


class Checks:
    def __init__(self):
        self.failures: list[str] = []
        self.passed = 0

    def expect(self, name: str, actual: int, expected: int):
        if actual == expected:
            self.passed += 1
            print(f"  PASS {name}: HTTP {actual}")
        else:
            self.failures.append(f"{name}: expected {expected}, got {actual}")
            print(f"  FAIL {name}: expected HTTP {expected}, got {actual}")


def _account(user_var: str, pass_var: str, demo_user: str, demo_pass: str) -> tuple[str, str]:
    """取账号：优先环境变量（rig_accounts.py 产出），回退 demo 默认值。

    两者都不可用时**显式失败**而不是跳过：本脚本约一半断言是学生/管理员视角的
    越权检查，跳过它们会让"权限矩阵通过"变成一句空话。
    """
    user = os.environ.get(user_var, "").strip()
    password = os.environ.get(pass_var, "").strip()
    if user and password:
        return user, password
    if demo_user and demo_pass:
        return demo_user, demo_pass
    raise SystemExit(
        f"缺少账号：请设置 {user_var}/{pass_var}"
        f"（先运行 python -I scripts/verify/rig_accounts.py 生成隔离账号）")


def purge_test_accounts(names: list[str]) -> None:
    """删除本脚本注册过的教师账号及其课程/课堂（与 ws_auth.py 同名函数同一动机）。

    本脚本建 2 个教师 + 2 门课程 + 2 个课堂。不清理的话每次运行都在 rig 库里
    堆一份，实测一次遗留 10 个 `acl-teacher-*`。这些账号会污染按教师过滤的
    计数断言——"同一份代码昨天绿今天红"，而原因不是代码变了。

    `users` 被三个 RESTRICT 外键引用（`lesson_sessions.teacher_id`、
    `attendance_records.student_id`、`media_assets.owner_id`），必须自底向上删。
    只按本次生成的用户名精确匹配，不做前缀批量删（避免误伤并发运行的验收）。
    """
    if not names:
        return
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    sql = (
        f"delete from attendance_records where student_id in "
        f"(select id from users where phone in ({quoted}));"
        f"delete from media_assets where owner_id in "
        f"(select id from users where phone in ({quoted}));"
        f"delete from lesson_sessions where teacher_id in "
        f"(select id from users where phone in ({quoted}));"
        f"delete from courses where teacher_id in "
        f"(select id from users where phone in ({quoted}));"
        f"delete from users where phone in ({quoted});"
    )
    try:
        result = subprocess.run(
            ["docker", "exec", "mact-ui-postgres", "psql", "-U", "macteval",
             "-d", "macteval_ui", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"  !! 清理测试账号失败（不影响结论）：{result.stderr.strip()[:200]}",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理测试账号异常：{exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:18001/api")
    args = parser.parse_args()

    api = Api(args.base)
    checks = Checks()
    suffix = str(int(time.time() * 1000))[-8:]

    admin = api.login(*_account("MACT_E2E_ADMIN_USER", "MACT_E2E_ADMIN_PASS", "admin", "admin123"))
    student = api.login(*_account("MACT_E2E_STUDENT_USER", "MACT_E2E_STUDENT_PASS",
                                  "student", "123456"))
    teacher_a = api.register(f"acl-teacher-a-{suffix}", "secret123", "教师A")
    teacher_b = api.register(f"acl-teacher-b-{suffix}", "secret123", "教师B")
    created = [f"acl-teacher-a-{suffix}", f"acl-teacher-b-{suffix}"]
    print(f"accounts ready: A={teacher_a['userId']} B={teacher_b['userId']}")

    # ---------- refresh token rotation contract ----------
    first_refresh = teacher_a["refreshToken"]
    status, body = api.call("POST", "/auth/refresh", body={"refreshToken": first_refresh})
    checks.expect("refresh token first use", status, 200)
    rotated = body["data"]["refreshToken"]
    assert rotated != first_refresh
    status, _ = api.call("POST", "/auth/refresh", body={"refreshToken": first_refresh})
    checks.expect("consumed refresh token replay rejected", status, 401)
    # use the rotated token's access token
    status, refresh_body = api.call("POST", "/auth/refresh", body={"refreshToken": rotated})
    checks.expect("rotated refresh token second use", status, 200)
    token_a = refresh_body["data"]["token"]
    token_b = teacher_b["token"]

    # ---------- resources ----------
    status, body = api.call("POST", "/classes", token=admin["token"], body={
        "name": f"ACL班-{suffix}", "grade": "测试", "department": "测试",
    })
    checks.expect("admin creates class", status, 200)
    class_id = body["data"]["id"]

    status, body = api.call("POST", "/courses", token=token_a, body={
        "name": f"课程A-{suffix}", "code": f"A{suffix}",
    })
    checks.expect("teacher A creates own course", status, 200)
    course_a = body["data"]["id"]

    status, body = api.call("POST", "/courses", token=token_b, body={
        "name": f"课程B-{suffix}", "code": f"B{suffix}",
    })
    checks.expect("teacher B creates own course", status, 200)
    course_b = body["data"]["id"]

    status, body = api.call("POST", "/sessions", token=token_a, body={
        "courseId": course_a, "classId": "class-001", "title": f"课堂A-{suffix}",
    })
    checks.expect("teacher A creates session in own course", status, 200)
    session_a = body["data"]["id"]

    status, body = api.call("POST", "/sessions", token=token_b, body={
        "courseId": course_b, "classId": class_id, "title": f"课堂B-{suffix}",
    })
    checks.expect("teacher B creates session in own course", status, 200)
    session_b = body["data"]["id"]

    # ---------- object-level authorization ----------
    status, _ = api.call("GET", f"/courses/{course_b}", token=token_a)
    checks.expect("teacher A cannot read teacher B course", status, 403)

    status, _ = api.call("GET", f"/courses/{course_b}", token=student["token"])
    checks.expect("student cannot read unenrolled course", status, 403)

    status, _ = api.call("GET", f"/sessions?courseId={course_b}", token=token_a)
    checks.expect("teacher A cannot list sessions by teacher B courseId", status, 403)

    status, _ = api.call("GET", f"/sessions/{session_b}", token=token_a)
    checks.expect("teacher A cannot read teacher B session", status, 403)

    status, _ = api.call("POST", "/sessions", token=token_a, body={
        "courseId": course_b, "classId": "class-001", "title": "越权课堂",
    })
    checks.expect("teacher A cannot create session in teacher B course", status, 403)

    status, _ = api.call("POST", "/courses", token=student["token"], body={"name": "学生课程"})
    checks.expect("student cannot create course", status, 403)

    status, _ = api.call("POST", "/classes", token=token_a, body={"name": "教师建班"})
    checks.expect("teacher cannot create class", status, 403)

    status, _ = api.call("GET", "/jobs", token=student["token"])
    checks.expect("student cannot list analysis jobs", status, 403)

    status, _ = api.call("GET", "/classroom/students", token=token_a)
    checks.expect("teacher must specify class for roster", status, 400)

    status, _ = api.call("GET", f"/classroom/students?classId={class_id}", token=token_a)
    checks.expect("teacher cannot read other teacher class roster", status, 403)

    # ---------- positive scope ----------
    status, _ = api.call("GET", f"/courses/{course_a}", token=student["token"])
    checks.expect("student can read course with own-class session", status, 200)

    status, _ = api.call("GET", f"/sessions/{session_a}", token=student["token"])
    checks.expect("student can read own-class session", status, 200)

    status, body = api.call("GET", "/jobs", token=token_a)
    checks.expect("teacher A lists own jobs", status, 200)

    status, body = api.call("GET", f"/courses?teacherId={teacher_b['userId']}", token=admin["token"])
    checks.expect("admin reads courses by teacherId", status, 200)
    assert any(c["id"] == course_b for c in body["data"]), "admin scoped list missing course B"

    status, _ = api.call("GET", "/courses")
    checks.expect("anonymous rejected", status, 401)

    print(f"\n{checks.passed} checks passed, {len(checks.failures)} failed")
    if checks.failures:
        for failure in checks.failures:
            print(" -", failure)
    purge_test_accounts(created)
    return 1 if checks.failures else 0


if __name__ == "__main__":
    sys.exit(main())
