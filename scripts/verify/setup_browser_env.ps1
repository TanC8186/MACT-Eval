# 建立教师端真实浏览器验收环境（P0-3），Windows PowerShell 版本。
#
#   powershell -ExecutionPolicy Bypass -File scripts/verify/setup_browser_env.ps1
#
# 与 setup_browser_env.sh 等价：先断言所有目标目录在 E:/Personal_Design 内，
# 任何一条解析后越界就 throw 退出，不允许"先落到 C 盘再移动"。
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Set-Location $Root

$Venv            = Join-Path $Root '.venv-browser'
$PlaywrightHome  = Join-Path $Root '.cache/ms-playwright'
$PipCache        = Join-Path $Root '.cache/pip'
$TmpDir          = Join-Path $Root '.cache/tmp'
$Requirement     = Join-Path $Root 'scripts/verify/requirements-browser.txt'

function Assert-Inside([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $normRoot = [System.IO.Path]::GetFullPath($Root)
    if (-not $resolved.StartsWith($normRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "路径越界：$Path -> $resolved 不在 $normRoot 内"
    }
    Write-Host ("OK    " + $resolved)
}

Write-Host '==> 路径审计（任一失败立即退出）'
Assert-Inside $Root
Assert-Inside $Venv
Assert-Inside $PlaywrightHome
Assert-Inside $PipCache
Assert-Inside $TmpDir
if (-not (Test-Path $Requirement)) { throw "缺少 $Requirement" }

$env:PIP_CACHE_DIR = $PipCache
$env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightHome
$env:TMPDIR = $TmpDir
$env:TEMP = $TmpDir
$env:TMP = $TmpDir
$env:PYTHONPYCACHEPREFIX = Join-Path $TmpDir 'pycache'
New-Item -ItemType Directory -Force -Path $PipCache, $PlaywrightHome, $TmpDir | Out-Null

$Python = Join-Path $Venv 'Scripts/python.exe'
if (Test-Path $Python) {
    Write-Host "==> 复用已有 venv: $Python"
} else {
    Write-Host "==> 创建 venv: $Venv"
    python -m venv $Venv
}

& $Python -m pip install --disable-pip-version-check --cache-dir $PipCache -r $Requirement
if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }

& $Python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "playwright install chromium 失败" }

Write-Host '==> 环境指纹'
& $Python -c @"
import importlib.metadata as md, os, sys
print('python      :', sys.version.split()[0])
for name in ('requests', 'playwright', 'minio'):
    try:
        print(f'{name:<12}: {md.version(name)}')
    except Exception as exc:
        print(f'{name:<12}: MISSING ({exc})')
root = sys.argv[1]
print('browsers    :', os.path.realpath(root))
found = [os.path.join(dp, fn) for dp, _dn, fns in os.walk(root) for fn in fns
         if fn.lower().startswith(('chrome.exe', 'headless_shell.exe', 'chrome'))]
for path in sorted(found)[:8]:
    print('chromium    :', os.path.realpath(path))
if not found:
    print('chromium    : NOT FOUND')
"@ $PlaywrightHome

Write-Host '==> self-check'
& $Python scripts/verify/browser_evidence_e2e.py --self-check
if ($LASTEXITCODE -ne 0) { throw "self-check 失败（浏览器验收环境不可用）" }
