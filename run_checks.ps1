$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONPATH = "src"
& ".\.venv\Scripts\python.exe" -m pytest -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "测试未通过（退出码 $LASTEXITCODE）" -ForegroundColor Red
    exit $LASTEXITCODE
}

& ".\.venv\Scripts\python.exe" -m compileall -q src ui tests
if ($LASTEXITCODE -ne 0) {
    Write-Host "编译检查未通过（退出码 $LASTEXITCODE）" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "自测完成，全部通过。" -ForegroundColor Green