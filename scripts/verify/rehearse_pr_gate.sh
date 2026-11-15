#!/usr/bin/env bash
# 在本机按 pr-gate.yml 的**步骤顺序**执行同样的命令（C1 的可行替代）。
#
#   bash scripts/verify/rehearse_pr_gate.sh
#
# 为什么需要它：任务书 §5 C1 要求"PR CI 至少真实运行一次"。真实运行需要把提交
# 推到 GitHub（本机 HEAD 比远端分支领先 1 个提交，工作区还有 90 个修改 + 153 个
# 新文件未提交），而推送需要用户授权，且会把未提交的改动暴露在远端。
#
# 在拿到授权之前，能做的最诚实的替代是：**在本机把 workflow 的每条命令按同样
# 顺序跑一遍**，记录哪一步与本机环境不兼容。这证明的是"命令本身可执行"，
# **不**证明"GitHub runner 上会绿"——后者受 runner 镜像、缓存、services 影响。
# 报告里必须区分这两件事。
#
# 已知会与本机不同的三处（脚本会显式标出来，不静默跳过）：
#   1. `docker compose config` —— 本机可用，runner 上需 Docker；
#   2. `pnpm --store-dir` —— 本机用 E 盘路径，workflow 用 $GITHUB_WORKSPACE；
#   3. ai-service 的快速集按 workflow 同样按测试文件选择，而不是使用容易
#      漏测服务令牌鉴权的 `-k` 关键字；需要装 requirements-test.txt。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERIFY_PY="${VERIFY_PYTHON:-$ROOT/.venv-verify/Scripts/python.exe}"
[[ -x "$VERIFY_PY" ]] || VERIFY_PY="$ROOT/.venv-verify/bin/python"
CACHE="$ROOT/.cache"
export TMPDIR="$CACHE/tmp" TEMP="$CACHE/tmp" TMP="$CACHE/tmp"
export CI=true
export npm_config_store_dir="$CACHE/pnpm"

OUT="$ROOT/docs/reviews/evidence/pr-gate-rehearsal-$(date +%Y%m%dT%H%M).json"
RESULTS=()

step() { # step <job> <name> <cmd...>
  local job="$1" name="$2"; shift 2
  printf '\n=== [%s] %s\n' "$job" "$name"
  local started code=0
  started=$(date +%s)
  "$@" > "$CACHE/tmp/rehearse-step.log" 2>&1 || code=$?
  local secs=$(( $(date +%s) - started ))
  tail -4 "$CACHE/tmp/rehearse-step.log" | sed 's/^/    /'
  RESULTS+=("{\"job\":\"$job\",\"step\":\"$name\",\"exit\":$code,\"seconds\":$secs}")
  if [[ "$code" -eq 0 ]]; then
    printf '  -> PASS (%ss)\n' "$secs"
  else
    printf '  -> FAIL exit=%s (%ss)\n' "$code" "$secs"
  fi
}

# ---- job: static ----
step static "单端范围检查" node scripts/verify/check-single-web-scope.mjs
step static "单端范围门禁自测" node scripts/verify/check-single-web-scope.mjs --self-test
step static "安全契约（含变异自测）" "$VERIFY_PY" -I scripts/verify/test_security_headers.py --self-test
step static "磁盘门禁自测" "$VERIFY_PY" -I scripts/verify/check_disk_policy.py --self-test
step static "compose 语法（正式）" docker compose config --quiet
step static "compose 语法（容量叠加）" env \
  MACT_JWT_SECRET=config-check-only-placeholder-0000000000 \
  MACT_SERVICE_TOKEN=config-check-only-placeholder-0000000000 \
  docker compose -f docker-compose.yml -f docker-compose.capacity-cpu.yml config --quiet
step static "无空白错误" git diff --check

# ---- job: backend ----
step backend "mvn test" mvn -B -q -f backend/pom.xml -Dmaven.repo.local="$CACHE/maven" test

# ---- job: python ----
step python "public-data tests" "$VERIFY_PY" -m pytest tools/public_data/tests -q -p no:cacheprovider
step python "ops tests" "$VERIFY_PY" -m pytest scripts/ops/tests -q -p no:cacheprovider
step python "verify tests" "$VERIFY_PY" -m pytest scripts/verify/tests -q -p no:cacheprovider
step python "ai-service 快速集（按 workflow 文件选择）" bash -c \
  'cd ai-service && .venv-test/Scripts/python.exe -m pytest \
    tests/test_cors_single_web.py \
    tests/test_api.py \
    tests/test_readiness.py \
    tests/test_status_capabilities.py \
    tests/test_native_dll_order.py \
    -q -p no:cacheprovider'

# ---- job: frontend ----
step frontend "typecheck" pnpm --config.storeDir="$CACHE/pnpm" typecheck:web
step frontend "production build" pnpm --config.storeDir="$CACHE/pnpm" build:web
step frontend "auth-session 回归" bash -c \
  'cd frontend/apps/teacher-web && node --experimental-strip-types --no-warnings tests/auth-session.test.mjs'
step frontend "evidence-gallery 回归" node --experimental-strip-types --no-warnings \
  frontend/apps/teacher-web/tests/evidence-gallery.test.mjs
step frontend "report-versions 回归" node --experimental-strip-types --no-warnings \
  frontend/apps/teacher-web/tests/report-versions.test.mjs
step frontend "产物级 SEC-02 断言（无 &token=）" bash -c \
  'if grep -rIl "&token=" frontend/apps/teacher-web/dist/assets/ 2>/dev/null; then exit 1; fi; echo "OK: 无 &token="'
step frontend "产物级 SEC-02 断言（含 ws-ticket）" bash -c \
  'grep -rIl "ws-ticket" frontend/apps/teacher-web/dist/assets/ >/dev/null || exit 1; echo "OK: 含 ws-ticket"'

printf '{"generatedAt":"%s","scope":"本机按 pr-gate.yml 顺序执行同样的命令；未在 GitHub runner 上运行","steps":[%s]}\n' \
  "$(date -Iseconds)" "$(IFS=,; echo "${RESULTS[*]}")" > "$OUT"

fails=$(printf '%s\n' "${RESULTS[@]}" | grep -c '"exit":[1-9]')
echo
echo "================ 彩排摘要 ================"
echo "  步骤: ${#RESULTS[@]}   失败: $fails"
echo "  结果: $OUT"
if [[ "$fails" -gt 0 ]]; then
  echo "  RESULT: FAILED"
  exit 1
fi
echo "  RESULT: PASSED（本机可执行性已证明；runner 环境仍未经证明）"
