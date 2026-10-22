@echo off
setlocal
"%~dp0SuperMonoAgent\SuperMonoAgent.exe" browser %*
if errorlevel 1 pause
