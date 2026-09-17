@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  资金链证审系统 - 一键启动入口
REM  首次运行: 自动检查 Python、创建虚拟环境、安装依赖
REM  之后运行: 跳过安装, 直接启动 Streamlit 并打开浏览器
REM  本脚本不含任何 API Key, 默认使用本地 Mock, 不联网产生费用
REM ============================================================

title 资金链证审系统

REM 1. 检查 Python (要求 >= 3.11)
set "PYCMD="
where py >nul 2>nul && (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PYCMD=py -3"
)
if not defined PYCMD (
    where python >nul 2>nul && (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PYCMD=python"
    )
)
if not defined PYCMD (
    echo.
    echo [错误] 未检测到可用的 Python 3.11 或更高版本。
    echo.
    echo 请按以下步骤操作:
    echo   1. 打开浏览器访问 https://www.python.org/downloads/ 下载 Python
    echo      (安装时请务必勾选 "Add Python to PATH")
    echo   2. 安装完成后, 重新双击本脚本
    echo.
    echo 如果已安装 Python 但仍提示本错误, 请重新安装并勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

set "VENV_MARK=.venv\.bootstrap_done"

REM 2. 首次运行: 初始化虚拟环境与依赖
if not exist ".venv\Scripts\python.exe" goto :bootstrap
if not exist "%VENV_MARK%" goto :bootstrap
goto :start

:bootstrap
echo.
echo [首次运行] 正在初始化环境, 可能需要几分钟, 请耐心等待...
echo.

REM 2a. 优先使用 uv (更快) ; 没有 uv 则回退 pip
where uv >nul 2>nul
if %errorlevel% equ 0 goto :bootstrap_uv
goto :bootstrap_pip

:bootstrap_uv
echo [1/3] 使用 uv 创建虚拟环境...
uv venv .venv
if %errorlevel% neq 0 goto :err_venv
echo [2/3] 使用 uv 安装依赖 (含项目本体与界面组件)...
call uv sync --extra ui --extra dev --extra pdf
if %errorlevel% neq 0 goto :err_dep
goto :mark

:bootstrap_pip
echo [1/3] 创建 Python 虚拟环境...
%PYCMD% -m venv .venv
if %errorlevel% neq 0 goto :err_venv
echo [2/3] 升级 pip...
.venv\Scripts\python.exe -m pip install --upgrade pip
if %errorlevel% neq 0 goto :err_dep
echo [3/3] 安装依赖 (含项目本体与界面组件)...
.venv\Scripts\python.exe -m pip install -e ".[ui,dev]"
if %errorlevel% neq 0 goto :err_dep

:mark
echo ok> "%VENV_MARK%"
echo.
echo [初始化完成] 依赖安装成功, 正在启动系统...
goto :start

:err_venv
echo.
echo [错误] 创建虚拟环境失败。请确认 Python 安装完整后重试。
pause
exit /b 1

:err_dep
echo.
echo [错误] 依赖安装失败。请检查网络连接后重新双击本脚本重试。
pause
exit /b 1

REM 3. 启动 (交给 start_ui.ps1 处理健康检查与浏览器打开)
:start
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_ui.ps1"
if %errorlevel% neq 0 (
    echo.
    echo [错误] 服务异常退出, 错误码 %errorlevel%
    pause
)
endlocal
