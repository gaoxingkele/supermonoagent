@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1" %*
set "SMA_INSTALL_EXIT=%ERRORLEVEL%"
if "%SMA_INSTALL_EXIT%"=="0" (
  echo Installation completed. An existing SuperMonoAgent window is reused.
  echo Close and reopen the client to load a new version. Background terminals stay running.
  echo This installer window will close in 8 seconds.
  timeout /t 8 /nobreak >nul
)
if not "%SMA_INSTALL_EXIT%"=="0" pause
exit /b %SMA_INSTALL_EXIT%
