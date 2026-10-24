@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1" %*
set "SMA_INSTALL_EXIT=%ERRORLEVEL%"
if not "%SMA_INSTALL_EXIT%"=="0" pause
exit /b %SMA_INSTALL_EXIT%
