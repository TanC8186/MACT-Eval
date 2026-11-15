#!/usr/bin/env python
"""MinIO POST policy 能否在**上传期**拒绝超大对象？—— 实测（B3.3）。

    python -I scripts/verify/probe_post_policy_limit.py [--json <path>]

### 为什么必须做这个实验

`MediaAssetService` 的注释与交付报告里写着"MinIO 的 Java SDK 8.5.12 的
`GetPresignedObjectUrlArgs` 只支持 method/bucket/object/expiry，没有
content-length-range"。**这句话只对 GET/PUT 预签名成立，对 POST policy 不成立**：

```
$ javap -classpath minio-8.5.12.jar io.minio.PostPolicy
  public void addContentLengthRangeCondition(long, long);
  public void addContentLengthRangeCondition(int, int);
$ javap -classpath minio-8.5.12.jar io.minio.MinioClient
  public Map<String,String> getPresignedPostFormData(io.minio.PostPolicy)
```

Python SDK 同样有 `PostPolicy.add_content_length_range_condition`。所以"SDK 不支持"
是一个**错的理由**——原结论建立在读错了一个类上。

### 两个可判定的问题

1. 带 `content-length-range` 的 POST policy，超限时 HTTP 会不会拒（403 EntityTooLarge）？
2. 拒掉之后，字节有没有真的落到磁盘（`stat_object` 查不到对象）？

对照：同一份字节用**预签名 PUT**（当前生产路径）再传一次。

### 三个必须写进输出的"坑"（第一版全踩了）

* `PostPolicy.form_data()` 返回的只有签名相关字段，**不含 `key`**。
  不显式补 `key` 时 MinIO 回 `MalformedPOSTRequest: The name of the uploaded key
  is missing` —— 那是**请求格式错**，不是"超限被拒"。把它读成"policy 生效"就是假绿。
* `classroom-raw-media` 是私有桶，匿名 PUT 一律 403。对照实验必须用**后端签发的
  预签名 URL**，否则 403 会被误读成"上传期约束生效"。
* 因此本脚本要求后端在 :8000 可用（拿预签名 URL）；不可用时返回 2，不猜。

### 边界

* 只测 8 MiB vs 1 KiB 上限。2 GiB 是同一路径的推论，不是实测。
* 不修改应用代码，也不改变当前上传路径 —— 它只让"要不要改成 POST form 上传"
  这个决定建立在实测上。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def api(base: str, method: str, path: str, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, base + path, json=body, headers=headers, timeout=60)
    return resp.status_code, (resp.json() if resp.content else {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000/api")
    ap.add_argument("--minio", default="http://127.0.0.1:19100")
    ap.add_argument("--access", default="uirig")
    ap.add_argument("--secret", default="uirig_secret")
    ap.add_argument("--bucket", default="classroom-raw-media")
    ap.add_argument("--limit", type=int, default=1024)
    ap.add_argument("--actual", type=int, default=8 * 1024 * 1024)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    from minio import Minio
    from minio.datatypes import PostPolicy
    from minio.error import S3Error

    try:
        st, _ = api(args.base, "GET", "/health")
        if st != 200:
            raise RuntimeError(f"health HTTP {st}")
    except Exception as exc:  # noqa: BLE001
        print(f"!! 后端不可达（需要它签预签名 URL 做对照）：{exc}", file=sys.stderr)
        return 2

    host = args.minio.replace("http://", "").replace("https://", "")
    client = Minio(host, access_key=args.access, secret_key=args.secret, secure=False)
    if not client.bucket_exists(args.bucket):
        print(f"!! 桶不存在：{args.bucket}", file=sys.stderr)
        return 2

    payload = b"\0" * args.actual
    suffix = str(int(time.time()))[-8:]

    # ---- 建账号与课堂，拿后端签发的预签名 PUT（当前生产路径的对照） ----
    st, body = api(args.base, "POST", "/auth/register", {
        "username": f"policy-{suffix}", "password": "Policy-Probe-Pw-1",
        "name": "policy", "role": "teacher"})
    if st != 200:
        print(f"!! 注册失败 HTTP {st}: {body}", file=sys.stderr)
        return 2
    token = body["data"]["token"]
    st, body = api(args.base, "POST", "/courses",
                   {"name": f"POL-{suffix}", "code": f"POL{suffix}", "department": "t"},
                   token)
    course = body["data"]["id"]
    st, body = api(args.base, "POST", "/sessions",
                   {"courseId": course, "classId": "class-001",
                    "title": f"POL-L-{suffix}"}, token)
    lesson = body["data"]["id"]

    # ---------------------------------------------------------------- 1) POST policy
    key_policy = f"policy-limit-{uuid.uuid4()}.mp4"
    policy = PostPolicy(args.bucket, datetime.now(timezone.utc) + timedelta(minutes=15))
    policy.add_content_length_range_condition(1, args.limit)
    policy.add_equals_condition("key", key_policy)
    creds = client._provider.retrieve()  # noqa: SLF001 - SDK 没有公开入口
    form = policy.form_data(creds, "us-east-1")
    # **必须补 key**：form_data 只给签名相关字段（实测 keys=[
    # x-amz-algorithm, x-amz-credential, x-amz-date, policy, x-amz-signature]）。
    form["key"] = key_policy
    post = requests.post(f"{args.minio}/{args.bucket}", data=form,
                         files={"file": ("blob", payload, "video/mp4")}, timeout=300)

    stored_policy = None
    try:
        stored_policy = client.stat_object(args.bucket, key_policy).size
    except S3Error:
        stored_policy = None

    # ---------------------------------------------------------------- 2) 对照：预签名 PUT
    st, body = api(args.base, "POST", "/upload/media", {
        "mediaType": "video", "fileName": "control.mp4", "lessonSessionId": lesson,
        "sizeBytes": args.actual, "contentType": "video/mp4"}, token)
    if st != 200:
        print(f"!! init 失败 HTTP {st}: {body}", file=sys.stderr)
        return 2
    presigned_url = body["data"]["uploadUrl"]
    key_put = body["data"]["objectName"]
    put = requests.put(presigned_url, data=payload,
                       headers={"Content-Type": "video/mp4"}, timeout=300)
    stored_put = None
    try:
        stored_put = client.stat_object(args.bucket, key_put).size
    except S3Error:
        stored_put = None

    # ---------------------------------------------------------------- 清理
    for key in (key_policy, key_put):
        try:
            client.remove_object(args.bucket, key)
        except S3Error:
            pass

    # ---------------------------------------------------------------- 判定
    post_rejected = post.status_code >= 400
    post_is_entity_too_large = "EntityTooLarge" in (post.text or "")
    post_malformed = "MalformedPOSTRequest" in (post.text or "")
    post_disk = stored_policy is not None
    put_disk = stored_put == args.actual

    if post_malformed:
        verdict = (
            "**实验无效**：POST 被拒是 `MalformedPOSTRequest`（表单字段缺失），"
            "不是超限拒绝。不能据此声称 policy 生效。")
        ok = False
    elif post_rejected and not post_disk:
        verdict = (
            f"POST policy 在**上传期**拒绝超限对象（HTTP {post.status_code}"
            f"{'，EntityTooLarge' if post_is_entity_too_large else ''}），"
            "且字节未落盘。→ 把直传改成 POST form 上传，就能把'事后删除'变成"
            "'上传期拦截'。")
        ok = True
    else:
        verdict = (f"POST policy 未拒绝超限对象（HTTP {post.status_code}，"
                   f"桶内 {stored_policy}）。→ 该构造路径不生效，需改服务端签名。")
        ok = False

    payload_out = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": f"实测 {args.actual} B；2 GiB 为同一路径的推论，不是实测",
        "postPolicy": {
            "limitBytes": args.limit,
            "actualBytes": args.actual,
            "httpStatus": post.status_code,
            "rejectedAsTooLarge": post_is_entity_too_large,
            "malformedRequest": post_malformed,
            "storedBytesAfterPost": stored_policy,
            "bytesReachedDisk": post_disk,
            "responseSnippet": (post.text or "")[:300],
        },
        "presignedPutControl": {
            "httpStatus": put.status_code,
            "storedBytes": stored_put,
            "bytesReachedDisk": put_disk,
            "note": "当前生产路径：预签名 PUT，无上传期大小约束。",
        },
        "verdict": verdict,
        "passed": ok,
    }
    if args.json:
        open(args.json, "w", encoding="utf-8").write(
            json.dumps(payload_out, ensure_ascii=False, indent=2))

    pp = payload_out["postPolicy"]
    print(f"  POST policy（limit {args.limit} B / 实发 {args.actual} B）"
          f"→ HTTP {pp['httpStatus']}，桶内 {pp['storedBytesAfterPost']}")
    print(f"  预签名 PUT 对照 → HTTP {payload_out['presignedPutControl']['httpStatus']}，"
          f"桶内 {payload_out['presignedPutControl']['storedBytes']}")
    print(f"\n  结论：{verdict}")
    if args.json:
        print(f"  结果写入 {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
