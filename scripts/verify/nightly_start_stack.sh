#!/usr/bin/env bash
# nightly E2E 的栈启动入口（P1-CI 第二层，2026-10-28）。
#
#   bash scripts/verify/nightly_start_stack.sh
#
# **这是为 CI 写的，不是本机门禁的一部分**。本机请用
# `python -I scripts/verify/browser_rig.py start`——它管的是隔离容器与固定端口。
#
# 为什么不能直接复用 browser_rig.py：
#   * 它用 `docker run` 起 mact-ui-* 容器、绑固定端口 15534/16380/19100，
#     并在 CI 上会尝试创建 Docker 资源——GitHub runner 允许，但容器里的
#     审计与资源清理会拖慢并且与 services: 块重复。
#   * 它的默认 Checkpoint 路径是 Windows 写法（E:\...），在 runner 上不存在。
#   * 它假定 5173 端口由 `vite dev` 提供；nightly 应当测**构建产物**，
#     用 `vite preview` 更接近线上。
#
# 本脚本的职责只有一件：让 browser_evidence_e2e.py 能对着
# http://127.0.0.1:5173 跑真实用例，并且后端连的是 services: 起的那三个库。
#
# 未验证声明：本脚本本轮**没有在 CI 上跑过**（没有可用的 Actions 环境，
# 也没有把仓库推上去的授权）。它在这里的作用是把 nightly 需要的外部契约
# 写成可执行的形状；首次真实 nightly 运行时必须按实际报错修正。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/data/tmp/nightly-logs"
mkdir -p "$LOG_DIR"

# 所有临时文件落项目内（CI 里就是 workspace），不用系统 /tmp。
export TMPDIR="$ROOT/.cache/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$TMPDIR"

# 固定测试用密钥。CI 是一次性环境，这两个值不用于任何真实数据；
# 但仍须满足 JwtTokenProvider 的 ≥32 字节检查，否则后端启动即失败。
export MACT_JWT_SECRET="${MACT_JWT_SECRET:-ci-only-jwt-secret-32-bytes-minimum-000}"
export MACT_SERVICE_TOKEN="${MACT_SERVICE_TOKEN:-ci-only-service-token-000}"

export MACT_DB_URL="${MACT_DB_URL:-jdbc:postgresql://127.0.0.1:5432/macteval_e2e}"
export MACT_DB_USERNAME="${MACT_DB_USERNAME:-macteval}"
export MACT_DB_PASSWORD="${MACT_DB_PASSWORD:-macteval}"
export SPRING_DATA_REDIS_HOST="${SPRING_DATA_REDIS_HOST:-127.0.0.1}"
export SPRING_DATA_REDIS_PORT="${SPRING_DATA_REDIS_PORT:-6379}"
export MACT_MINIO_ENDPOINT="${MACT_MINIO_ENDPOINT:-http://127.0.0.1:9000}"
export MACT_MINIO_PUBLIC_ENDPOINT="${MACT_MINIO_PUBLIC_ENDPOINT:-http://127.0.0.1:9000}"
export MACT_MINIO_ACCESS_KEY="${MACT_MINIO_ACCESS_KEY:-macteval}"
export MACT_MINIO_SECRET_KEY="${MACT_MINIO_SECRET_KEY:-macteval_secret}"
export MACT_AI_SERVICE_URL="${MACT_AI_SERVICE_URL:-http://127.0.0.1:8001}"
export MACT_DEMO_ENABLED=false
export MACT_AI_DEMO_ENABLED=false
export MACT_AI_REDIS_URL="redis://127.0.0.1:6379/0"
export MACT_AI_MINIO_ENDPOINT="127.0.0.1:9000"
export MACT_AI_BACKEND_URL="http://127.0.0.1:8000/api"
export MACT_AI_SERVICE_TOKEN="$MACT_SERVICE_TOKEN"
export MACT_AI_DEVICE=cpu
export MACT_AI_WHISPER_DEVICE=cpu
export MACT_AI_WHISPER_COMPUTE_TYPE=int8

echo "==> 打包后端（跳过测试，测试在 pr-gate 已跑）"
mvn -q -B -f backend/pom.xml -DskipTests package
JAR="$(ls -t backend/target/*.jar | grep -v original | head -1)"
echo "    jar: $JAR"

echo "==> 启动 backend :8000"
java -jar "$JAR" > "$LOG_DIR/backend-8000.log" 2>&1 &
echo $! > "$LOG_DIR/backend.pid"

echo "==> 启动 ai-service :8001（无 GPU，CPU/int8）"
(
  cd ai-service
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
) > "$LOG_DIR/ai-8001.log" 2>&1 &
echo $! > "$LOG_DIR/ai.pid"

wait_http() { # wait_http <url> <seconds> <name>
  local url="$1" deadline=$(( $(date +%s) + $2 )) name="$3"
  while [[ $(date +%s) -lt $deadline ]]; do
    if curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null; then
      echo "    $name ready"
      return 0
    fi
    sleep 2
  done
  echo "!! $name 未在 $2 秒内就绪；日志尾部：" >&2
  tail -30 "$LOG_DIR"/*.log >&2 || true
  return 1
}

wait_http "http://127.0.0.1:8000/api/health" 180 backend
wait_http "http://127.0.0.1:8001/ready" 240 ai-service

echo "==> 启动前端预览 :5173（测构建产物，不测 dev server）"
(
  cd frontend/apps/teacher-web
  npx vite preview --host 127.0.0.1 --port 5173 --strictPort
) > "$LOG_DIR/vite-5173.log" 2>&1 &
echo $! > "$LOG_DIR/vite.pid"

wait_http "http://127.0.0.1:5173/" 120 vite

echo "==> 栈就绪：backend 8000 / ai 8001 / web 5173"
cat <<'EOF'
注意：本脚本只负责起栈，不做数据准备。E2E 用例要求存在已发布的
报告与证据帧，nightly 必须先跑 python -I scripts/verify/browser_rig.py seed
（或等价夹具）。首次真实 nightly 运行时会暴露这一顺序问题——不要在这里
假装它已经解决。
EOF
