#!/usr/bin/env python
"""在隔离 rig 上准备 U05 需要的"其他身份"账号（学生、另一位教师、管理员）。

为什么需要这个脚本而不是走 ``/auth/register``：
``AuthService.register`` 只接受 ``role=teacher``（见 AuthService 第 119-120 行，
非 teacher 直接拒绝），所以浏览器验收脚本无法自行创建学生账号。而 U05 的核心
验收目标恰恰是"学生访问行为证据必须 403"——没有学生账号，这一项只能记
``requiredSkips``，用例判 incomplete，退出码非零。

三种错误做法与它们的后果：
* 用 ``skip`` 把它从结论里抹掉 —— 等于放弃本轮验收目标；
* 让操作者手工建号 —— 证据不可复现，换台机器就重来；
* 把学生口令写进仓库 —— 凭据泄漏。

本脚本走第四种：**只在隔离 rig 的测试库里**用 bcrypt 直接写入/更新三个账号，
口令是每次运行随机生成的一次性值，只打印到 stdout 供当次验收使用，不落盘。

安全边界（与 ``verify_outbox_repair.py`` / ``browser_rig.py`` 一致）：
* 容器名必须正好是 ``mact-ui-postgres``；
* 库名必须正好是 ``macteval_ui``；
* 拒绝 ``macteval`` / ``postgres`` 等用户库名；
* 只 INSERT/UPDATE 这三个固定 id 的行，不删任何数据、不动历史报告。

用法：
    python -I scripts/verify/rig_accounts.py [--json]
输出（JSON）：
    {"student": {"username": ..., "password": ...}, "otherTeacher": {...}, "admin": {...}}
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
import uuid

CONTAINER = "mact-ui-postgres"
DB_USER = "macteval"
DB_NAME = "macteval_ui"
FORBIDDEN_DATABASES = {"macteval", "postgres", "template0", "template1"}

# Windows 控制台默认是 cp936，而本脚本的输出会被验收脚本用 UTF-8 解析
# （``--json``）。不强制重设编码时，中文账号名会被写成 GBK 字节，
# 下游 ``json.loads`` 直接 UnicodeDecodeError。这里在导入后立刻固定为 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 非 TTY / 已重定向时忽略
        pass

#: 三个专用账号的固定标识。固定 id 让脚本幂等：重复运行只更新口令。
#: 登录名契约（``AuthService.login`` 第 91-99 行）：先按 ``users.phone`` 查，
#: 找不到再按 ``students.student_id``（即学号）查。因此教师/管理员必须把
#: 登录名写进 **phone**，只写 teacher_id/admin_id 是登录不上的——这曾经让
#: "另一教师 403" 这条必检断言因 401 失败，看起来像权限回归，其实是造号脚本的错。
ACCOUNTS = {
    "student": {"id": "student-e2e-001", "name": "界面验收学生", "role": "student",
                "student_id": "STU-E2E-001", "class_id": "class-001"},
    "otherTeacher": {"id": "teacher-e2e-other", "name": "界面验收教师B", "role": "teacher",
                     "teacher_id": "TCH-E2E-OTHER", "phone": "13900002099"},
    "admin": {"id": "admin-e2e-001", "name": "界面验收管理员", "role": "admin",
              "admin_id": "ADM-E2E-001", "phone": "13900002098"},
}


def fail(msg: str) -> None:
    print(f"FAIL  {msg}", file=sys.stderr)
    raise SystemExit(1)


def psql(sql: str, database: str = DB_NAME) -> str:
    """在 rig 容器里执行一条 SQL，返回 -A -t 输出。"""
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-d", database,
         "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        fail(f"psql 失败：{result.stderr.strip()[:400]}")
    return result.stdout.strip()


def guard() -> None:
    """隔离守卫：容器名、库名、真实存在性，全部匹配才继续。"""
    if DB_NAME in FORBIDDEN_DATABASES or not DB_NAME.startswith("macteval_"):
        fail(f"拒绝操作数据库 {DB_NAME}")
    if not CONTAINER.startswith("mact-ui-"):
        fail(f"拒绝操作容器 {CONTAINER}")
    inspect = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", CONTAINER],
                             capture_output=True, text=True, timeout=30)
    if inspect.returncode != 0 or inspect.stdout.strip() != "true":
        fail(f"{CONTAINER} 未运行；先执行 python scripts/verify/browser_rig.py start")

    existing = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
         "-A", "-t", "-c",
         "select current_database() || '|' || (select count(*) from users)"],
        capture_output=True, text=True, timeout=30)
    if existing.returncode != 0:
        fail(f"无法读取 {DB_NAME}：{existing.stderr.strip()[:300]}")
    print(f"target: container={CONTAINER} database={DB_NAME} "
          f"users={existing.stdout.strip().split('|')[-1]}", file=sys.stderr)


def bcrypt_hash(password: str) -> str:
    """后端用 BCryptPasswordEncoder（$2a$10$ 格式），这里产出同格式的哈希。"""
    try:
        import bcrypt
    except Exception as exc:  # noqa: BLE001
        fail(f"缺少 bcrypt 依赖：{exc}；用 {sys.executable} -m pip install bcrypt 安装")

    digest = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10))
    return digest.decode("utf-8")


def upsert(spec: dict, password: str) -> None:
    hashed = bcrypt_hash(password)
    # 单引号转义：口令是随机 base64url，理论不含引号，但仍按 SQL 规则转义一次。
    safe_hash = hashed.replace("'", "''")
    safe_name = spec["name"].replace("'", "''")
    columns = ["id", "name", "password", "role", "created_at", "updated_at"]
    values = [f"'{spec['id']}'", f"'{safe_name}'", f"'{safe_hash}'", f"'{spec['role']}'",
              "NOW()", "NOW()"]
    for column in ("student_id", "teacher_id", "admin_id", "class_id", "phone"):
        if spec.get(column):
            columns.append(column)
            values.append(f"'{spec[column]}'")
    # must_change_password 等列有默认值；只覆盖我们显式给出的列，其余靠默认。
    assign = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in ("id", "created_at"))
    sql = (f"INSERT INTO users ({', '.join(columns)}) VALUES ({', '.join(values)}) "
           f"ON CONFLICT (id) DO UPDATE SET {assign}")
    psql(sql)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="只输出 JSON（供脚本消费）")
    args = parser.parse_args(argv)

    guard()
    payload: dict[str, dict[str, str]] = {}
    for key, spec in ACCOUNTS.items():
        # 一次性口令：不写入任何文件，只在本次运行的输出里出现。
        password = "UiRig-" + secrets.token_urlsafe(9)
        upsert(spec, password)
        # 登录名：学生用学号，教师/管理员用 phone（见 ACCOUNTS 上的注释）。
        username = spec.get("phone") or spec.get("student_id") or spec.get("name")
        payload[key] = {"id": spec["id"], "username": username, "password": password,
                        "role": spec["role"], "name": spec["name"]}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"已准备 rig 账号（数据库 {DB_NAME}，容器 {CONTAINER}）：")
        for key, info in payload.items():
            print(f"  {key:<13} username={info['username']}  password={info['password']}")
        print("\n导出给浏览器验收：")
        print(f"  export MACT_E2E_STUDENT_USER={payload['student']['username']}")
        print(f"  export MACT_E2E_STUDENT_PASS={payload['student']['password']}")
        print(f"  export MACT_E2E_OTHER_TEACHER_USER={payload['otherTeacher']['username']}")
        print(f"  export MACT_E2E_OTHER_TEACHER_PASS={payload['otherTeacher']['password']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
