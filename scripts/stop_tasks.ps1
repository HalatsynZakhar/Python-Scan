[CmdletBinding()]
param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$AppDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonScript = Join-Path $AppDir "images_xml.py"
$ServerScript = Join-Path $AppDir "image_sync_server.py"
$SupervisorScript = Join-Path $AppDir "scripts\supervisor.ps1"
$AutoUpdateBat = Join-Path $AppDir "scripts\auto_update.bat"
$PidFile = Join-Path $AppDir "logs\images_xml.pid"
$StoppedProcesses = 0

trap {
    Write-Host ""
    Write-Host "ПОМИЛКА ПОВНОЇ ЗУПИНКИ" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($_.InvocationInfo.PositionMessage) {
        Write-Host $_.InvocationInfo.PositionMessage -ForegroundColor DarkRed
    }
    Write-Host ""

    if (!$NoPause) {
        Read-Host "Натисніть Enter, щоб закрити вікно"
    }
    exit 1
}

function Invoke-SchtasksIgnoringMissingTask {
    param([string[]]$Arguments)

    $Process = Start-Process `
        -FilePath "schtasks.exe" `
        -ArgumentList $Arguments `
        -WindowStyle Hidden `
        -Wait `
        -PassThru

    # schtasks returns code 1 when the task does not exist. For cleanup this is
    # already the desired state.
    if ($Process.ExitCode -notin @(0, 1)) {
        throw (
            "schtasks завершився з кодом $($Process.ExitCode): " +
            ($Arguments -join " ")
        )
    }
}

function Stop-ProjectProcesses {
    $Processes = Get-CimInstance Win32_Process |
        Where-Object {
            $CommandLine = [string]$_.CommandLine
            $CommandLine -and (
                $CommandLine.IndexOf(
                    $PythonScript,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0 -or
                $CommandLine.IndexOf(
                    $ServerScript,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0 -or
                $CommandLine.IndexOf(
                    $SupervisorScript,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0 -or
                $CommandLine.IndexOf(
                    $AutoUpdateBat,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            )
        } |
        Sort-Object {
            # Спочатку зупиняється Python, потім супервізор і BAT.
            if ($_.Name -in @("python.exe", "pythonw.exe")) { 0 } else { 1 }
        }

    foreach ($Process in $Processes) {
        try {
            Stop-Process -Id $Process.ProcessId -Force -ErrorAction Stop
            Write-Output (
                "Зупинено процес: $($Process.Name), PID $($Process.ProcessId)"
            )
            $script:StoppedProcesses++
        }
        catch {
            Write-Warning (
                "Не вдалося зупинити PID $($Process.ProcessId): " +
                $_.Exception.Message
            )
        }
    }
}

Write-Output "Зупинка завдання ImagesXML..."
Invoke-SchtasksIgnoringMissingTask `
    -Arguments @("/End", "/TN", "ImagesXML")

# Після зупинки завдання явно завершуємо всі процеси проєкту. Це також
# охоплює ручний запуск auto_update.bat поза Планувальником.
Stop-ProjectProcesses

Write-Output "Видалення завдань Планувальника..."
Invoke-SchtasksIgnoringMissingTask `
    -Arguments @("/Delete", "/TN", "ImagesXML", "/F")
Invoke-SchtasksIgnoringMissingTask `
    -Arguments @("/End", "/TN", "ImagesXMLUpdate")
Invoke-SchtasksIgnoringMissingTask `
    -Arguments @("/Delete", "/TN", "ImagesXMLUpdate", "/F")

Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue

# Повторна перевірка закриває рідкісну гонку, якщо супервізор встиг запустити
# Python між першою зупинкою процесів і видаленням завдання.
Start-Sleep -Seconds 1
Stop-ProjectProcesses

Write-Output "Повну зупинку завершено:"
Write-Output "- Python images_xml.py / image_sync_server.py зупинено;"
Write-Output "- BAT і супервізор автооновлення зупинені;"
Write-Output "- завдання ImagesXML та ImagesXMLUpdate видалені;"
Write-Output "- PID-файл видалено."
Write-Output "- завершено процесів проєкту: $StoppedProcesses."

Write-Host ""
Write-Host "УСІ ПРОЦЕСИ ТА ЗАВДАННЯ ПРОЄКТУ ЗУПИНЕНО" `
    -ForegroundColor Green
Write-Host ""

if (!$NoPause) {
    Read-Host "Натисніть Enter, щоб закрити вікно"
}
