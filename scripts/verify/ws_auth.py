#!/usr/bin/env python
"""WebSocket 一次性票据验收（SEC-02，2026-10-28）。

前置：隔离后端已启动，并安装 websockets。

    python scripts/verify/ws_auth.py \
        --base http://localhost:8000/api --ws ws://localhost:8000/api/ws/classroom

覆盖的是**票据**而不是旧查询串令牌。旧脚本断言的"跨课堂连接先建立再以 1008
关闭"已不再成立——票据在签发阶段就被拒绝，这是更强的保证，因此断言也随之改变。

断言：
  1. 教师 A 为自己的课堂换票 → 握手成功；
  2. 同班学生换票 → 握手成功；
  3. 匿名握手（无票据）→ 401；
  4. 教师 B 为教师 A 的课堂换票 → /auth/ws-ticket 403（在签发阶段拒绝）；
  5. 异班学生为教师 A 的课堂换票 → 403；
  6. 拿 A 课堂的票据去连 B 课堂 → 握手 403（票据与课堂绑定）；
  7. 同一票据用两次 → 第二次 401（单次消费）；
  8. 伪造票据（32 位十六进制但未签发）→ 401；
  9. 畸形票据（非十六进制）→ 401；
 10. 旧写法 ?token=<JWT> → 401（默认关闭，证明泄漏路径已封）；
 11. **过期票据** → 401（任务书 §B2.6 明列的"过期"情形）。

第 11 项要真的等过 TTL（60 s）才能验，所以只有加 ``--check-expiry`` 时才跑；
不加时它在结果里记成"未执行"并让退出码变成 3（而不是 0）——"过期没验"不能
悄悄算作通过。也可以用 ``--expiry-wait`` 配合一个调短了 TTL 的后端来加速。

任何一条不满足都返回非零。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time

import requests
from websockets.exceptions import ConnectionClosed, InvalidStatus

try:
    import websockets
except ImportError:  # pragma: no cover - 运行环境缺依赖时给出可执行提示
    print("需要 websockets：python -m pip install websockets", file=sys.stderr)
    raise SystemExit(2)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def api(base: str, method: str, path: str, token: str | None = None, body: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, base.rstrip("/") + path, json=body, headers=headers, timeout=15)
    payload = resp.json() if resp.content else {}
    return resp.status_code, payload


def login(base: str, username: str, password: str) -> dict:
    status, body = api(base, "POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"login {username}: {status} {body}"
    return body["data"]


def register(base: str, username: str) -> dict:
    status, body = api(base, "POST", "/auth/register", body={
        "username": username, "password": "secret123", "name": username, "role": "teacher",
    })
    assert status == 200, f"register {username}: {status} {body}"
    return body["data"]


def ticket(base: str, token: str, lesson_id: str) -> tuple[int, str | None, str]:
    """申请票据，返回 (HTTP 状态, 票据或 None, 说明)。"""
    status, body = api(base, "POST", "/auth/ws-ticket", token=token,
                       body={"lessonSessionId": lesson_id})
    if status != 200:
        message = (body.get("message") if isinstance(body, dict) else "") or ""
        return status, None, message
    data = body.get("data") or {}
    value = data.get("ticket")
    ttl = data.get("ttlSeconds")
    if not isinstance(value, str) or len(value) != 32:
        return status, None, f"ticket not 32 hex chars: {value!r}"
    if ttl != 60:
        return status, None, f"unexpected ttlSeconds={ttl}"
    return status, value, ""


async def expect_open(ws_url: str, lesson_id: str, tk: str, name: str, failures: list[str]):
    url = f"{ws_url}?lessonId={lesson_id}&ticket={tk}"
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            await asyncio.sleep(0.3)
            if ws.state.name == "OPEN":
                print(f"  PASS {name}: handshake open")
            else:
                failures.append(f"{name}: state={ws.state}")
                print(f"  FAIL {name}: state={ws.state}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"{name}: {type(e).__name__} {e}")
        print(f"  FAIL {name}: {type(e).__name__} {e}")


async def expect_status(ws_url: str, query: str, expected: int, name: str, failures: list[str]):
    """断言握手被以指定 HTTP 状态拒绝（不建立连接）。"""
    url = f"{ws_url}?{query}"
    try:
        async with websockets.connect(url, open_timeout=10):
            failures.append(f"{name}: unexpectedly connected")
            print(f"  FAIL {name}: unexpectedly connected")
    except InvalidStatus as e:
        if e.response.status_code == expected:
            print(f"  PASS {name}: HTTP {expected}")
        else:
            failures.append(f"{name}: HTTP {e.response.status_code} (expected {expected})")
            print(f"  FAIL {name}: HTTP {e.response.status_code} (expected {expected})")
    except Exception as e:  # noqa: BLE001
        failures.append(f"{name}: {type(e).__name__} {e}")
        print(f"  FAIL {name}: {type(e).__name__} {e}")


def purge_test_accounts(base: str, names: list[str]) -> None:
    """删除本脚本注册过的账号及其课程/课堂。

    为什么必须做：脚本每次都 `register` 两个教师，却从不删。跑几十次就在 rig 库里
    留下几十个 `ws-teacher-*` 账号，各带一门课程与一个课堂（实测一次遗留 34 个）。
    这会让 `listReports` 一类按教师过滤的计数断言随运行次数漂移——
    "同一份代码，昨天绿今天红"，而原因不是代码变了。

    `users` 被三个 RESTRICT 外键引用（`lesson_sessions.teacher_id`、
    `attendance_records.student_id`、`media_assets.owner_id`），所以必须
    自底向上删。只按本次生成的用户名精确匹配，不做前缀批量删（那会误伤别人
    正在跑的并发验收）。
    """
    if not names:
        return
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    sql = (
        f"delete from attendance_records where student_id in "
        f"(select id from users where phone in ({quoted}) or student_id in ({quoted}));"
        f"delete from media_assets where owner_id in "
        f"(select id from users where phone in ({quoted}) or student_id in ({quoted}));"
        f"delete from lesson_sessions where teacher_id in "
        f"(select id from users where phone in ({quoted}));"
        f"delete from courses where teacher_id in "
        f"(select id from users where phone in ({quoted}));"
        f"delete from users where phone in ({quoted}) or student_id in ({quoted});"
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


async def main_async(args) -> int:
    failures: list[str] = []
    base = args.base
    suffix = str(int(time.time() * 1000))[-8:]

    teacher_a = register(base, f"ws-teacher-a-{suffix}")
    teacher_b = register(base, f"ws-teacher-b-{suffix}")
    created = [f"ws-teacher-a-{suffix}", f"ws-teacher-b-{suffix}"]

    # 学生与管理员账号不是注册来的：学生由管理员批量导入（含初始密码），
    # 管理员来自种子数据。硬编码 "student/123456" 只在 demo 种子存在时成立，
    # 而隔离 rig 的库里没有那两个账号。
    # 用 rig_accounts.py 生成的账号，通过环境变量传入；缺失就**显式跳过**
    # 需要该角色的断言，并把跳过写进结果，而不是静默当成通过。
    student_user = os.environ.get("MACT_E2E_STUDENT_USER", "")
    student_pass = os.environ.get("MACT_E2E_STUDENT_PASS", "")
    admin_user = os.environ.get("MACT_E2E_ADMIN_USER", "")
    admin_pass = os.environ.get("MACT_E2E_ADMIN_PASS", "")
    skipped: list[str] = []

    student = login(base, student_user, student_pass) if (student_user and student_pass) else None
    if student is None:
        skipped.append("student-in-lesson-class（需要 MACT_E2E_STUDENT_USER/PASS）")

    admin = login(base, admin_user, admin_pass) if (admin_user and admin_pass) else None
    if admin is None:
        skipped.append("out-of-class-student（需要 MACT_E2E_ADMIN_USER/PASS 建异班）")

    status, body = api(base, "POST", "/courses", token=teacher_a["token"], body={
        "name": f"WS课程-{suffix}", "code": f"WS{suffix}",
    })
    assert status == 200, body
    course_a = body["data"]["id"]

    other_class = "class-001"
    if admin is not None:
        status, body = api(base, "POST", "/classes", token=admin["token"], body={
            "name": f"WS异班-{suffix}", "grade": "测试", "department": "测试",
        })
        assert status == 200, body
        other_class = body["data"]["id"]

    status, body = api(base, "POST", "/sessions", token=teacher_a["token"], body={
        "courseId": course_a, "classId": "class-001", "title": f"WS课堂-{suffix}",
    })
    assert status == 200, body
    lesson_a = body["data"]["id"]

    status, body = api(base, "POST", "/courses", token=teacher_b["token"], body={
        "name": f"WS课B-{suffix}", "code": f"WSB{suffix}",
    })
    assert status == 200, body
    course_b = body["data"]["id"]
    status, body = api(base, "POST", "/sessions", token=teacher_b["token"], body={
        "courseId": course_b, "classId": other_class, "title": f"WS课堂B-{suffix}",
    })
    assert status == 200, body
    lesson_b = body["data"]["id"]

    # 1/2. 有权用户换票后能连
    st, ticket_a, why = ticket(base, teacher_a["token"], lesson_a)
    if st != 200 or not ticket_a:
        failures.append(f"teacher A ticket: {st} {why}")
        print(f"  FAIL teacher A ticket: {st} {why}")
    else:
        print(f"  PASS teacher A ticket issued (ttl=60s)")
        await expect_open(args.ws, lesson_a, ticket_a, "teacher A own lesson", failures)

    if student is not None:
        st, ticket_s, why = ticket(base, student["token"], lesson_a)
        if st != 200 or not ticket_s:
            failures.append(f"student ticket: {st} {why}")
            print(f"  FAIL student ticket: {st} {why}")
        else:
            print("  PASS student ticket issued")
            await expect_open(args.ws, lesson_a, ticket_s, "student in lesson class", failures)

    # 3. 匿名
    await expect_status(args.ws, f"lessonId={lesson_a}", 401, "anonymous handshake", failures)

    # 4/5. 无权用户连票据都换不到（签发阶段拒绝，比连接后 1008 更早）
    st, _, why = ticket(base, teacher_b["token"], lesson_a)
    if st == 403:
        print("  PASS teacher B cross-lesson ticket denied: 403 at issuance")
    else:
        failures.append(f"teacher B cross-lesson ticket: {st} {why}")
        print(f"  FAIL teacher B cross-lesson ticket: {st} {why}")

    if student is not None and other_class != "class-001":
        st, _, why = ticket(base, student["token"], lesson_b)
        if st == 403:
            print("  PASS out-of-class student ticket denied: 403 at issuance")
        else:
            failures.append(f"out-of-class student ticket: {st} {why}")
            print(f"  FAIL out-of-class student ticket: {st} {why}")

    # 6. 票据与课堂绑定：A 课堂的票据连 B 课堂必须 403
    st, ticket_bound, why = ticket(base, teacher_a["token"], lesson_a)
    if st == 200 and ticket_bound:
        await expect_status(args.ws, f"lessonId={lesson_b}&ticket={ticket_bound}", 403,
                            "ticket bound to lesson A used on lesson B", failures)
    else:
        failures.append(f"bound-ticket setup: {st} {why}")
        print(f"  FAIL bound-ticket setup: {st} {why}")

    # 7. 单次消费：同一票据第二次 401
    st, ticket_once, why = ticket(base, teacher_a["token"], lesson_a)
    if st == 200 and ticket_once:
        await expect_open(args.ws, lesson_a, ticket_once, "single-use ticket (first use)", failures)
        await expect_status(args.ws, f"lessonId={lesson_a}&ticket={ticket_once}", 401,
                            "single-use ticket (replay)", failures)
    else:
        failures.append(f"single-use setup: {st} {why}")
        print(f"  FAIL single-use setup: {st} {why}")

    # 8/9. 伪造与畸形
    await expect_status(args.ws, f"lessonId={lesson_a}&ticket={'0' * 32}", 401,
                        "forged ticket (well-formed, never issued)", failures)
    await expect_status(args.ws, f"lessonId={lesson_a}&ticket=not-a-ticket", 401,
                        "malformed ticket", failures)

    # 10. 旧查询串令牌默认关闭
    await expect_status(args.ws, f"lessonId={lesson_a}&token={teacher_a['token']}", 401,
                        "legacy ?token= rejected by default", failures)

    # 11. 过期票据（任务书 §B2.6 的"过期"情形）。
    #     必须真的等过 TTL 才有意义——把票据从 Redis 删掉再试是另一件事
    #     （那是"不存在"，已被 #8 覆盖）。这里等的是自然的 TTL 到期。
    if args.check_expiry:
        st, ticket_exp, why = ticket(base, teacher_a["token"], lesson_a)
        if st == 200 and ticket_exp:
            wait_s = args.expiry_wait
            print(f"  ... 等 {wait_s:.0f}s 让票据自然过期（TTL=60s）")
            await asyncio.sleep(wait_s)
            await expect_status(args.ws, f"lessonId={lesson_a}&ticket={ticket_exp}", 401,
                                "expired ticket (past TTL)", failures)
        else:
            failures.append(f"expired-ticket setup: {st} {why}")
            print(f"  FAIL expired-ticket setup: {st} {why}")
    else:
        skipped.append("expired-ticket（加 --check-expiry 才跑；要等 60s）")

    if skipped:
        print(f"\n{len(skipped)} 跳过（前置账号缺失，不计为通过）：")
        for item in skipped:
            print(" -", item)
    print(f"\n{len(failures)} failed")
    if failures:
        for failure in failures:
            print(" -", failure)
    purge_test_accounts(base, created)
    if failures:
        return 1
    # 有跳过时返回 3：与门禁其余部分一致——"未执行"不能当成"通过"。
    return 3 if skipped else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000/api")
    parser.add_argument("--ws", default="ws://localhost:8000/api/ws/classroom")
    parser.add_argument("--check-expiry", action="store_true",
                        help="额外验证票据自然过期（要等 60s；不加则记未执行、退出码 3）")
    parser.add_argument("--expiry-wait", type=float, default=65.0,
                        help="等多久算过期（默认 65s，须 > TICKET_TTL=60s）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
