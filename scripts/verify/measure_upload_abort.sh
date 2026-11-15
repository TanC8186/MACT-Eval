#!/usr/bin/env bash
# 上传阶段（而非 confirm 阶段）真的会发生什么 —— B3.4 的实测（2026-10-28）。
#
#   bash scripts/verify/measure_upload_abort.sh
#
# 任务书 §B3.4：「测量超大对象失败后的删除延迟和临时存储峰值」。
# 本脚本用真实 rig（后端 :8000 + MinIO :19100）测三件事：
#
#   1. **预签名 PUT 不接受任何大小约束** —— 声明 1 KiB、实传 8 MiB 会被 MinIO
#      完整收下（HTTP 200）。这不是估计，是 bash+curl 直传的结果。
#   2. **对象在 confirm 之前就已经占满磁盘** —— 峰值 = 实传字节数。
#   3. **confirm 拒绝后到对象真正消失的延迟** —— 反复 statObject 直到 NOT_FOUND，
#      记录毫秒数。这是"风险窗口"的实际长度。
#
# 边界：本脚本只测 8 MiB，不测 2 GiB。理由写在输出里：2 GiB 的单次上传在
# 本机约需 20+ 秒并且会真实写入 E 盘 2 GiB（可回收，但没必要为一条已被证实的
# 结论再付一次）。**结论对 2 GiB 成立是推论，不是实测** —— 这点必须说清。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BASE="${MACT_BASE:-http://127.0.0.1:8000/api}"
MINIO_HOST="${MACT_MINIO_HOST:-127.0.0.1:19100}"
MINIO_ACCESS="${MACT_MINIO_ACCESS_KEY:-uirig}"
MINIO_SECRET="${MACT_MINIO_SECRET_KEY:-uirig_secret}"
VERIFY_PY="${VERIFY_PYTHON:-$ROOT/.venv-verify/Scripts/python.exe}"
[[ -x "$VERIFY_PY" ]] || VERIFY_PY="$ROOT/.venv-verify/bin/python"
OUT="$ROOT/docs/reviews/evidence/upload-abort-$(date +%Y%m%dT%H%M).json"

export TMPDIR="$ROOT/.cache/tmp" TEMP="$ROOT/.cache/tmp" TMP="$ROOT/.cache/tmp"

if ! curl -fsS -o /dev/null --max-time 5 "$BASE/health"; then
  echo "!! 后端不可达：$BASE/health" >&2; exit 2
fi

"$VERIFY_PY" - "$BASE" "$MINIO_HOST" "$MINIO_ACCESS" "$MINIO_SECRET" "$OUT" <<'PYEOF'
import json
import sys
import time
import uuid

import requests
from minio import Minio
from minio.error import S3Error

base, minio_host, access, secret, out_path = sys.argv[1:6]
PASSWORD = "Abort-Probe-Pw-1"
suffix = str(int(time.time()))[-8:]
DECLARED = 1024                 # 声明 1 KiB
ACTUAL = 8 * 1024 * 1024        # 实传 8 MiB


def api(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.request(method, base + path, json=body, headers=headers, timeout=60)
    return r.status_code, (r.json() if r.content else {})


st, body = api("POST", "/auth/register", {
    "username": f"abort-{suffix}", "password": PASSWORD, "name": "abort", "role": "teacher"})
if st != 200:
    sys.exit(f"register failed HTTP {st}: {body}")
token = body["data"]["token"]

st, body = api("POST", "/courses",
               {"name": f"ABORT-{suffix}", "code": f"AB{suffix}", "department": "t"}, token)
course = body["data"]["id"]
st, body = api("POST", "/sessions",
               {"courseId": course, "classId": "class-001", "title": f"ABORT-L-{suffix}"}, token)
lesson = body["data"]["id"]

st, body = api("POST", "/upload/media", {
    "mediaType": "video", "fileName": "abort.mp4", "lessonSessionId": lesson,
    "sizeBytes": DECLARED, "contentType": "video/mp4"}, token)
if st != 200:
    sys.exit(f"init failed HTTP {st}: {body}")
data = body["data"]
asset_id, object_name, upload_url = data["assetId"], data["objectName"], data["uploadUrl"]

client = Minio(minio_host, access_key=access, secret_key=secret, secure=False)
payload = b"\0" * ACTUAL

# ---- 1. 预签名 PUT 接受超过声明 8192 倍的载荷 ----
put_start = time.monotonic()
put = requests.put(upload_url, data=payload, headers={"Content-Type": "video/mp4"},
                   timeout=300)
put_seconds = time.monotonic() - put_start

stat = client.stat_object("classroom-raw-media", object_name)
peak_bytes = stat.size

# ---- 2. confirm 拒绝 ----
declined_at = time.monotonic()
st2, body2 = api("POST", "/upload/media/confirm", {
    "assetId": asset_id, "objectName": object_name, "lessonSessionId": lesson,
    "totalSeats": 30, "sampleInterval": 2, "windowDuration": 60}, token)
confirm_seconds = time.monotonic() - declined_at

# ---- 3. 对象真正消失的延迟 ----
gone_after = None
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        client.stat_object("classroom-raw-media", object_name)
    except S3Error:
        gone_after = time.monotonic() - declined_at
        break
    time.sleep(0.05)

payload_out = {
    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "scope": f"实测 {ACTUAL} 字节；2 GiB 为推论不是实测",
    "declaredBytes": DECLARED,
    "actualBytes": ACTUAL,
    "putHttpStatus": put.status_code,
    "putSeconds": round(put_seconds, 3),
    "storedBytesBeforeConfirm": peak_bytes,
    "bytesOverDeclarationFactor": round(peak_bytes / DECLARED, 1),
    "putAcceptedDespiteCap": put.status_code in (200, 201, 204),
    "confirmHttpStatus": st2,
    "confirmSeconds": round(confirm_seconds, 3),
    "objectGoneAfterSeconds": (round(gone_after, 3) if gone_after is not None else None),
    "conclusion": (
        "预签名 PUT 不含 content-length-range：MinIO 完整收下了超过声明值的大对象，"
        "所以 confirm 阶段的校验是**事后**的，不是上传期拦截。"
        "临时存储峰值 = 实传字节数（本次 "
        f"{peak_bytes:,} B）。confirm 拒绝后对象在 "
        f"{round(gone_after, 3) if gone_after is not None else '>30' } s 内消失。"),
}

if gone_after is None:
    payload_out["failures"] = ["对象在 confirm 拒绝后 30 秒内仍未消失"]
    payload_out["passed"] = False
else:
    payload_out["failures"] = []
    payload_out["passed"] = (put.status_code in (200, 201, 204)
                             and st2 == 409 and gone_after is not None)

open(out_path, "w", encoding="utf-8").write(
    json.dumps(payload_out, ensure_ascii=False, indent=2))

print(f"  声明 {DECLARED:,} B / 实传 {ACTUAL:,} B")
print(f"  PUT HTTP {put.status_code}（{put_seconds:.2f}s）→ 桶内 {peak_bytes:,} B "
      f"= 声明的 {peak_bytes / DECLARED:.0f} 倍")
print(f"  confirm HTTP {st2}（{confirm_seconds:.3f}s）")
print(f"  对象消失延迟: "
      f"{round(gone_after, 3) if gone_after is not None else '>30'} s")
print(f"  结果写入 {out_path}")
sys.exit(0 if payload_out["passed"] else 1)
PYEOF
