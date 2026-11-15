#!/usr/bin/env bash
# 建立/更新项目验证 venv（P0-1.7，2026-10-28）。
#
#   bash scripts/verify/setup_verify_env.sh
#
# 产物固定在 E:/Personal_Design/.venv-verify，所有缓存与临时文件也落在项目内。
# **本脚本不创建任何项目外的目录**：pip cache、TMP、pycache 全部指向 .cache/。
#
# 为什么必须固定：verify_all.sh 曾用裸 `python -m pytest`，解释器取决于 PATH
# （本机是 E:/anaconda3）。换台机器或改了 PATH，同一份代码会静默换成另一套
# 依赖版本，而门禁照样"绿"。固定 venv 后，"绿"才对应一组确定的版本。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/.venv-verify"
CACHE="$ROOT/.cache"
REQ="$ROOT/scripts/verify/requirements-verify.txt"

# 目标路径必须都在项目内——本脚本自己先过这一关，避免"装完才发现装错盘"。
for dir in "$VENV" "$CACHE"; do
  case "$dir" in
    "$ROOT"/*) ;;
    *) echo "!! $dir 不在项目根 $ROOT 下，拒绝继续" >&2; exit 1 ;;
  esac
done

export PIP_CACHE_DIR="$CACHE/pip"
export TMPDIR="$CACHE/tmp"
export TEMP="$CACHE/tmp"
export TMP="$CACHE/tmp"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$CACHE/tmp/pycache"
mkdir -p "$PIP_CACHE_DIR" "$TMPDIR" "$PYTHONPYCACHEPREFIX"

echo "==> 项目根:  $ROOT"
echo "==> venv:    $VENV"
echo "==> pip 缓存: $PIP_CACHE_DIR"
echo "==> 临时目录: $TMPDIR"

if [[ -x "$VENV/Scripts/python.exe" ]]; then
  PY="$VENV/Scripts/python.exe"
elif [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
else
  echo "==> 创建 venv"
  # --system-site-packages 刻意不用：本机全局 Python 有互相冲突的 OpenCV 轮子
  # （见 scripts/verify/run_ai_tests.py 的说明），验证环境必须与它隔离。
  python -m venv "$VENV"
  if [[ -x "$VENV/Scripts/python.exe" ]]; then
    PY="$VENV/Scripts/python.exe"
  else
    PY="$VENV/bin/python"
  fi
fi

echo "==> 解释器: $PY"
"$PY" -c "import sys; print('   ', sys.version.replace(chr(10), ' '))"

# 解释器必须在项目内，否则后面的 pip 会把包装进一个不受控的环境。
# 不要用 bash 的 case 比较：`sys.prefix` 是 Windows 写法（E:\...），而 $ROOT
# 是 MSYS 写法（/e/...），直接比永远不相等。交给 Python 用 pathlib 比。
"$PY" - "$ROOT" <<'PYEOF'
import pathlib, sys
prefix = pathlib.Path(sys.prefix).resolve()
root = pathlib.Path(sys.argv[1]).resolve()
if prefix != root and root not in prefix.parents:
    sys.exit(f"!! 解释器前缀 {prefix} 不在项目根 {root} 下，拒绝安装")
PYEOF

"$PY" -m pip install --disable-pip-version-check --quiet --upgrade pip
"$PY" -m pip install --disable-pip-version-check -r "$REQ"

echo
echo "==> 已安装版本"
"$PY" - <<'PYEOF'
import importlib.metadata as md
for name in ("pytest", "numpy", "pandas", "scikit-learn", "scipy",
             "matplotlib", "PyYAML", "requests", "minio", "psutil"):
    try:
        print(f"    {name}=={md.version(name)}")
    except md.PackageNotFoundError:
        print(f"    {name}: MISSING")
PYEOF

echo
echo "==> 自检：三个纯 Python 套件能否 import"
"$PY" -m pytest --version

echo
echo "完成。verify_all.sh 将通过 VERIFY_PYTHON 使用该解释器："
echo "    export VERIFY_PYTHON=\"$PY\""
