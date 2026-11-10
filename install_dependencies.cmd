@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt
set "AOM_INSTALL_EXIT=%errorlevel%"
if not "%AOM_INSTALL_EXIT%"=="0" pause
exit /b %AOM_INSTALL_EXIT%
