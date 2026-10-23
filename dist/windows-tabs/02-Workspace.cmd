@echo off
setlocal
start "" "%SystemRoot%\System32\wscript.exe" "%~dp0..\windows-hidden\launch-hidden.vbs" "%~dp0..\windows-hidden\SuperMonoAgent\SuperMonoAgent.exe" workspace %*
