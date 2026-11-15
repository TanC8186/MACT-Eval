#!/usr/bin/env bash
# 磁盘路径门禁（Bash 入口，P0-1.4，2026-10-28）。
#
#   bash scripts/verify/check_disk_policy.sh
#   bash scripts/verify/check_disk_policy.sh --self-test
#   bash scripts/verify/check_disk_policy.sh --json docs/reviews/evidence/disk-policy.json
#
# 判定逻辑只有一份，在 check_disk_policy.py 里；本文件与 .ps1 都只是入口，
# 避免两个平台各写一套规则然后逐渐漂移（这正是本任务书要消除的那类问题）。
#
# 退出码：0 = 全部路径在项目内；1 = 有越界；2 = 前置条件缺失（找不到解释器）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# 解释器优先级：VERIFY_PYTHON -> .venv-verify -> 系统 python。
# 用 venv 优先是因为门禁自己也会导出 PATH 变量，系统解释器可能已经带上了
# C 盘的 TMP。
PY=""
if [[ -n "${VERIFY_PYTHON:-}" && -x "${VERIFY_PYTHON}" ]]; then
  PY="$VERIFY_PYTHON"
elif [[ -x "$ROOT/.venv-verify/Scripts/python.exe" ]]; then
  PY="$ROOT/.venv-verify/Scripts/python.exe"
elif [[ -x "$ROOT/.venv-verify/bin/python" ]]; then
  PY="$ROOT/.venv-verify/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY="$(command -v python)"
else
  echo "!! 找不到 Python 解释器（先跑 bash scripts/verify/setup_verify_env.sh）" >&2
  exit 2
fi

# 门禁自身的临时文件也必须落在项目内，否则它一边检查磁盘纪律一边写 C 盘。
export TMPDIR="$ROOT/.cache/tmp"
export TEMP="$ROOT/.cache/tmp"
export TMP="$ROOT/.cache/tmp"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$ROOT/.cache/tmp/pycache"
mkdir -p "$TMPDIR" "$PYTHONPYCACHEPREFIX"

echo "==> 磁盘门禁解释器: $PY"
exec "$PY" -I "$ROOT/scripts/verify/check_disk_policy.py" "$@"
