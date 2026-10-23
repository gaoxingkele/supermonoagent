@echo off
setlocal
"%~dp0SuperMonoAgent\SuperMonoAgent.exe" microsoft %*
if errorlevel 1 pause
