#!/usr/bin/env bash
# HTTP/WS integration gate against an already-running isolated backend.
#
# Prerequisites:
#   - backend started with MACT_DEMO_ENABLED=true once to seed admin/student,
#     then restarted with MACT_DEMO_ENABLED=false for REAL report assertions;
#   - database migrated to the latest Flyway version;
#   - Redis/MinIO reachable by that backend.
#
#   BASE_URL=http://localhost:18001/api \
#   WS_URL=ws://localhost:18001/api/ws/classroom \
#   bash scripts/verify/verify_integration.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BASE_URL="${BASE_URL:-http://localhost:18001/api}"
WS_URL="${WS_URL:-ws://localhost:18001/api/ws/classroom}"
SERVICE_TOKEN="${SERVICE_TOKEN:-mact-eval-dev-token}"

echo "==> permission matrix"
python scripts/verify/acl_matrix.py --base "$BASE_URL"

echo "==> websocket authentication"
python scripts/verify/ws_auth.py --base "$BASE_URL" --ws "$WS_URL"

echo "==> authentication hardening"
python scripts/verify/auth_security.py --base "$BASE_URL"

echo "==> business loop"
python scripts/verify/business_flow.py --base "$BASE_URL" --service-token "$SERVICE_TOKEN"

echo "INTEGRATION CHECKS PASSED"
