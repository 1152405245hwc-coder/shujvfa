@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $desktop = [Environment]::GetFolderPath('Desktop'); $s = $ws.CreateShortcut(\"$desktop\资金链证审系统.lnk\"); $s.TargetPath = '%~dp0双击启动.bat'; $s.WorkingDirectory = '%~dp0'; $s.Save(); Write-Host '[SUCCESS] Desktop shortcut created successfully!' -ForegroundColor Green"
pause
