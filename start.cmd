@echo off
setlocal
cd /d "%~dp0"
python -B app.py %*
set "AOM_LAUNCH_EXIT=%errorlevel%"
if not "%AOM_LAUNCH_EXIT%"=="0" pause
exit /b %AOM_LAUNCH_EXIT%
