# 一键安装（Windows PowerShell）
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# 做四件事：
#   1. 检查 Python 3.11+（要 tomllib）
#   2. 装 mcp 依赖
#   3. 把本仓库注册成 Codex 本地 marketplace 并安装插件
#   4. 跑 doctor.py --fix 自动体检 + 修环境
$ErrorActionPreference = 'Continue'

$Repo = Split-Path -Parent $MyInvocation.MyCommand.Definition
Write-Host "==> model-switcher 安装" -ForegroundColor Cyan
Write-Host "    仓库：$Repo"

# 1) Python 3.11+
$Py = $null
foreach ($cand in @('python', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cand -eq 'py') {
        & $cmd.Source -3 -c "import tomllib" 2>$null
        if ($LASTEXITCODE -eq 0) { $Py = @($cmd.Source, '-3'); break }
    } else {
        & $cmd.Source -c "import tomllib" 2>$null
        if ($LASTEXITCODE -eq 0) { $Py = @($cmd.Source); break }
    }
}
if (-not $Py) {
    Write-Host "!! 找不到 Python 3.11+（需要 tomllib）。请先安装 Python 3.11 或更高版本。" -ForegroundColor Red
    exit 1
}
Write-Host "    Python：$($Py -join ' ')"

# 2) 依赖
& $Py[0] @($Py[1..($Py.Length-1)]) -c "import mcp" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> 安装 mcp 依赖" -ForegroundColor Cyan
    & $Py[0] @($Py[1..($Py.Length-1)]) -m pip install --quiet mcp
}

# 3) 注册 marketplace + 安装插件
$codexExe = $null
$codex = Get-Command codex -ErrorAction SilentlyContinue
if ($codex) { $codexExe = $codex.Source }
if (-not $codexExe) {
    $hit = Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe" -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { $codexExe = $hit.FullName }
}
if ($codexExe) {
    Write-Host "==> 注册 marketplace" -ForegroundColor Cyan
    & $codexExe plugin marketplace add $Repo
    Write-Host "==> 安装插件" -ForegroundColor Cyan
    & $codexExe plugin add model-switcher@codex-model-switcher
} else {
    Write-Host "!! 找不到 codex 命令，请手动执行：" -ForegroundColor Yellow
    Write-Host "     codex plugin marketplace add `"$Repo`""
    Write-Host "     codex plugin add model-switcher@codex-model-switcher"
}

# 4) 体检 + 自动修
Write-Host "==> 环境体检" -ForegroundColor Cyan
& $Py[0] @($Py[1..($Py.Length-1)]) (Join-Path $Repo 'doctor.py') --fix

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host "下一步："
Write-Host "  1. 完全退出并重开 Codex 桌面版"
Write-Host "  2. 在对话里说「用 deepseek，key 是 sk-xxx」即可接入"
Write-Host "  3. 接入后建议再跑一次 doctor.py 确认"
