#Requires -Version 5.1
# PLC 设备远程控制程序启动脚本 (PowerShell)
# 使用 uv 运行，自动处理虚拟环境

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.BackgroundColor = "Black"
$Host.UI.RawUI.ForegroundColor = "White"
Clear-Host

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "   PLC 设备远程控制程序" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""

# 检查 uv
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "[错误] 未找到 uv，请先安装 uv:" -ForegroundColor Red
    Write-Host "  https://docs.astral.sh/uv/getting-started/installation/"
    Write-Host ""
    Read-Host "按 Enter 键退出"
    exit 1
}

# 检查 Python 文件
$scriptPath = Join-Path $PSScriptRoot "device_control.py"
if (-not (Test-Path $scriptPath)) {
    Write-Host "[错误] 当前目录未找到 device_control.py" -ForegroundColor Red
    Write-Host "  请确保脚本与 device_control.py 在同一目录下运行。"
    Write-Host ""
    Read-Host "按 Enter 键退出"
    exit 1
}

Write-Host "[信息] 正在使用 uv 启动程序..." -ForegroundColor Green
Write-Host "[信息] uv 版本: " -NoNewline -ForegroundColor Gray
& uv --version
Write-Host ""

# 运行程序
Push-Location $PSScriptRoot
try {
    & uv run python device_control.py
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "[信息] 程序已退出。" -ForegroundColor Yellow
Read-Host "按 Enter 键关闭窗口"
