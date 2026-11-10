@echo off
setlocal
call "%~dp0start.cmd" --simulate %*
exit /b %errorlevel%
