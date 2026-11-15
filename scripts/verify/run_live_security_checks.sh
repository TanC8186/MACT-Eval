#!/usr/bin/env bash
# 活栈安全验收（SEC-01/02/03/04，2026-10-28）。
#
#   bash scripts/verify/run_live_security_checks.sh \
#       --base http://127.0.0.1:8000/api \
#       --ws   ws://127.0.0.1:8000/api/ws/classroom \
#       --minio http://127.0.0.1:19100
#
# 为什么单独一份、不塞进 verify_all.sh：
# 这三项都必须对着**跑起来的后端**做（真实 HTTP 栈、真实 Redis、真实 MinIO）。
# verify_all.sh 的定位是"不需要起服务也能判定的门禁"，把需要活栈的检查塞进去，
# 会让它在没起服务时只能 skip，而 skip 会让退出码变成 3——那个 3 本该表示
# "有必检项没跑"，不该被"这一档本来就不跑"稀释。
#
# 前置：
#   1. 后端与 ai-service 已启动（本机可用 python -I scripts/verify/browser_rig.py start，
#      但那个脚本要求 5173 空闲；若只需 API，直接 java -jar 起后端即可）；
#   2. rig_accounts.py 已跑过，且导出了 MACT_E2E_STUDENT_USER/PASS 与
#      MACT_E2E_ADMIN_USER/PASS（ws_auth.py 与 acl_matrix.py 需要学生/管理员账号）；
#   3. **手工起后端时必须照抄 `browser_rig.backend_env()` 的变量**，尤其
#      `MACT_MINIO_PUBLIC_ENDPOINT`。漏掉它预签名 URL 会指向默认 localhost:9000，
#      直传 PUT 全 403 —— 症状看起来像"直传校验回归"，实际是起服务的环境不全
#      （第四轮真实踩过，见 execution 文档 §14.5）。
#
# 退出码：0 全通过；1 有失败；2 环境不可用；3 有跳过（不计为通过）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BASE="http://127.0.0.1:8000/api"
WS="ws://127.0.0.1:8000/api/ws/classroom"
MINIO="http://127.0.0.1:19100"
MINIO_ACCESS="${MACT_MINIO_ACCESS_KEY:-uirig}"
MINIO_SECRET="${MACT_MINIO_SECRET_KEY:-uirig_secret}"
OUT_DIR="$ROOT/docs/reviews/evidence"
STAMP="$(date +%Y%m%dT%H%M)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --ws) WS="$2"; shift 2 ;;
    --minio) MINIO="$2"; shift 2 ;;
    --minio-access) MINIO_ACCESS="$2"; shift 2 ;;
    --minio-secret) MINIO_SECRET="$2"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT_DIR"

CACHE="$ROOT/.cache"
export TMPDIR="$CACHE/tmp" TEMP="$CACHE/tmp" TMP="$CACHE/tmp"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$CACHE/tmp/pycache"
mkdir -p "$TMPDIR" "$PYTHONPYCACHEPREFIX"

VERIFY_PY="${VERIFY_PYTHON:-$ROOT/.venv-verify/Scripts/python.exe}"
[[ -x "$VERIFY_PY" ]] || VERIFY_PY="$ROOT/.venv-verify/bin/python"
if [[ ! -x "$VERIFY_PY" ]]; then
  echo "!! 未找到 .venv-verify（bash scripts/verify/setup_verify_env.sh）" >&2
  exit 2
fi

# 前置探测：后端必须活着。不做这一步的话，后面三个脚本会各自以
# "连接被拒"失败，看起来像三个独立缺陷。
if ! curl -fsS -o /dev/null --max-time 5 "${BASE}/health" 2>/dev/null; then
  echo "!! 后端不可达：${BASE}/health" >&2
  echo "   先起后端（java -jar backend/target/mact-eval-backend-1.0.0.jar 或 rig start）" >&2
  exit 2
fi

FAILED=0
SKIPPED=0
SUMMARY=()

run() { # run <label> <cmd...>
  local label="$1"; shift
  echo
  echo "==> $label"
  local code=0
  "$@" || code=$?
  case "$code" in
    0) SUMMARY+=("PASS  $label") ;;
    3) SUMMARY+=("SKIP  $label（前置缺失，不计为通过）"); SKIPPED=$((SKIPPED+1)) ;;
    *) SUMMARY+=("FAIL  $label（exit=$code）"); FAILED=$((FAILED+1)) ;;
  esac
  return 0
}

run "API 安全响应头（实测）" \
  "$VERIFY_PY" -I scripts/verify/capture_security_headers.py \
    --base "$BASE" --out "$OUT_DIR/security-headers-$STAMP.json"

# §B4.4 敏感字段扫描。放在活栈检查里而不是 verify_all.sh：它要看的是
#「刚跑完上面这些检查之后」产生的日志，而 verify_all.sh 不产生这些日志。
run "敏感字段日志扫描（真实凭据 0 命中）" \
  "$VERIFY_PY" -I scripts/verify/scan_log_secrets.py \
    --json-out "$OUT_DIR/log-secret-scan-$STAMP.json"

run "WebSocket 一次性票据（11 项，含过期/跨课堂/重放/伪造/旧写法）" \
  "$VERIFY_PY" -I scripts/verify/ws_auth.py --base "$BASE" --ws "$WS" --check-expiry

run "直传绕过测试（14 项，含超大/伪造 MIME/错误课堂/未知 key）" \
  "$VERIFY_PY" -I scripts/verify/upload_bypass_test.py \
    --base "$BASE" --minio "$MINIO" \
    --minio-access "$MINIO_ACCESS" --minio-secret "$MINIO_SECRET" \
    --json "$OUT_DIR/upload-bypass-$STAMP.json"

# 下面两项对应 2026-10-29-closure-implementation-review.md §4 里"未能验证"的条目：
# 单测用 Mockito 打桩 ObjectProbe、用 ApplicationContextRunner 绑属性，二者都不证明
# 真实 MinIO/真实进程的行为。它们需要活栈，所以放在这里而不是 verify_all.sh。
run "权限矩阵（23 项，含 refresh 轮换与跨教师/跨班越权）" \
  "$VERIFY_PY" -I scripts/verify/acl_matrix.py --base "$BASE"

run "删除报告连带删除证据帧（真实 MinIO + 真实库）" \
  "$VERIFY_PY" -I scripts/verify/verify_delete_evidence_live.py \
    --base "$BASE" --minio "$MINIO" \
    --minio-access "$MINIO_ACCESS" --minio-secret "$MINIO_SECRET" \
    --json "$OUT_DIR/delete-evidence-live-$STAMP.json"

echo
echo "================ 活栈安全验收摘要 ================"
for line in "${SUMMARY[@]}"; do
  printf '  %s\n' "$line"
done
echo "=================================================="
if (( FAILED > 0 )); then
  echo "  RESULT: FAILED（$FAILED 项）"
  exit 1
fi
if (( SKIPPED > 0 )); then
  echo "  RESULT: INCOMPLETE（$SKIPPED 项未执行，不得当作通过）"
  exit 3
fi
echo "  RESULT: PASSED"
