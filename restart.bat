@echo off
chcp 65001 >nul
title Перезапуск Python-Scan (Horoshop Sync)
cd /d "%~dp0"

echo ================================================================
echo   Перезапуск Horoshop Sync (Python-Scan)
echo ================================================================
echo.

echo [*] Зупинка попередніх процесів...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_tasks.ps1" -NoPause

echo [*] Запуск оновленого сервера під контролем супервізора...
echo [*] Панель керування: http://localhost:8092
echo.
call "%~dp0scripts\auto_update.bat" main 5
