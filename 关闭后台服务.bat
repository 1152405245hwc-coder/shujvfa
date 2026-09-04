@echo off
setlocal
echo Stopping Streamlit background process...
taskkill /F /FI "WINDOWTITLE eq *streamlit*" >nul 2>&1
taskkill /F /IM python.exe /FI "MODULES eq *streamlit*" >nul 2>&1
echo Done.
timeout /t 2 >nul