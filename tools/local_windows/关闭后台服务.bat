@echo off
setlocal

echo [信息] 正在查找并停止 Streamlit 后台服务...

powershell -NoProfile -Command "$k=0; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'streamlit' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $k++ }; $portProcs = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue; if ($portProcs) { foreach ($p in $portProcs) { Stop-Process -Id $p.OwningProcess -Force -ErrorAction SilentlyContinue; $k++ } }; if ($k -gt 0) { Write-Host \"[成功] 已关闭 $k 个运行中的服务。\" } else { Write-Host \"[提示] 未发现运行中的 Streamlit 服务。\" }"
