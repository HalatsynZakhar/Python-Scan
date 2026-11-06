@echo off
chcp 65001 >nul
setlocal

for %%I in ("%~dp0.") do set "APP_DIR=%%~fI"
cd /d "%APP_DIR%"

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%APP_DIR%\scripts\restart.ps1" %*