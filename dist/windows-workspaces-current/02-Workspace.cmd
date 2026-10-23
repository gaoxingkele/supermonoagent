@echo off
setlocal
start "" "%SystemRoot%\System32\wscript.exe" "%~dp0launch-hidden.vbs" "%~dp0..\windows-render-fixed\SuperMonoAgent\SuperMonoAgent.exe" workspace %*
