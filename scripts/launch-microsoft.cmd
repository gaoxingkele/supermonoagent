@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m supermonoagent.microsoft %*
) else (
  python -m supermonoagent.microsoft %*
)
exit /b %errorlevel%
