#!/usr/bin/env bash
# Prod-profile startup smoke (OPS01/OPS06).
#
# Creates an isolated PostgreSQL database, starts the packaged backend with
# `--spring.profiles.active=prod`, verifies JSON logs + requestId propagation,
# then stops the process and drops the database. Requires the dev Docker
# postgres/redis containers to be running.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PG_CONTAINER="${PG_CONTAINER:-personal_design-postgres-1}"
REDIS_DB="${REDIS_DB:-13}"
PORT="${PORT:-18080}"
DB="macteval_smoke_$$"
# 临时文件必须落在项目内：系统 TEMP 在 C 盘，本项目禁止写那里。
# 回退值改成项目内目录，而不是 /tmp（Git Bash 下 /tmp 就是 C 盘 TEMP）。
TMPDIR="${TMPDIR:-$ROOT/.cache/tmp}"
export TMPDIR
mkdir -p "$TMPDIR"
LOG="$TMPDIR/mact-prod-smoke-$$.log"
JAR="backend/target/mact-eval-backend-1.0.0.jar"
PID=""

cleanup() {
  if [[ -n "$PID" ]]; then
    kill "$PID" 2>/dev/null || true
    sleep 1
    taskkill //PID "$PID" //F >/dev/null 2>&1 || true
  fi
  docker exec "$PG_CONTAINER" psql -U macteval -d postgres \
    -c "DROP DATABASE IF EXISTS $DB;" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[prod-smoke] packaging backend"
mvn -q -f backend/pom.xml package -DskipTests

echo "[prod-smoke] creating isolated database $DB"
docker exec "$PG_CONTAINER" psql -U macteval -d postgres -c "CREATE DATABASE $DB;" >/dev/null

echo "[prod-smoke] starting prod profile on :$PORT"
MACT_JWT_SECRET="prod-smoke-secret-0123456789abcdef-0123456789" \
MACT_SERVICE_TOKEN="prod-smoke-service-token" \
MACT_DB_URL="jdbc:postgresql://localhost:5432/$DB" \
MACT_DB_USERNAME=macteval MACT_DB_PASSWORD=macteval \
SPRING_DATA_REDIS_DATABASE="$REDIS_DB" \
MACT_DEMO_ENABLED=false MACT_MINIO_ENABLED=true \
java -jar "$JAR" --spring.profiles.active=prod --server.port="$PORT" >"$LOG" 2>&1 &
PID=$!

for _ in $(seq 1 90); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/api/health" || true)"
  if [[ "$code" == "200" ]]; then break; fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "[prod-smoke] process exited during startup" >&2
    tail -30 "$LOG" >&2
    exit 1
  fi
  sleep 1
done

if [[ "${code:-}" != "200" ]]; then
  echo "[prod-smoke] health check did not become ready" >&2
  tail -30 "$LOG" >&2
  exit 1
fi

if grep -qiE "mdcKeyFieldName|Logback configuration error|Application run failed" "$LOG"; then
  echo "[prod-smoke] logback/startup error detected" >&2
  grep -iE "mdcKeyFieldName|Logback configuration error|Application run failed" "$LOG" >&2
  exit 1
fi

# requestId must appear in response header and structured JSON logs.
curl -s -D "$TMPDIR"/mact-prod-headers-$$ -o /dev/null \
  -H 'X-Request-Id: prod-smoke-request' \
  -H 'Authorization: Bearer invalid' \
  "http://localhost:$PORT/api/courses" || true
grep -qi 'X-Request-Id: prod-smoke-request' "$TMPDIR"/mact-prod-headers-$$ || {
  echo "[prod-smoke] response header missing X-Request-Id" >&2
  exit 1
}
rm -f "$TMPDIR"/mact-prod-headers-$$
sleep 1
grep -q '"requestId":"prod-smoke-request"' "$LOG" || {
  echo "[prod-smoke] JSON log does not contain requestId" >&2
  exit 1
}
grep -q '"service":"mact-eval-backend"' "$LOG" || {
  echo "[prod-smoke] JSON log missing service field" >&2
  exit 1
}

echo "[prod-smoke] OK: startup, JSON logs, requestId propagation"
