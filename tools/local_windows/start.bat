@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_ui.ps1"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Process exited with code %errorlevel%
    pause
)
