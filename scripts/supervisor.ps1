[CmdletBinding()]
param(
    [string]$Branch = "main",
    [ValidateRange(1, 1439)]
    [int]$CheckIntervalMinutes = 5
)

$ErrorActionPreference = "Stop"
$AppDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $AppDir ".venv\Scripts\python.exe"
$ClockScript = Join-Path $AppDir "images_xml.py"
$ImagesXmlScript = Join-Path $AppDir "images_xml.py"
$ServerScript = Join-Path $AppDir "image_sync_server.py"
$WorkerScript = $ImagesXmlScript
$WorkerName = "images_xml.py"
$PidFile = Join-Path $AppDir "logs\images_xml.pid"
$LocalDiagnosticLog = Join-Path $AppDir "logs\supervisor-local.log"
$WorkerOutputLog = Join-Path $AppDir "logs\python-output.log"
$WorkerErrorLog = Join-Path $AppDir "logs\python-error.log"
$ConsoleLogFile = Join-Path $AppDir "logs\supervisor.log"
$MaxLogLines = 2000
$LogMutexName = "Global\ImagesXmlSharedLog"
$Worker = $null
$WorldUtcEpoch = 0.0
$WorldClock = $null
$KyivTimeZone = $null

Set-Location $AppDir

try {
    $Config = Get-Content (Join-Path $AppDir "config.json") -Raw |
        ConvertFrom-Json
    if ($Config.output_dir) {
        $LogFilename = if ($Config.log_filename) {
            [string]$Config.log_filename
        }
        else {
            "images_export.log"
        }
        $ConsoleLogFile = Join-Path ([string]$Config.output_dir) $LogFilename
    }
    elseif ($Config.log_file) {
        $ConsoleLogFile = [string]$Config.log_file
    }
    if ($Config.max_log_lines) {
        $MaxLogLines = [int]$Config.max_log_lines
    }
    if (
        $Config.server -and
        [bool]$Config.server.enabled
    ) {
        $WorkerScript = $ServerScript
        $WorkerName = "image_sync_server.py"
    }
}
catch {
    Write-Warning "Не вдалося прочитати налаштування журналу: $($_.Exception.Message)"
}

function Initialize-ReliableClock {
    $EpochOutput = & $PythonExe $ClockScript --print-utc-epoch 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning (
            "Не вдалося отримати час із Python; використовується системний " +
            "UTC-час."
        )
        $ParsedEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000
    }
    else {
        $ParsedEpoch = 0.0
        $EpochText = ([string]($EpochOutput | Select-Object -Last 1)).Trim()
        $Parsed = [double]::TryParse(
            $EpochText,
            [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$ParsedEpoch
        )
        if (!$Parsed) {
            Write-Warning (
                "Некоректна відповідь світового часу; використовується " +
                "системний UTC-час."
            )
            $ParsedEpoch = (
                [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000
            )
        }
    }

    $script:WorldUtcEpoch = $ParsedEpoch
    $script:WorldClock = [Diagnostics.Stopwatch]::StartNew()
    $script:KyivTimeZone = [TimeZoneInfo]::FindSystemTimeZoneById(
        "FLE Standard Time"
    )
}

function Get-ReliableKyivTime {
    if ($null -eq $script:WorldClock) {
        throw "Світовий годинник супервізора не ініціалізовано."
    }

    $CurrentEpoch = (
        $script:WorldUtcEpoch + $script:WorldClock.Elapsed.TotalSeconds
    )
    $UtcTime = [DateTimeOffset]::FromUnixTimeMilliseconds(
        [long]($CurrentEpoch * 1000)
    )
    return [TimeZoneInfo]::ConvertTime(
        $UtcTime,
        $script:KyivTimeZone
    )
}

function Write-SupervisorLog {
    param([string]$Message)
    $Timestamp = (Get-ReliableKyivTime).ToString(
        "yyyy-MM-ddTHH:mm:sszzz",
        [Globalization.CultureInfo]::InvariantCulture
    )
    $Line = "[$Timestamp] [супервізор] $Message"
    Write-Output $Line

    try {
        New-Item -ItemType Directory -Path (Split-Path $LocalDiagnosticLog) `
            -Force | Out-Null
        Add-Content `
            -LiteralPath $LocalDiagnosticLog `
            -Value $Line `
            -Encoding UTF8

        $LocalLines = Get-Content -LiteralPath $LocalDiagnosticLog
        if ($LocalLines.Count -gt $MaxLogLines) {
            $LocalLines |
                Select-Object -Last $MaxLogLines |
                Set-Content -LiteralPath $LocalDiagnosticLog -Encoding UTF8
        }
    }
    catch {
        Write-Warning "Не вдалося записати локальний журнал: $($_.Exception.Message)"
    }

    $LogMutex = $null
    $LockAcquired = $false
    try {
        $LogMutex = [System.Threading.Mutex]::new($false, $LogMutexName)
        try {
            $LockAcquired = $LogMutex.WaitOne(30000)
        }
        catch [System.Threading.AbandonedMutexException] {
            $LockAcquired = $true
        }
        if (!$LockAcquired) {
            throw "Не вдалося отримати блокування журналу за 30 секунд."
        }

        $LogDirectory = Split-Path $ConsoleLogFile
        if ($LogDirectory) {
            New-Item -ItemType Directory -Path $LogDirectory -Force |
                Out-Null
        }

        $Utf8Bom = New-Object System.Text.UTF8Encoding($true)
        $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

        if (!(Test-Path $ConsoleLogFile)) {
            [System.IO.File]::WriteAllBytes(
                $ConsoleLogFile,
                $Utf8Bom.GetPreamble()
            )
        }
        else {
            $ExistingBytes = [System.IO.File]::ReadAllBytes($ConsoleLogFile)
            $Preamble = $Utf8Bom.GetPreamble()
            $HasBom = (
                $ExistingBytes.Length -ge $Preamble.Length -and
                $ExistingBytes[0] -eq $Preamble[0] -and
                $ExistingBytes[1] -eq $Preamble[1] -and
                $ExistingBytes[2] -eq $Preamble[2]
            )
            if (!$HasBom) {
                $BytesWithBom = [byte[]]::new(
                    $Preamble.Length + $ExistingBytes.Length
                )
                [Array]::Copy(
                    $Preamble,
                    0,
                    $BytesWithBom,
                    0,
                    $Preamble.Length
                )
                [Array]::Copy(
                    $ExistingBytes,
                    0,
                    $BytesWithBom,
                    $Preamble.Length,
                    $ExistingBytes.Length
                )
                [System.IO.File]::WriteAllBytes(
                    $ConsoleLogFile,
                    $BytesWithBom
                )
            }
        }

        [System.IO.File]::AppendAllText(
            $ConsoleLogFile,
            "$Line$([Environment]::NewLine)",
            $Utf8NoBom
        )

        $Lines = [System.IO.File]::ReadAllLines(
            $ConsoleLogFile,
            [System.Text.Encoding]::UTF8
        )
        if ($Lines.Count -gt $MaxLogLines) {
            $NewestLines = $Lines |
                Select-Object -Last $MaxLogLines
            [System.IO.File]::WriteAllLines(
                $ConsoleLogFile,
                $NewestLines,
                $Utf8Bom
            )
        }
    }
    catch {
        Write-Warning "Не вдалося записати журнал супервізора: $($_.Exception.Message)"
    }
    finally {
        if ($LockAcquired) {
            $LogMutex.ReleaseMutex()
        }
        if ($null -ne $LogMutex) {
            $LogMutex.Dispose()
        }
    }
}

function Test-WorkerRunning {
    return ($null -ne $script:Worker -and !$script:Worker.HasExited)
}

function Start-Worker {
    if (Test-WorkerRunning) {
        return
    }

    if (!(Test-Path $PythonExe)) {
        throw "Не знайдено Python віртуального середовища: $PythonExe"
    }

    New-Item -ItemType Directory -Path (Split-Path $PidFile) -Force |
        Out-Null
    Remove-Item -LiteralPath $WorkerOutputLog -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $WorkerErrorLog -Force -ErrorAction SilentlyContinue

    $script:Worker = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList "`"$WorkerScript`"" `
        -WorkingDirectory $AppDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $WorkerOutputLog `
        -RedirectStandardError $WorkerErrorLog `
        -PassThru

    Set-Content -LiteralPath $PidFile -Value $script:Worker.Id -Encoding ASCII
    Write-SupervisorLog "Запущено Python-процес. PID: $($script:Worker.Id)"

    Start-Sleep -Seconds 5
    $script:Worker.Refresh()
    if ($script:Worker.HasExited) {
        $ExitCode = $script:Worker.ExitCode
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue

        $Details = @()
        if (Test-Path $WorkerOutputLog) {
            $Details += Get-Content -LiteralPath $WorkerOutputLog -Tail 10
        }
        if (Test-Path $WorkerErrorLog) {
            $Details += Get-Content -LiteralPath $WorkerErrorLog -Tail 10
        }
        $DetailsText = ($Details | Where-Object { $_ }) -join " | "
        if (!$DetailsText) {
            $DetailsText = "Python не вивів додаткових відомостей."
        }

        $script:Worker = $null
        throw (
            "Python-скрипт завершився одразу після запуску, код $ExitCode. " +
            "Ймовірна причина: обліковий запис завдання не має доступу до " +
            "images_dir або output_dir з config.json. " +
            "Якщо використовується UNC-шлях, перевірте мережеві права саме для " +
            "користувача завдання. Подробиці: $DetailsText"
        )
    }

    Write-SupervisorLog (
        "Python-скрипт успішно запущено, він продовжує працювати. " +
        "PID: $($script:Worker.Id)"
    )
}

function Stop-Worker {
    if (!(Test-WorkerRunning)) {
        return
    }

    $WorkerId = $script:Worker.Id
    Stop-Process -Id $WorkerId -Force
    $script:Worker.WaitForExit()
    Write-SupervisorLog "Скрипт відстеження зупинено. PID: $WorkerId"
    $script:Worker = $null
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

function Restore-WorkerFromPidFile {
    if (!(Test-Path $PidFile)) {
        return
    }

    $SavedPid = 0
    if (![int]::TryParse((Get-Content $PidFile -Raw).Trim(), [ref]$SavedPid)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return
    }

    $ProcessInfo = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $SavedPid" `
        -ErrorAction SilentlyContinue

    if ($null -eq $ProcessInfo -or $ProcessInfo.CommandLine -notlike "*$WorkerName*") {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return
    }

    $script:Worker = Get-Process -Id $SavedPid -ErrorAction SilentlyContinue
    if (Test-WorkerRunning) {
        Write-SupervisorLog "Знайдено вже запущений скрипт. PID: $SavedPid"
    }
}

function Test-UpdateRequired {
    git fetch origin $Branch --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося перевірити origin/$Branch."
    }

    $Local = (git rev-parse HEAD).Trim()
    $Remote = (git rev-parse "origin/$Branch").Trim()
    $WorkingTreeChanges = git status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося перевірити робочу копію Git."
    }

    return (($Local -ne $Remote) -or [bool]$WorkingTreeChanges)
}

function Update-Project {
    $Remote = (git rev-parse "origin/$Branch").Trim()
    Write-SupervisorLog "Примусова синхронізація з origin/$Branch."

    git reset --hard $Remote
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося синхронізувати код з origin/$Branch."
    }

    # Ігноровані config.json, .venv, logs і data зберігаються.
    git clean -fd
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося видалити зайві невідстежувані файли."
    }

    & $PythonExe -m pip install -r (Join-Path $AppDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося встановити залежності після оновлення."
    }
}

if (!(Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git не знайдено в PATH."
}
if (!(Test-Path (Join-Path $AppDir ".git"))) {
    throw "$AppDir не є Git-репозиторієм."
}

Initialize-ReliableClock
Write-SupervisorLog (
    "Джерело часу ініціалізовано; якщо NTP недоступний, використовується " +
    "системний час. Зона Europe/Kyiv. " +
    "Супервізор запущено. Гілка: $Branch; перевірка Git кожні " +
    "$CheckIntervalMinutes хв. Робочий процес: $WorkerName."
)
Restore-WorkerFromPidFile
$SupervisorTimer = [Diagnostics.Stopwatch]::StartNew()
$NextHeartbeatSeconds = 300.0
$NextGitCheckSeconds = 0.0

try {
    while ($true) {
        try {
            if (
                $SupervisorTimer.Elapsed.TotalSeconds -ge
                $NextGitCheckSeconds
            ) {
                try {
                    if (Test-UpdateRequired) {
                        Write-SupervisorLog (
                            "Знайдено оновлення або локальну розбіжність."
                        )
                        Stop-Worker

                        Update-Project

                        Write-SupervisorLog (
                            "Оновлення встановлено. Супервізор буде " +
                            "перезапущено для застосування нової версії."
                        )

                        # auto_update.bat постійно запускає цей файл у циклі.
                        # Завершення потрібне, щоб оновлений supervisor.ps1
                        # також завантажився, а не залишався старим процесом
                        # у пам'яті після оновлення файлів через Git.
                        exit 75
                    }
                }
                finally {
                    $NextGitCheckSeconds = (
                        $SupervisorTimer.Elapsed.TotalSeconds +
                        $CheckIntervalMinutes * 60
                    )
                }
            }

            Start-Worker

            if (
                $SupervisorTimer.Elapsed.TotalSeconds -ge
                $NextHeartbeatSeconds
            ) {
                Write-SupervisorLog (
                    "Сервер оновлень працює; перевірка Git і контроль " +
                    "Python-процесу активні."
                )
                $NextHeartbeatSeconds = (
                    $SupervisorTimer.Elapsed.TotalSeconds + 300
                )
            }
        }
        catch {
            Write-SupervisorLog "Помилка: $($_.Exception.Message)"

            # Якщо Git недоступний, робочий скрипт однаково має продовжувати роботу.
            try {
                Start-Worker
            }
            catch {
                Write-SupervisorLog "Не вдалося запустити скрипт: $($_.Exception.Message)"
            }
        }

        Start-Sleep -Seconds 60
    }
}
finally {
    Stop-Worker
}
