@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-ForgeFlow.ps1" %*
set "forgeflow_result=%errorlevel%"
if not "%forgeflow_result%"=="0" pause
exit /b %forgeflow_result%
