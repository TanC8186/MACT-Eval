#!/usr/bin/env bash
# 建立教师端真实浏览器验收环境（P0-3）。
#
#   bash scripts/verify/setup_browser_env.sh
#
# 硬性约束：所有下载、缓存、浏览器与临时文件都必须落在 E:/Personal_Design 下。
# 本脚本在动手前先把每个目标目录解析成绝对路径并断言在项目根内，
# 任何一条不在项目根下就直接退出——不允许"先下到 C 盘再移动"。
#
# 幂等：已存在的 venv 不重建（重建会丢掉已下载的 Chromium 绑定）；
# 依赖版本变了会 pip install 覆盖，Chromium 缺失时补装。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv-browser"
PLAYWRIGHT_HOME="$ROOT/.cache/ms-playwright"
PIP_CACHE="$ROOT/.cache/pip"
TMP_DIR="$ROOT/.cache/tmp"
REQ="$ROOT/scripts/verify/requirements-browser.txt"

# ---- 1. 路径审计（先断言，再动手） ----------------------------------------
assert_inside() {
  local p="$1"
  local resolved
  resolved="$(python -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$p")"
  case "$resolved" in
    "$ROOT"/*) printf 'OK    %s\n' "$resolved" ;;
    *) printf 'FAIL  %s resolves to %s (outside %s)\n' "$p" "$resolved" "$ROOT" >&2; exit 1 ;;
  esac
}

echo "==> 路径审计（任一失败立即退出）"
assert_inside "$ROOT"
assert_inside "$VENV"
assert_inside "$PLAYWRIGHT_HOME"
assert_inside "$PIP_CACHE"
assert_inside "$TMP_DIR"
[[ -f "$REQ" ]] || { echo "缺少 $REQ" >&2; exit 1; }
printf 'requirements: %s\n' "$REQ"

# ---- 2. 缓存与临时目录全部改到 E 盘 --------------------------------------
export PIP_CACHE_DIR="$PIP_CACHE"
export PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_HOME"
export TMPDIR="$TMP_DIR" TEMP="$TMP_DIR" TMP="$TMP_DIR"
export PYTHONPYCACHEPREFIX="$ROOT/.cache/tmp/pycache"
mkdir -p "$PIP_CACHE" "$PLAYWRIGHT_HOME" "$TMP_DIR"

# ---- 3. venv -------------------------------------------------------------
PY="$VENV/Scripts/python.exe"
if [[ -f "$PY" ]]; then
  echo "==> 复用已有 venv: $PY"
else
  echo "==> 创建 venv: $VENV"
  python -m venv "$VENV"
fi

"$PY" -m pip install --disable-pip-version-check --cache-dir "$PIP_CACHE" -r "$REQ"

# ---- 4. Chromium（版本由 playwright 包自带绑定决定） ---------------------
if "$PY" -c "import sys;from playwright.sync_api import sync_playwright" 2>/dev/null; then
  echo "==> playwright 可导入，检查 Chromium"
fi
"$PY" -m playwright install chromium

# ---- 5. 打印实际落盘位置与版本 -------------------------------------------
echo "==> 环境指纹"
"$PY" - "$PLAYWRIGHT_HOME" <<'PY'
import importlib.metadata as md, os, sys
print("python      :", sys.version.split()[0])
for name in ("requests", "playwright", "minio"):
    try:
        print(f"{name:<12}: {md.version(name)}")
    except Exception as exc:              # 缺包后续 self-check 会明确报错
        print(f"{name:<12}: MISSING ({exc})")
root = sys.argv[1]
print("browsers    :", os.path.realpath(root))
found = []
for dirpath, _dirnames, filenames in os.walk(root):
    for fn in filenames:
        if fn.lower().startswith(("chrome.exe", "headless_shell.exe", "chrome")):
            found.append(os.path.join(dirpath, fn))
for path in sorted(found)[:8]:
    print("chromium    :", os.path.realpath(path))
if not found:
    print("chromium    : NOT FOUND")
PY

echo
echo "==> self-check"
"$PY" scripts/verify/browser_evidence_e2e.py --self-check
