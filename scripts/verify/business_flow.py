#!/usr/bin/env python
"""业务闭环验收：导入 → 登录/改密 → 考勤 → 任务回调 → 报告版本 → 建议采纳。

前置：隔离后端已启动（MACT_DEMO_ENABLED=true），数据库已迁移到 V23。
脚本只创建带唯一后缀的测试数据；退出码非 0 表示断言失败。

    python scripts/verify/business_flow.py --base http://localhost:18001/api
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time

import requests


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()

    def call(self, method: str, path: str, token: str | None = None,
             body=None, headers: dict | None = None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if headers:
            h.update(headers)
        resp = self.session.request(method, self.base + path, json=body, headers=h, timeout=30)
        try:
            payload = resp.json() if resp.content else {}
        except json.JSONDecodeError:
            payload = {"raw": resp.text}
        return resp.status_code, payload


class Checks:
    def __init__(self):
        self.failures: list[str] = []
        self.passed = 0

    def eq(self, name: str, actual, expected):
        if actual == expected:
            self.passed += 1
            print(f"  PASS {name}: {actual}")
        else:
            self.failures.append(f"{name}: expected {expected!r}, got {actual!r}")
            print(f"  FAIL {name}: expected {expected!r}, got {actual!r}")

    def true(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  PASS {name}")
        else:
            self.failures.append(f"{name} {detail}")
            print(f"  FAIL {name} {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:18001/api")
    parser.add_argument("--service-token", default="mact-eval-dev-token")
    args = parser.parse_args()

    api = Api(args.base)
    checks = Checks()
    suffix = str(int(time.time() * 1000))[-8:]

    admin = api.call("POST", "/auth/login", body={"username": "admin", "password": "admin123"})[1]["data"]
    status, body = api.call("POST", "/classes", token=admin["token"], body={
        "name": f"闭环班-{suffix}", "grade": "测试", "department": "测试"})
    checks.eq("admin creates class", status, 200)
    class_id = body["data"]["id"]

    # ---------- BE02 import / login / forced password change ----------
    students_payload = [
        {"studentId": f"S{suffix}1", "name": "导入学生一", "seatRow": 1, "seatCol": 1},
        {"studentId": f"S{suffix}2", "name": "导入学生二", "seatRow": 1, "seatCol": 2},
    ]
    for attempt in (1, 2):
        status, body = api.call("POST", "/admin/students/import", token=admin["token"], body={
            "classId": class_id, "students": students_payload})
        checks.eq(f"student import attempt {attempt}", status, 200)
        expected_imported = 2 if attempt == 1 else 0
        checks.eq(f"import attempt {attempt} imported", body["data"]["imported"], expected_imported)
        checks.eq(f"import attempt {attempt} skipped", body["data"]["skipped"], 2 - expected_imported)

    status, body = api.call("POST", "/admin/students/import", token=admin["token"], body={
        "classId": class_id, "students": [{"studentId": "", "name": "无效"}]})
    checks.eq("invalid import rejects whole batch", status, 400)

    username = f"S{suffix}1"
    status, body = api.call("POST", "/auth/login", body={"username": username, "password": "123456"})
    checks.eq("imported student can login by studentId", status, 200)
    student_token = body["data"]["token"]
    checks.true("imported student must change password", body["data"]["mustChangePassword"] is True)

    status, body = api.call("GET", "/courses", token=student_token)
    checks.eq("business API blocked before password change", status, 403)
    checks.eq("password-change-required business code", body.get("code"), 428)

    status, body = api.call("PUT", "/auth/password", token=student_token,
                            body={"oldPassword": "123456", "newPassword": "newpass123"})
    checks.eq("student changes initial password", status, 200)
    student_token = body["data"]["token"]
    checks.true("new token no longer requires change", body["data"]["mustChangePassword"] is False)
    status, _ = api.call("GET", "/courses", token=student_token)
    checks.eq("business API available after password change", status, 200)

    # ---------- BE03/BE04 attendance ----------
    teacher = api.call("POST", "/auth/register", body={
        "username": f"flow-teacher-{suffix}", "password": "secret123",
        "name": "闭环教师", "role": "teacher"})[1]["data"]
    status, body = api.call("POST", "/courses", token=teacher["token"], body={
        "name": f"闭环课程-{suffix}", "code": f"FL{suffix}"})
    checks.eq("teacher creates course", status, 200)
    course_id = body["data"]["id"]
    status, body = api.call("POST", "/sessions", token=teacher["token"], body={
        "courseId": course_id, "classId": class_id, "title": f"闭环课堂-{suffix}"})
    checks.eq("teacher creates session", status, 200)
    lesson_id = body["data"]["id"]

    status, _ = api.call("POST", f"/attendance/{lesson_id}/init", token=teacher["token"])
    checks.eq("initialize roster", status, 200)
    status, body = api.call("GET", f"/attendance/{lesson_id}", token=teacher["token"])
    checks.eq("roster has two records", len(body["data"]), 2)
    my_record = next(r for r in body["data"] if r["studentName"] == "导入学生一")
    other_record = next(r for r in body["data"] if r["studentName"] == "导入学生二")

    status, _ = api.call("POST", "/attendance/checkin", token=student_token, body={
        "lessonSessionId": lesson_id, "studentId": my_record["studentId"]})
    checks.eq("checkin rejected before class starts", status, 409)

    status, _ = api.call("PATCH", f"/sessions/{lesson_id}/status", token=teacher["token"],
                         body={"status": "IN_PROGRESS"})
    checks.eq("teacher starts session", status, 200)

    status, body = api.call("POST", "/attendance/checkin", token=student_token, body={
        "lessonSessionId": lesson_id, "studentId": my_record["studentId"]})
    checks.eq("student checkin while in progress", status, 200)
    checks.true("checkin marks present or late",
                body["data"]["status"] in ("PRESENT", "LATE"))

    status, _ = api.call("POST", "/attendance/confirm", token=teacher["token"], body={
        "lessonSessionId": lesson_id,
        "records": [{"recordId": other_record["id"], "status": "ABSENT", "comment": "确认缺勤"}]})
    checks.eq("teacher confirms absence", status, 200)

    # 学生不能替他人签到
    status, _ = api.call("POST", "/attendance/checkin", token=student_token, body={
        "lessonSessionId": lesson_id, "studentId": other_record["studentId"]})
    checks.eq("student cannot check in for another student", status, 403)

    # 学生本人也不能覆盖教师已确认的缺勤
    username2 = f"S{suffix}2"
    status, body = api.call("POST", "/auth/login", body={"username": username2, "password": "123456"})
    checks.eq("second student login", status, 200)
    student2_token = body["data"]["token"]
    status, body = api.call("PUT", "/auth/password", token=student2_token,
                            body={"oldPassword": "123456", "newPassword": "newpass123"})
    checks.eq("second student changes password", status, 200)
    student2_token = body["data"]["token"]
    status, _ = api.call("POST", "/attendance/checkin", token=student2_token, body={
        "lessonSessionId": lesson_id, "studentId": other_record["studentId"]})
    checks.eq("confirmed absence cannot be overwritten by student", status, 409)
    status, body = api.call("GET", f"/attendance/{lesson_id}", token=teacher["token"])
    still_absent = next(r for r in body["data"] if r["id"] == other_record["id"])
    checks.true("confirmed absence status unchanged", still_absent["status"] == "ABSENT")

    status, _ = api.call("POST", f"/attendance/{lesson_id}/init", token=teacher["token"])
    checks.eq("re-initialize roster", status, 200)
    status, body = api.call("GET", f"/attendance/{lesson_id}", token=teacher["token"])
    checks.eq("re-initialize does not duplicate records", len(body["data"]), 2)
    confirmed = next(r for r in body["data"] if r["id"] == other_record["id"])
    checks.true("teacher decision preserved after re-init",
                confirmed["status"] == "ABSENT" and confirmed["teacherConfirmed"] is True)

    # ---------- AI02 media job + idempotent callback ----------
    # SEC-03（2026-10-28）：init 必须声明 sizeBytes 与 contentType，confirm 用
    # statObject 的实测值对照。这里上传的字节数必须与声明一致，否则 confirm 409。
    payload = b"not-a-real-video"
    status, body = api.call("POST", "/upload/media", token=teacher["token"], body={
        "mediaType": "video", "fileName": "flow-test.mp4", "lessonSessionId": lesson_id,
        "sizeBytes": len(payload), "contentType": "video/mp4"})
    checks.eq("request presigned upload", status, 200)
    upload_url, object_name = body["data"]["uploadUrl"], body["data"]["objectName"]
    asset_id = body["data"]["assetId"]
    put = requests.put(upload_url, data=payload, timeout=30,
                       headers={"Content-Type": "video/mp4"})
    checks.eq("upload object to MinIO", put.status_code, 200)
    # 另一名教师不能确认他人上传会话（对象归属绑定）
    other_teacher = api.call("POST", "/auth/register", body={
        "username": f"flow-teacher2-{suffix}", "password": "secret123",
        "name": "闭环教师2", "role": "teacher"})[1]["data"]
    status, _ = api.call("POST", "/upload/media/confirm", token=other_teacher["token"], body={
        "assetId": asset_id, "objectName": object_name, "lessonSessionId": lesson_id})
    checks.eq("cross-user media asset confirm rejected", status, 404)

    status, body = api.call("POST", "/upload/media/confirm", token=teacher["token"], body={
        "assetId": asset_id, "objectName": object_name,
        "lessonSessionId": lesson_id, "totalSeats": 2, "sampleInterval": 2, "windowDuration": 60})
    checks.eq("confirm media creates job", status, 200)
    job_id = body["data"]["jobId"]

    callback = {
        "status": "succeeded",
        "model_digest": "test-weights-sha256",
        "config": {"device": "cpu", "window_duration": 60},
        "media_sha256": "a" * 64,
        "observations": [
            {"window_index": 0, "window_start_ms": 0, "window_end_ms": 60000,
             "visible_seat_count": 2, "total_configured_seats": 2, "coverage": 1.0,
             "data_quality": "usable", "in_seat_count": 2, "leave_seat_count": 0,
             "hand_raised_count": 0, "standing_count": 0, "upright_posture_count": 2,
             "upright_proportion": 0.8},
            {"window_index": 1, "window_start_ms": 60000, "window_end_ms": 120000,
             "visible_seat_count": 2, "total_configured_seats": 2, "coverage": 1.0,
             "data_quality": "usable", "in_seat_count": 2, "leave_seat_count": 0,
             "hand_raised_count": 0, "standing_count": 0, "upright_posture_count": 1,
             "upright_proportion": 0.75},
        ],
        "window_scene_metadata": [
            {"window_index": 0, "data_quality": "usable"},
            {"window_index": 1, "data_quality": "usable"},
        ],
    }
    headers = {"X-Service-Token": args.service_token}
    status, body = api.call("POST", f"/jobs/{job_id}/claim", body={"leaseSeconds": 120}, headers=headers)
    checks.eq("worker claims job", status, 200)
    checks.true("first claim owns lease", body["data"]["claimed"] is True)
    attempt = body["data"]["attempt"]
    status, body = api.call("POST", f"/jobs/{job_id}/claim", body={"leaseSeconds": 120}, headers=headers)
    checks.true("second claim rejected while lease active", body["data"]["claimed"] is False)
    status, body = api.call("POST", f"/jobs/{job_id}/heartbeat", body={"leaseSeconds": 120}, headers=headers)
    checks.true("heartbeat extends lease", body["data"]["extended"] is True)

    callback["attempt"] = attempt
    status, _ = api.call("POST", f"/jobs/{job_id}/complete", body=callback, headers=headers)
    checks.eq("worker callback accepted", status, 200)

    # 并发重放同一回调：行锁 + 终态检查 + (job,window) 幂等键
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(api.call, "POST", f"/jobs/{job_id}/complete",
                               None, callback, headers) for _ in range(2)]
        for future in results:
            status, _ = future.result()
            checks.eq("duplicate callback status", status, 200)
    status, body = api.call("GET", f"/behavior/{lesson_id}", token=teacher["token"])
    checks.eq("duplicate callbacks do not duplicate observations", len(body["data"]), 2)
    status, body = api.call("GET", f"/window-features/{lesson_id}", token=teacher["token"])
    checks.true("window alignment produced features", len(body["data"]["windows"]) >= 1)

    # ---------- BE06/BE07/BE08 report versions + suggestions ----------
    status, body = api.call("POST", f"/reports/generate/{lesson_id}", token=teacher["token"])
    checks.eq("generate report", status, 200)
    report = body["data"]
    checks.eq("report data source", report["dataSource"], "REAL")
    checks.true("report has climate score", report["climateScore"] is not None)
    checks.true("report has version 1", report.get("version") == 1)
    checks.true("report has evidence-based suggestions", len(report["suggestions"]) > 0)
    suggestion_id = report["suggestions"][0]["id"]
    checks.true("suggestions carry rule id", report["suggestions"][0].get("ruleId") is not None)
    checks.true("analysis video job recorded", report.get("analysisVideoJobId") == job_id)
    trace = report.get("evidence", {}).get("traceability", {}).get("video", {})
    checks.eq("report traces media digest", trace.get("mediaSha256"), "sha256:" + ("a" * 64))
    checks.eq("report traces model digest", trace.get("modelDigest"), "test-weights-sha256")
    checks.eq("report traces runtime config", trace.get("config", {}).get("device"), "cpu")

    status, body = api.call("POST", f"/reports/{lesson_id}/publish", token=teacher["token"])
    checks.eq("publish report", status, 200)
    checks.eq("published status", body["data"]["status"], "published")
    checks.true("published version snapshot hash present", body["data"].get("publishedBy") == teacher["userId"])

    status, body = api.call("GET", f"/reports/{lesson_id}/versions", token=teacher["token"])
    checks.eq("version history has one snapshot", len(body["data"]), 1)
    first_hash = body["data"][0]["contentHash"]

    status, body = api.call("POST", f"/reports/generate/{lesson_id}", token=teacher["token"])
    checks.eq("regenerate published report", status, 200)
    checks.eq("regenerated into draft", body["data"]["status"], "draft")
    checks.eq("regenerated version increments", body["data"]["version"], 2)
    status, body = api.call("GET", f"/reports/{lesson_id}/versions", token=teacher["token"])
    checks.eq("published snapshot retained after regeneration", len(body["data"]), 1)
    checks.eq("snapshot content hash unchanged", body["data"][0]["contentHash"], first_hash)

    status, _ = api.call("POST", f"/reports/{lesson_id}/publish", token=teacher["token"])
    checks.eq("publish version 2", status, 200)
    status, body = api.call("GET", f"/reports/{lesson_id}/versions", token=teacher["token"])
    checks.eq("two published versions retained", len(body["data"]), 2)

    # 重新生成会替换 pending 建议；采纳最新报告中的一条 pending 建议。
    status, body = api.call("GET", f"/reports/{lesson_id}", token=teacher["token"])
    pending = [s for s in body["data"]["suggestions"] if s["status"] == "pending"]
    checks.true("regenerated report exposes pending suggestions", len(pending) > 0)
    suggestion_id = pending[0]["id"]
    status, body = api.call("POST", f"/suggestions/{suggestion_id}/adopt", token=teacher["token"])
    checks.eq("adopt suggestion", status, 200)
    checks.eq("suggestion status persisted", body["data"]["status"], "adopted")
    status, body = api.call("GET", f"/reports/{lesson_id}", token=teacher["token"])
    adopted = next(s for s in body["data"]["suggestions"] if s["id"] == suggestion_id)
    checks.eq("report reads adopted status from DB", adopted["status"], "adopted")

    # 学生只能看已发布版本
    status, _ = api.call("GET", f"/reports/{lesson_id}", token=student_token)
    checks.eq("student reads published report", status, 200)
    status, _ = api.call("POST", f"/reports/generate/{lesson_id}", token=teacher["token"])
    checks.eq("regenerate to draft again", status, 200)
    status, body = api.call("GET", f"/reports/{lesson_id}", token=student_token)
    checks.eq("student still reads last published version", status, 200)
    checks.eq("student snapshot status", body["data"]["status"], "published")
    checks.eq("student snapshot version", body["data"]["version"], 2)

    # ---------- empty report cannot be published ----------
    status, body = api.call("POST", "/sessions", token=teacher["token"], body={
        "courseId": course_id, "classId": class_id, "title": f"空课堂-{suffix}"})
    empty_lesson = body["data"]["id"]
    status, body = api.call("POST", f"/reports/generate/{empty_lesson}", token=teacher["token"])
    checks.eq("generate empty report", status, 200)
    checks.eq("empty report data source", body["data"]["dataSource"], "EMPTY")
    status, _ = api.call("POST", f"/reports/{empty_lesson}/publish", token=teacher["token"])
    checks.eq("empty report publish rejected", status, 409)

    print(f"\n{checks.passed} checks passed, {len(checks.failures)} failed")
    if checks.failures:
        for failure in checks.failures:
            print(" -", failure)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
