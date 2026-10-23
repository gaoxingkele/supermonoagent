@echo off
setlocal
rem Attach the current UI to the existing shared daemon on port 6769.
"%~dp0..\windows-linked\SuperMonoAgent\SuperMonoAgent.exe" workspace %*
if errorlevel 1 pause
