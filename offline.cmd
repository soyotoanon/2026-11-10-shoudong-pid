@echo off
setlocal
call "%~dp0start.cmd" --offline %*
exit /b %errorlevel%
