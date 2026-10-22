@echo off
setlocal
set "SUPERMONO_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%SUPERMONO_PY%" (
  echo Missing .venv. Run: py -m venv .venv
  echo Then: .venv\Scripts\python -m pip install -e ".[desktop]"
  exit /b 1
)
start "" "%SystemRoot%\System32\wscript.exe" "%~dp0launch-hidden.vbs" "%SUPERMONO_PY%" -m supermonoagent.launcher browser %*
