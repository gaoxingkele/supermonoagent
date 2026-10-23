@echo off
setlocal
"%~dp0SuperMonoAgent\SuperMonoAgent.exe" workspace %*
if errorlevel 1 pause
