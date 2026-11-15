# 磁盘路径门禁（PowerShell 入口，P0-1.4，2026-10-28）
#
#   powershell -NoProfile -File scripts/verify/check_disk_policy.ps1
#   powershell -NoProfile -File scripts/verify/check_disk_policy.ps1 -SelfTest
#   powershell -NoProfile -File scripts/verify/check_disk_policy.ps1 `
#       -Json docs/reviews/evidence/disk-policy.json
#
# 判定逻辑只有一份，在 check_disk_policy.py 里。本文件刻意不复制规则：
# 两个平台各写一套判定，早晚会漂移成"Bash 说合规、PowerShell 说越界"，
# 而那种分歧比没有门禁更糟——它让人不再相信任何一侧的绿灯。
#
# 存在理由：PowerShell 用户在 Windows 上大概率没有 Git Bash 的 PATH，
# 但同一台机器上 Docker Desktop 正是由 PowerShell 启动的。
#
# 退出码：0 = 全部路径在项目内；1 = 有越界；2 = 前置条件缺失。

[CmdletBinding()]
param(
    [switch]$SelfTest,
    [string]$Json
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

# 解释器优先级：$env:VERIFY_PYTHON -> .venv-verify -> 系统 python
$Python = $null
if ($env:VERIFY_PYTHON -and (Test-Path $env:VERIFY_PYTHON)) {
    $Python = $env:VERIFY_PYTHON
} elseif (Test-Path (Join-Path $Root '.venv-verify\Scripts\python.exe')) {
    $Python = Join-Path $Root '.venv-verify\Scripts\python.exe'
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python) {
    Write-Error '找不到 Python 解释器（先运行 scripts/verify/setup_verify_env.sh）'
    exit 2
}

# 门禁自身的临时文件也必须落在项目内。
$env:TMPDIR = Join-Path $Root '.cache\tmp'
$env:TEMP   = $env:TMPDIR
$env:TMP    = $env:TMPDIR
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $Root '.cache\tmp\pycache'
New-Item -ItemType Directory -Force -Path $env:TMPDIR, $env:PYTHONPYCACHEPREFIX | Out-Null

# 控制台默认按 GBK 输出，中文明细会变成乱码，读的人无法据此修复。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 重定向到文件时没有控制台句柄，忽略即可（文件本身就是 UTF-8）。
}

Write-Host "==> 磁盘门禁解释器: $Python"

$checker = Join-Path $Root 'scripts\verify\check_disk_policy.py'
$argv = @('-I', $checker)
if ($SelfTest) { $argv += '--self-test' }
if ($Json) {
    if (-not [System.IO.Path]::IsPathRooted($Json)) {
        $Json = Join-Path $Root $Json
    }
    $argv += @('--json', $Json)
}

& $Python @argv
exit $LASTEXITCODE
