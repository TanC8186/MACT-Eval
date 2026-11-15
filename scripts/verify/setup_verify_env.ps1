# 建立/更新项目验证 venv（P0-1.7，2026-10-28）— PowerShell 版本
#
#   powershell -NoProfile -File scripts/verify/setup_verify_env.ps1
#
# 与 setup_verify_env.sh 等价。存在理由：Windows 上不一定有 Git Bash，
# 而 Docker Desktop 正是由 PowerShell 启动的同一批用户。
#
# 产物固定在 E:/Personal_Design/.venv-verify；pip cache、TMP、pycache 全部
# 指向项目内 .cache/。**本脚本不创建任何项目外的目录。**

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Venv = Join-Path $Root '.venv-verify'
$Cache = Join-Path $Root '.cache'
$Req = Join-Path $Root 'scripts\verify\requirements-verify.txt'

# 目标路径必须都在项目内——本脚本自己先过这一关。
foreach ($dir in @($Venv, $Cache)) {
    if (-not $dir.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Error "$dir 不在项目根 $Root 下，拒绝继续"
        exit 1
    }
}

$env:PIP_CACHE_DIR = Join-Path $Cache 'pip'
$env:TMPDIR        = Join-Path $Cache 'tmp'
$env:TEMP          = $env:TMPDIR
$env:TMP           = $env:TMPDIR
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $Cache 'tmp\pycache'
New-Item -ItemType Directory -Force -Path $env:PIP_CACHE_DIR, $env:TMPDIR, $env:PYTHONPYCACHEPREFIX | Out-Null

# 控制台默认按 GBK 输出，中文会变成乱码。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

Write-Host "==> 项目根:  $Root"
Write-Host "==> venv:    $Venv"
Write-Host "==> pip 缓存: $($env:PIP_CACHE_DIR)"
Write-Host "==> 临时目录: $($env:TMPDIR)"

$Python = $null
$candidate = Join-Path $Venv 'Scripts\python.exe'
if (Test-Path $candidate) {
    $Python = $candidate
} else {
    Write-Host '==> 创建 venv'
    # 刻意不用 --system-site-packages：本机全局 Python 有互相冲突的 OpenCV
    # 轮子（见 scripts/verify/run_ai_tests.py 的说明），验证环境必须隔离。
    python -m venv $Venv
    if (-not (Test-Path $candidate)) {
        Write-Error "venv 创建后仍找不到 $candidate"
        exit 1
    }
    $Python = $candidate
}

Write-Host "==> 解释器: $Python"
& $Python -c "import sys; print('   ', sys.version.split(chr(10))[0])"

# 解释器前缀必须在项目内，否则 pip 会把包装进一个不受控的环境。
& $Python -c @"
import pathlib, sys
prefix = pathlib.Path(sys.prefix).resolve()
root = pathlib.Path(r'$Root').resolve()
if prefix != root and root not in prefix.parents:
    sys.exit(f'!! 解释器前缀 {prefix} 不在项目根 {root} 下，拒绝安装')
"@
if ($LASTEXITCODE -ne 0) { exit 1 }

& $Python -m pip install --disable-pip-version-check --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { exit 1 }
& $Python -m pip install --disable-pip-version-check -r $Req
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ''
Write-Host '==> 已安装版本'
# 用临时脚本文件而不是 -c here-string：PowerShell 会把多行 here-string 拆成
# 多个参数传给 -c，Python 只拿到第一段（实测报 "print(f ... never closed"）。
$report = Join-Path $env:TMPDIR 'report_versions.py'
@'
import importlib.metadata as md
for name in ("pytest", "numpy", "pandas", "scikit-learn", "scipy",
             "matplotlib", "PyYAML", "requests", "minio", "psutil",
             "bcrypt", "websockets"):
    try:
        print(f"    {name}=={md.version(name)}")
    except md.PackageNotFoundError:
        print(f"    {name}: MISSING")
'@ | Set-Content -Path $report -Encoding UTF8
& $Python $report

Write-Host ''
Write-Host '完成。verify_all.sh 将通过 VERIFY_PYTHON 使用该解释器：'
Write-Host "    `$env:VERIFY_PYTHON = `"$Python`""
