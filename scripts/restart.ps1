[CmdletBinding()]
param(
    [switch]$NoPause
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"
$AppDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StopScript = Join-Path $AppDir "scripts\stop_tasks.ps1"
$AutoUpdateBat = Join-Path $AppDir "scripts\auto_update.bat"
$TaskName = "ImagesXML"
$Port = 8092

Set-Location $AppDir

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "   ПЕРЕЗАПУСК Horoshop Sync (Python-Scan)" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Зупинка процесів
Write-Host "[1/3] Зупинка попередніх процесів..." -ForegroundColor Yellow
if (Test-Path $StopScript) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $StopScript -NoPause
}

# 2. Очищення BOM у config.json якщо він з'явився після редагування Блокнотом
$ConfigFile = Join-Path $AppDir "config.json"
if (Test-Path $ConfigFile) {
    try {
        $b = [System.IO.File]::ReadAllBytes($ConfigFile)
        if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
            Write-Host "  -> Очищено UTF-8 BOM з config.json" -ForegroundColor DarkGray
            $clean = New-Object byte[] ($b.Length - 3)
            [System.Array]::Copy($b, 3, $clean, 0, $clean.Length)
            [System.IO.File]::WriteAllBytes($ConfigFile, $clean)
        }
    } catch {}
}

# 3. Запуск сервісу
Write-Host ""
Write-Host "[2/3] Запуск служби..." -ForegroundColor Yellow

$TaskExists = $false
try {
    $TaskCheck = Start-Process -FilePath "schtasks.exe" -ArgumentList @("/Query", "/TN", $TaskName) -WindowStyle Hidden -Wait -PassThru
    $TaskExists = ($TaskCheck.ExitCode -eq 0)
} catch {
    $TaskExists = $false
}

if ($TaskExists) {
    Write-Host "  -> Запуск через Планувальник Windows (завдання: $TaskName)..." -ForegroundColor Green
    & schtasks.exe /Run /TN $TaskName 2>&1 | Out-Null
} else {
    Write-Host "  -> Запуск через фоновий супервізор ($AutoUpdateBat)..." -ForegroundColor Green
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$AutoUpdateBat`" main 5" -WorkingDirectory $AppDir -WindowStyle Hidden
}

# 4. Перевірка статусу
Write-Host ""
Write-Host "[3/3] Перевірка готовності панелі (http://localhost:$Port)..." -ForegroundColor Yellow
$Started = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/login" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($resp -and ($resp.StatusCode -eq 200 -or $resp.StatusCode -eq 303)) {
            $Started = $true
            break
        }
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -in @(200, 303, 401)) {
            $Started = $true
            break
        }
    }
}

Write-Host ""
if ($Started) {
    Write-Host "================================================================" -ForegroundColor Green
    Write-Host "   СЕРВЕР УСПІШНО ЗАПУЩЕНО І ГОТОВИЙ ДО РОБОТИ!" -ForegroundColor Green
    Write-Host "   Адреса веб-панелі: http://localhost:$Port" -ForegroundColor Green
    Write-Host "   Логін: admin" -ForegroundColor Green
    Write-Host "   Пароль: Bublik44" -ForegroundColor Green
    Write-Host "================================================================" -ForegroundColor Green
} else {
    Write-Host "Сервер ще запускається в фоні або ініціалізує базу фото." -ForegroundColor Yellow
    Write-Host "Перевірте журнал: C:\Python-Scan\logs\python-output.log" -ForegroundColor Yellow
}
Write-Host ""

if (!$NoPause) {
    Read-Host "Натисніть Enter, щоб закрити вікно"
}