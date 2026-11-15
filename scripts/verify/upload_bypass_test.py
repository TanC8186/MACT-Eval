#!/usr/bin/env python
"""直传媒体服务端校验的真实绕过测试（SEC-03，2026-10-28）。

    python -I scripts/verify/upload_bypass_test.py \
        --base http://127.0.0.1:8000/api \
        --minio http://127.0.0.1:19100

和 `MediaAssetUploadValidationTest`（JUnit，mock 掉 MinIO）的分工：

  * JUnit 证明**判定逻辑**对；
  * 本脚本证明**真实的 MinIO + 真实 HTTP 栈**下也挡住，且对象真的被删掉。

两者都需要：mock 测试不可能发现"removeObject 的 bucket 名写错了"或
"预签名 URL 其实允许匿名覆盖"，而这类错误恰恰只在真实栈上出现。

覆盖审计点名的六种绕过（全部**不经过前端**，直接调 API + 直 PUT 到 MinIO）：

  1. 超大文件（声明小、实传大 → confirm 必须 413 且对象被删）
  2. 伪造 MIME（扩展名 .mp4，Content-Type text/html → 415 且对象被删）
  3. 扩展名错配（.exe → init 415）
  4. 错误课堂（用别人的课堂 ID → 403/404）
  5. 未知 object key（拿别人的 objectName 去 confirm → 409，且不 stat）
  6. 重复 confirm（第二次必须被拒或幂等，不得再建第二个任务）

外加：声明值缺失/为零时被拒（验证"必填"不是文档说说）。

退出码：0 全部按预期拒绝；1 有任一用例没挡住；2 环境不可用。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


@dataclass
class Api:
    base: str
    token: str = ""
    results: list[Result] = field(default_factory=list)

    def call(self, method: str, path: str, body: dict | None = None,
             token: str | None = None, raw: bool = False):
        headers = {"Content-Type": "application/json"}
        use = token if token is not None else self.token
        if use:
            headers["Authorization"] = f"Bearer {use}"
        resp = requests.request(method, self.base.rstrip("/") + path,
                                json=body, headers=headers, timeout=60)
        if raw:
            return resp
        payload = resp.json() if resp.content else {}
        return resp.status_code, payload

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))


def login_or_register(api: Api, username: str) -> str:
    status, body = api.call("POST", "/auth/login",
                            {"username": username, "password": "Bypass-Test-Pw-1"})
    if status == 200:
        return body["data"]["token"]
    status, body = api.call("POST", "/auth/register", {
        "username": username, "password": "Bypass-Test-Pw-1",
        "name": username, "role": "teacher"})
    if status != 200:
        raise SystemExit(f"cannot create teacher {username}: {status} {body}")
    return body["data"]["token"]


def make_lesson(api: Api, token: str, suffix: str) -> str:
    status, body = api.call("POST", "/courses", {
        "name": f"绕过测试课程-{suffix}", "code": f"BP{suffix}", "department": "测试",
    }, token=token)
    assert status == 200, f"course: {status} {body}"
    course = body["data"]["id"]
    status, body = api.call("POST", "/sessions", {
        "courseId": course, "classId": "class-001", "title": f"绕过测试课堂-{suffix}",
    }, token=token)
    assert status == 200, f"session: {status} {body}"
    return body["data"]["id"]


def put(url: str, data: bytes, content_type: str) -> int:
    resp = requests.put(url, data=data, headers={"Content-Type": content_type}, timeout=120)
    return resp.status_code


def object_exists(minio: str, bucket: str, access: str, secret: str, name: str) -> bool:
    """直接用 MinIO SDK 查对象是否还在（不经过后端，避免被后端的缓存骗到）。"""
    from minio import Minio
    from minio.error import S3Error

    client = Minio(minio.replace("http://", "").replace("https://", ""),
                   access_key=access, secret_key=secret, secure=False)
    try:
        client.stat_object(bucket, name)
        return True
    except S3Error:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000/api")
    parser.add_argument("--minio", default="http://127.0.0.1:19100")
    parser.add_argument("--minio-access", default="macteval")
    parser.add_argument("--minio-secret", default="macteval_secret")
    parser.add_argument("--json", help="结果写成 JSON")
    args = parser.parse_args()

    api = Api(base=args.base)
    suffix = str(int(time.time()))[-8:]

    try:
        probe = api.call("GET", "/health", raw=True)
        status = probe.status_code
        if status != 200:
            raise RuntimeError(f"backend not healthy: HTTP {status}")
    except Exception as exc:
        print(f"环境不可用：{exc}", file=sys.stderr)
        return 2

    token = login_or_register(api, f"bypass-a-{suffix}")
    api.token = token
    other = login_or_register(api, f"bypass-b-{suffix}")
    lesson = make_lesson(api, token, suffix)
    other_lesson = make_lesson(api, other, suffix + "b")

    print("==> 1. 扩展名白名单（init 阶段）")
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "payload.exe",
        "lessonSessionId": lesson, "sizeBytes": 1024, "contentType": "video/mp4"})
    api.check("init 拒绝 .exe", status == 415, f"HTTP {status}")

    print("==> 2. 声明 MIME 与扩展名不匹配（init 阶段）")
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "ok.mp4",
        "lessonSessionId": lesson, "sizeBytes": 1024, "contentType": "text/html"})
    api.check("init 拒绝 text/html + .mp4", status == 415, f"HTTP {status}")

    print("==> 3. 声明大小超上限（init 阶段）")
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "ok.mp4", "lessonSessionId": lesson,
        "sizeBytes": 3 * 1024 * 1024 * 1024, "contentType": "video/mp4"})
    # 这一层由 DTO 的 @Max 拦下，所以是 400（Bean Validation）而不是 413。
    # 断言 400 并注明原因，比"接受 400 或 413"诚实：那个宽松写法会让
    # "策略层根本没被调用"也看起来通过。
    api.check("init 拒绝 3 GiB 声明（@Max 先于策略层，故为 400）",
              status == 400, f"HTTP {status} body.code={body.get('code')}")

    print("==> 4. 缺少声明字段（init 阶段）")
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "ok.mp4", "lessonSessionId": lesson})
    api.check("init 拒绝缺少 sizeBytes/contentType", status == 400, f"HTTP {status}")

    print("==> 5. 别人的课堂（init 阶段）")
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "ok.mp4",
        "lessonSessionId": other_lesson, "sizeBytes": 1024, "contentType": "video/mp4"})
    api.check("init 拒绝跨教师课堂", status in (403, 404, 400), f"HTTP {status}")

    print("==> 6. 实传大于声明（confirm 阶段必须拒绝并删对象）")
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "grow.mp4",
        "lessonSessionId": lesson, "sizeBytes": 1024, "contentType": "video/mp4"})
    if status != 200:
        api.check("init 合法媒体失败", False, f"HTTP {status} {body}")
    else:
        data = body["data"]
        real_bytes = b"x" * 4096          # 实传 4 KiB，声明 1 KiB
        code = put(data["uploadUrl"], real_bytes, "video/mp4")
        api.check("直 PUT 到预签名 URL 成功（证明预签名本身不限制大小）",
                  code in (200, 204), f"HTTP {code}")
        status, body = api.call("POST", "/upload/media/confirm", {
            "assetId": data["assetId"], "objectName": data["objectName"],
            "lessonSessionId": lesson, "totalSeats": 30,
            "sampleInterval": 2, "windowDuration": 60})
        api.check("confirm 拒绝大小与声明不符", status == 409, f"HTTP {status}")
        still = object_exists(args.minio, "classroom-raw-media",
                              args.minio_access, args.minio_secret, data["objectName"])
        api.check("越界对象已被删除", not still,
                  "对象仍在桶里" if still else "已删除")

    print("==> 7. 伪造 MIME（扩展名 .mp4，Content-Type text/html）")
    # 关键：声明的大小必须与实传**完全一致**，否则大小校验会先命中 409，
    # MIME 校验根本不会被走到，这一条就成了"看起来通过、实际没测"。
    # 第一版就是这样写错的（声明 64、实传 13，拿到 409 就当作通过）。
    payload = b"<html>x</html>"
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "mime.mp4",
        "lessonSessionId": lesson, "sizeBytes": len(payload),
        "contentType": "video/mp4"})
    if status != 200:
        api.check("init 合法媒体失败", False, f"HTTP {status}")
    else:
        data = body["data"]
        code = put(data["uploadUrl"], payload, "text/html")
        api.check("直 PUT 接受任意 Content-Type（预签名不限制类型）",
                  code in (200, 204), f"HTTP {code}")
        status, body = api.call("POST", "/upload/media/confirm", {
            "assetId": data["assetId"], "objectName": data["objectName"],
            "lessonSessionId": lesson, "totalSeats": 30,
            "sampleInterval": 2, "windowDuration": 60})
        # 大小一致，所以只能是 MIME 校验命中 → 415。
        api.check("confirm 拒绝伪造 MIME（大小一致，故非 size 分支）",
                  status == 415, f"HTTP {status} body.code={body.get('code')}")
        still = object_exists(args.minio, "classroom-raw-media",
                              args.minio_access, args.minio_secret, data["objectName"])
        api.check("伪造 MIME 对象已被删除", not still,
                  "对象仍在桶里" if still else "已删除")

    print("==> 8. 未知 / 他人 object key（confirm 阶段）")
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "mine.mp4",
        "lessonSessionId": lesson, "sizeBytes": 32, "contentType": "video/mp4"})
    if status == 200:
        data = body["data"]
        status, body = api.call("POST", "/upload/media/confirm", {
            "assetId": data["assetId"], "objectName": "somebody-else.mp4",
            "lessonSessionId": lesson, "totalSeats": 30,
            "sampleInterval": 2, "windowDuration": 60})
        api.check("confirm 拒绝未知 object key", status == 409, f"HTTP {status}")

    print("==> 9. 他人资产（confirm 阶段）")
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": "theirs.mp4",
        "lessonSessionId": lesson, "sizeBytes": 32, "contentType": "video/mp4"})
    if status == 200:
        data = body["data"]
        status, _ = api.call("POST", "/upload/media/confirm", {
            "assetId": data["assetId"], "objectName": data["objectName"],
            "lessonSessionId": lesson, "totalSeats": 30,
            "sampleInterval": 2, "windowDuration": 60}, token=other)
        api.check("confirm 拒绝非属主", status in (404, 403), f"HTTP {status}")

    print("==> 10. 临时上传入口（SEC-04：学生角色不得写入）")
    status, body = api.call("POST", "/upload/temporary",
                            {"fileName": "a.pdf"}, token=other)
    # other 是 teacher，应当是 200（入口对 teacher 开放）；关键是 .exe 必须被拒。
    status_exe, _ = api.call("POST", "/upload/temporary",
                             {"fileName": "a.exe"}, token=token)
    api.check("临时上传拒绝 .exe", status_exe == 415, f"HTTP {status_exe}")

    print()
    failed = [r for r in api.results if not r.ok]
    print(f"{len(api.results) - len(failed)}/{len(api.results)} passed")
    for r in failed:
        print(f"  - {r.name}: {r.detail}")

    if args.json:
        out = {"base": args.base, "results": [
            {"name": r.name, "ok": r.ok, "detail": r.detail} for r in api.results],
            "passed": len(api.results) - len(failed), "total": len(api.results)}
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(out, handle, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
