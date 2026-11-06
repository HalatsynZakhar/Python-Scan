@echo off
chcp 65001 >nul
setlocal

for %%I in ("%~dp0..") do set "APP_DIR=%%~fI"
set "BRANCH=%~1"
set "INTERVAL=%~2"

if not defined BRANCH set "BRANCH=main"
if not defined INTERVAL set "INTERVAL=5"

cd /d "%APP_DIR%"

:restart
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%APP_DIR%\scripts\supervisor.ps1" -Branch "%BRANCH%" -CheckIntervalMinutes %INTERVAL%
set "EXIT_CODE=%errorlevel%"

echo [!] Supervisor exited with code %EXIT_CODE%. Restarting in 10s...
timeout /t 10 /nobreak >nul
goto restart