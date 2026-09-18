param([switch]$NoPause)
$ErrorActionPreference = "Stop"
# 本脚本位于 tools/local_windows/，项目根目录为上两级
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$env:PYTHONPATH = "src"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "          资金链证审系统 (Legal Funds Agent)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

$pythonExe = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Host "[错误] 未找到 Python 虚拟环境: $pythonExe" -ForegroundColor Red
    Write-Host "请确认 .venv 文件夹是否已放置在项目根目录下。" -ForegroundColor Yellow
    Write-Host ""
    if (-not $NoPause) { Read-Host "按回车键退出" }
    exit 1
}

# 清理残留的 Streamlit 进程（含孤儿进程），防止端口冲突或网址打不开
try {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'streamlit' } |
        ForEach-Object {
            Write-Host "[系统清理] 正在停止残留的 Streamlit 进程 (PID: $($_.ProcessId))..." -ForegroundColor Yellow
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    $occupied = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
    if ($occupied) {
        Write-Host "[系统清理] 正在释放被占用的 8501 端口 (PID: $($occupied.OwningProcess))..." -ForegroundColor Yellow
        $occupied | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 800
} catch {}

Write-Host "[1/2] 正在启动 Streamlit 高稳定性守护模式..." -ForegroundColor Green
Write-Host "本地访问地址: http://localhost:8501" -ForegroundColor Yellow
Write-Host "提示: 保持此窗口开启即可保持系统运行。服务就绪后浏览器将自动打开。" -ForegroundColor Gray
Write-Host "--------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

# 后台异步等待服务就绪后自动打开浏览器（健康检查最多 30 秒，取代旧的固定 2 秒延迟）
# 必须在启动 streamlit 之后再开始等待，否则永远等不到服务就绪
$browserJob = Start-Job -ScriptBlock {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri http://localhost:8501/healthz -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) {
                Start-Process "http://localhost:8501"
                return
            }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
}

$streamlitArgs = @(
    "-m", "streamlit", "run", "ui\streamlit_app.py",
    "--server.port", "8501",
    "--server.headless", "true",
    "--server.fileWatcherType", "none",
    "--browser.gatherUsageStats", "false",
    "--server.enableCORS", "false",
    "--server.enableXsrfProtection", "false",
    "--server.maxUploadSize", "200"
)

& $pythonExe $streamlitArgs

Stop-Job $browserJob -ErrorAction SilentlyContinue
Remove-Job $browserJob -ErrorAction SilentlyContinue

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[提示] 服务已停止。" -ForegroundColor Yellow
    if (-not $NoPause) { Read-Host "按回车键退出..." }
}
