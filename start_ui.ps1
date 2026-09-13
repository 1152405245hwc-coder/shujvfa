$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$env:PYTHONPATH = "src"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "          资金链证审系统 (Legal Funds Agent)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

$pythonExe = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Host "[错误] 未找到 Python 虚拟环境: $pythonExe" -ForegroundColor Red
    Write-Host "请确认 .venv 文件夹是否已放置在项目根目录下。" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

# 自动清理 8501 端口可能残留的僵死进程，防止端口冲突或网址打不开
try {
    $occupied = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
    if ($occupied) {
        Write-Host "[系统清理] 正在释放被占用的 8501 端口 (PID: $($occupied.OwningProcess))..." -ForegroundColor Yellow
        Stop-Process -Id $occupied.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 800
    }
} catch {}

Write-Host "[1/2] 正在准备启动资金链证审工作台..." -ForegroundColor Green
Write-Host "本地访问地址: http://localhost:8501" -ForegroundColor Yellow

# 后台异步启动默认浏览器，循环健康检查最多 30 秒，确保 Streamlit 端口已就绪
$maxAttempts = 60
$opened = $false
for ($i = 0; $i -lt $maxAttempts; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri http://localhost:8501/healthz -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) {
            Start-Process http://localhost:8501
            $opened = $true
            break
        }
    } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $opened) {
    Write-Host "[警告] 服务启动超时，请手动刷新浏览器。" -ForegroundColor Yellow
}

Write-Host "[2/2] 正在启动 Streamlit 高稳定性守护模式并打开浏览器..." -ForegroundColor Green
Write-Host "提示: 保持此窗口开启即可保持系统运行。如需关闭，直接关闭本控制台窗口即可。" -ForegroundColor Gray
Write-Host "--------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

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

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[提示] 服务已停止。" -ForegroundColor Yellow
    Read-Host "按回车键退出..."
}
