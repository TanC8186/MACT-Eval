#!/usr/bin/env python
"""删除报告连带删除证据帧：**真实栈**端到端验证（SEC-11 / TASK B）。

    python -I scripts/verify/verify_delete_evidence_live.py \
        --base http://127.0.0.1:8000/api \
        --minio http://127.0.0.1:19100 \
        --pg-container mact-ui-postgres --pg-db macteval_ui

## 为什么需要这个脚本

单测（`ReportDeleteEvidenceTest`）用 Mockito 打桩 `ObjectProbe`，因此它证明的是
"**如果** probe 返回 ERROR 就保留行"，而不是"真实 MinIO 在什么情况下返回 ERROR"。
`docs/reviews/2026-10-29-closure-implementation-review.md` §4 把这两点列为未验证：

  1. NOT_FOUND vs ERROR 对**真实** MinIO 的行为（S3 错误码 → ObjectProbe 的映射）；
  2. 外键 `fk_evidence_lesson`（ON DELETE RESTRICT）在**真实库**上的删除行为。

本脚本补的就是这两条。它**不**重跑单测已经覆盖的纯逻辑，只补真实环境那一层。

## 安全边界

* 只在隔离 rig 上跑：容器名必须正好是 `mact-ui-*`，库名必须 `macteval_ui`；
  与 `rig_accounts.py` / `browser_rig.py` 同一套守卫。
* 只操作**本次新建**的课堂与报告（ID 带唯一后缀），不碰既有课堂/报告/证据帧。
* 退出时清理自己造的数据；清理失败会打印残留 ID，不静默吞掉。

## 覆盖的断言

| # | 断言 | 怎么证明 |
|---|---|---|
| 1 | 真实 MinIO：存在的对象 → FOUND | `stat_object` 成功 |
| 2 | 真实 MinIO：不存在的 key → `NoSuchKey`(404) → NOT_FOUND | 错误码在 `MISSING_ERROR_CODES` 里 |
| 3 | 真实 MinIO：不存在的桶 → `NoSuchBucket`(404) → NOT_FOUND | 同上 |
| 4 | 真实 MinIO：错误凭据 → `SignatureDoesNotMatch`(403) → **ERROR**，不是 NOT_FOUND | 403 不在缺失码集合里 |
| 5 | 真实库：`fk_evidence_lesson` 是 RESTRICT | 直接查 information_schema |
| 6 | 真实栈：DELETE /reports/{lesson} → 对象真的从桶里消失 | `stat_object` 由命中转 NoSuchKey |
| 7 | 真实栈：DELETE 后该课堂的证据帧行归零 | SQL count == 0 |
| 8 | 真实栈：DELETE 后报告行与其版本行归零 | SQL count == 0 |
| 9 | 真实栈：其它课堂的证据帧**一条不少**（不超删） | 删除前后计数相等 |

退出码：0 全通过；1 有失败；2 环境不可用（不是"通过"）。
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    from minio import Minio
    from minio.error import S3Error
except ImportError:  # pragma: no cover
    print("需要 minio：python -m pip install minio", file=sys.stderr)
    raise SystemExit(2)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: Java 侧 MinioService.MISSING_ERROR_CODES 的镜像。刻意重复写一遍而不是 import：
#: 这个脚本要独立证明"真实 MinIO 返回的码**确实**落在这个集合里"，从被测代码里
#: 读集合会让断言变成同义反复。
MISSING_ERROR_CODES = {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NoSuchVersion", "NotFound"}
BUCKET = "analysis-artifacts"


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

    def expect_eq(self, name: str, actual, expected) -> None:
        self.expect(name, actual == expected, f"actual={actual!r} expected={expected!r}")


def http(base: str, method: str, path: str, token: str | None = None,
         body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, (json.loads(raw) if raw.strip() else {})
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def psql(container: str, db: str, sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, "psql", "-U", "macteval", "-d", db,
         "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    if result.returncode != 0:
        raise SystemExit(f"psql 失败：{result.stderr.strip()[:400]}")
    return result.stdout.strip()


def guard(container: str, db: str) -> None:
    """隔离守卫：只允许在 rig 容器/库上跑。"""
    if not container.startswith("mact-ui-"):
        raise SystemExit(f"拒绝操作容器 {container!r}（必须是 mact-ui-*）")
    if db != "macteval_ui":
        raise SystemExit(f"拒绝操作数据库 {db!r}（必须是 macteval_ui）")
    running = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", container],
                             capture_output=True, text=True, timeout=30)
    if running.returncode != 0 or running.stdout.strip() != "true":
        raise SystemExit(f"{container} 未运行")


def probe_status(client: Minio, bucket: str, key: str) -> tuple[str, str]:
    """镜像 MinioService.probeObject 的三态判定，返回 (status, code)。"""
    try:
        client.stat_object(bucket, key)
        return "FOUND", ""
    except S3Error as exc:
        code = exc.code
        if code in MISSING_ERROR_CODES:
            return "NOT_FOUND", code
        return "ERROR", code
    except Exception as exc:  # noqa: BLE001
        return "ERROR", type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000/api")
    parser.add_argument("--minio", default="http://127.0.0.1:19100")
    parser.add_argument("--minio-access", default=os.environ.get("MACT_MINIO_ACCESS_KEY", "uirig"))
    parser.add_argument("--minio-secret", default=os.environ.get("MACT_MINIO_SECRET_KEY", "uirig_secret"))
    parser.add_argument("--pg-container", default="mact-ui-postgres")
    parser.add_argument("--pg-db", default="macteval_ui")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    guard(args.pg_container, args.pg_db)
    checks = Checks()
    suffix = uuid.uuid4().hex[:8]
    endpoint = args.minio.split("://", 1)[-1]
    client = Minio(endpoint, access_key=args.minio_access, secret_key=args.minio_secret, secure=False)

    # ---------- 1–4：真实 MinIO 的错误码映射 ----------
    print("==> 1. 真实 MinIO 的 S3 错误码 → ObjectProbe 三态")
    existing_key = psql(args.pg_container, args.pg_db,
                        "select object_key from behavior_evidence_frames "
                        f"where bucket='{BUCKET}' limit 1")
    if not existing_key:
        print("!! rig 库里没有证据帧，无法取真实 key；跳过真实对象断言", file=sys.stderr)
        return 2
    status, code = probe_status(client, BUCKET, existing_key)
    checks.expect_eq("存在的对象 → FOUND", status, "FOUND")

    status, code = probe_status(client, BUCKET, f"live-probe-{suffix}/never-written.jpg")
    checks.expect("不存在的 key → NOT_FOUND", status == "NOT_FOUND", f"status={status} code={code}")
    checks.expect_eq("  且错误码是 NoSuchKey", code, "NoSuchKey")

    status, code = probe_status(client, f"no-such-bucket-{suffix}", "a.jpg")
    checks.expect("不存在的桶 → NOT_FOUND", status == "NOT_FOUND", f"status={status} code={code}")
    checks.expect_eq("  且错误码是 NoSuchBucket", code, "NoSuchBucket")

    bad = Minio(endpoint, access_key=args.minio_access, secret_key="definitely-wrong", secure=False)
    status, code = probe_status(bad, BUCKET, existing_key)
    checks.expect("错误凭据(403) → ERROR（**不是** NOT_FOUND）", status == "ERROR",
                  f"status={status} code={code}")
    checks.expect_eq("  且错误码是 SignatureDoesNotMatch", code, "SignatureDoesNotMatch")
    checks.expect("ERROR≠NOT_FOUND：403 不在缺失码集合内", code not in MISSING_ERROR_CODES,
                  f"code={code}")

    # ---------- 5：真实库的外键删除规则 ----------
    print("==> 2. 真实库的外键行为")
    rule = psql(args.pg_container, args.pg_db,
                "select rc.delete_rule from information_schema.table_constraints tc "
                "join information_schema.referential_constraints rc "
                "  on rc.constraint_name=tc.constraint_name "
                "where tc.constraint_type='FOREIGN KEY' "
                "  and tc.constraint_name='fk_evidence_lesson'")
    checks.expect_eq("fk_evidence_lesson 是 RESTRICT", rule, "RESTRICT")

    # ---------- 6–9：真实栈 DELETE /reports/{lesson} ----------
    print("==> 3. 真实栈：DELETE /reports/{lesson} 的连带删除")
    teacher_user = os.environ.get("MACT_E2E_TEACHER_USER", "").strip() or "13800002026"
    teacher_pass = os.environ.get("MACT_E2E_TEACHER_PASS", "").strip() or "UiRigPass2026"
    st, body = http(args.base, "POST", "/auth/login",
                    body={"username": teacher_user, "password": teacher_pass})
    if st != 200:
        print(f"!! 教师登录失败 HTTP {st}：{body}", file=sys.stderr)
        return 2
    data = body["data"]
    token = data["token"]
    teacher_id = data.get("userId") or data.get("id")

    st, body = http(args.base, "POST", "/courses", token=token,
                    body={"name": f"删除验证课程-{suffix}", "code": f"DEL{suffix}"})
    if st != 200:
        print(f"!! 建课程失败 HTTP {st}：{body}", file=sys.stderr)
        return 2
    course_id = body["data"]["id"]

    st, body = http(args.base, "POST", "/sessions", token=token,
                    body={"courseId": course_id, "classId": "class-001",
                          "title": f"删除验证课堂-{suffix}"})
    if st != 200:
        print(f"!! 建课堂失败 HTTP {st}：{body}", file=sys.stderr)
        return 2
    lesson_id = body["data"]["id"]
    local_keys: list[str] = []

    # 从这里开始会写入合成数据。首版脚本没有失败清理：
    #   * `uq_evidence_frame` 冲突中止后留下课堂 + 课程 + 报告；
    #   * **对象也没删** —— 在"跑的是旧 jar、产品没删对象"那次运行之后，
    #     `analysis-artifacts/lessons/<lesson>/` 下留了 3 个孤儿 JPEG（已手工清掉）。
    # 数据库行和 MinIO 对象两边都要清，缺一边就是留垃圾。挂 atexit：它对正常返回、
    # SystemExit 与未捕获异常都会触发。
    def cleanup_synthetic() -> None:
        for key in local_keys:
            try:
                client.remove_object(BUCKET, key)
            except Exception:  # noqa: BLE001 - 对象可能本来就没上传成功
                pass
        try:
            psql(args.pg_container, args.pg_db,
                 f"delete from behavior_evidence_frames where lesson_session_id='{lesson_id}'")
            psql(args.pg_container, args.pg_db,
                 f"delete from evaluation_report_versions where lesson_session_id='{lesson_id}'")
            psql(args.pg_container, args.pg_db,
                 f"delete from suggestions where lesson_session_id='{lesson_id}'")
            psql(args.pg_container, args.pg_db,
                 f"delete from evaluation_reports where lesson_session_id='{lesson_id}'")
            psql(args.pg_container, args.pg_db,
                 f"delete from lesson_sessions where id='{lesson_id}'")
            psql(args.pg_container, args.pg_db, f"delete from courses where id='{course_id}'")
        except SystemExit:
            print(f"!! 清理失败，残留 lesson={lesson_id} course={course_id}", file=sys.stderr)

    atexit.register(cleanup_synthetic)

    # 报告 + 版本 + 证据帧 + 真实对象：直接用 SQL 造最小可删集合。
    # 走完整分析管线要 120 分钟视频，与本脚本要验证的东西无关。
    report_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    keys = [f"lessons/{lesson_id}/jobs/{job_id}/window-{i:04d}-overview.jpg" for i in range(3)]
    # cleanup 闭包在调用时才读这个列表；先填上，保证"上传到一半就异常"也能清干净。
    local_keys.extend(keys)
    frame_ids = [str(uuid.uuid4()) for _ in keys]
    other_lesson = psql(args.pg_container, args.pg_db,
                        "select lesson_session_id from behavior_evidence_frames "
                        "group by 1 order by count(*) asc limit 1")
    before_other = int(psql(args.pg_container, args.pg_db,
                            "select count(*) from behavior_evidence_frames "
                            f"where lesson_session_id='{other_lesson}'"))

    psql(args.pg_container, args.pg_db,
         "insert into evaluation_reports (id, lesson_session_id, course_id, class_id, teacher_id, "
         f"title, status, updated_at) values ('{report_id}','{lesson_id}','{course_id}',"
         f"'class-001','{teacher_id}','删除验证报告','published', now())")
    psql(args.pg_container, args.pg_db,
         "insert into evaluation_report_versions (id, report_id, lesson_session_id, version, status, "
         f"snapshot_json) values ('{uuid.uuid4()}','{report_id}','{lesson_id}',1,'published','{{}}')")
    for index, (fid, key) in enumerate(zip(frame_ids, keys)):
        # uq_evidence_frame 是 (job_id, window_index, code)：同一 job 内
        # window_index 必须递增，三帧不能都写 0（首版就踩了这个）。
        psql(args.pg_container, args.pg_db,
             "insert into behavior_evidence_frames (id, lesson_session_id, job_id, window_index, "
             "bucket, object_key, code) values "
             f"('{fid}','{lesson_id}','{job_id}',{index},'{BUCKET}','{key}','overview')")

    import io
    for key in keys:
        payload = b"\xff\xd8\xff\xe0" + b"live-delete-drill" * 64
        client.put_object(BUCKET, key, io.BytesIO(payload), length=len(payload),
                          content_type="image/jpeg")

    frames_before = int(psql(args.pg_container, args.pg_db,
                             "select count(*) from behavior_evidence_frames "
                             f"where lesson_session_id='{lesson_id}'"))
    checks.expect_eq("准备：该课堂有 3 条证据帧", frames_before, 3)
    checks.expect("准备：3 个对象真的在桶里",
                  all(probe_status(client, BUCKET, k)[0] == "FOUND" for k in keys),
                  "statObject 全部命中")

    st, body = http(args.base, "DELETE", f"/reports/{lesson_id}", token=token)
    checks.expect_eq("DELETE /reports/{lesson} → 200", st, 200)

    gone = [probe_status(client, BUCKET, k) for k in keys]
    checks.expect("对象真的从桶里消失", all(s == "NOT_FOUND" for s, _ in gone),
                  f"probes={gone}")
    frames_after = int(psql(args.pg_container, args.pg_db,
                            "select count(*) from behavior_evidence_frames "
                            f"where lesson_session_id='{lesson_id}'"))
    checks.expect_eq("证据帧行归零", frames_after, 0)
    reports_after = int(psql(args.pg_container, args.pg_db,
                             "select count(*) from evaluation_reports "
                             f"where lesson_session_id='{lesson_id}'"))
    versions_after = int(psql(args.pg_container, args.pg_db,
                              "select count(*) from evaluation_report_versions "
                              f"where lesson_session_id='{lesson_id}'"))
    checks.expect_eq("报告行归零", reports_after, 0)
    checks.expect_eq("报告版本行归零", versions_after, 0)
    after_other = int(psql(args.pg_container, args.pg_db,
                           "select count(*) from behavior_evidence_frames "
                           f"where lesson_session_id='{other_lesson}'"))
    checks.expect_eq("其它课堂的证据帧一条不少（不超删）", after_other, before_other)

    # ---------- 清理（atexit 里统一做，这里只断言结果） ----------
    cleanup_synthetic()
    leftover = psql(args.pg_container, args.pg_db,
                    f"select count(*) from lesson_sessions where id='{lesson_id}'")
    checks.expect_eq("清理：合成课堂已删除", int(leftover), 0)
    leftover_course = psql(args.pg_container, args.pg_db,
                           f"select count(*) from courses where id='{course_id}'")
    checks.expect_eq("清理：合成课程已删除", int(leftover_course), 0)

    total = checks.passed + len(checks.failed)
    print(f"\n{checks.passed}/{total} checks passed, {len(checks.failed)} failed")
    if checks.failed:
        for item in checks.failed:
            print(" -", item)

    if args.json:
        evidence = {
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "taskbookRef": "SEC-11 / TASK B — 删除报告连带删除证据帧（真实栈）",
            "why": "单测用 Mockito 打桩 ObjectProbe；本脚本补真实 MinIO 错误码映射"
                   "与真实库外键行为，对应 2026-10-29-closure-implementation-review §4",
            "stack": {"base": args.base, "minio": args.minio,
                      "pgContainer": args.pg_container, "pgDb": args.pg_db},
            "checks": {"passed": checks.passed, "total": total, "failures": checks.failed},
            "passed": not checks.failed,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(evidence, fh, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")

    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
