[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$InstallDir = "C:\Python-Scan"
$Repository = "https://github.com/HalatsynZakhar/Python-Scan.git"
$InstallLog = "C:\Python-Scan-install.log"
$TranscriptStarted = $false
$TaskRunAs = "SYSTEM"
$TaskPassword = $null

[Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor `
    [Net.SecurityProtocolType]::Tls12

try {
    Start-Transcript -Path $InstallLog -Append -Force | Out-Null
    $TranscriptStarted = $true
}
catch {
    Write-Warning "Не вдалося увімкнути журнал встановлення: $($_.Exception.Message)"
}

trap {
    $ErrorMessage = $_.Exception.Message
    $ErrorPosition = $_.InvocationInfo.PositionMessage

    Write-Host ""
    Write-Host "ПОМИЛКА ВСТАНОВЛЕННЯ" -ForegroundColor Red
    Write-Host $ErrorMessage -ForegroundColor Red
    if ($ErrorPosition) {
        Write-Host $ErrorPosition -ForegroundColor DarkRed
    }
    Write-Host ""
    Write-Host "Журнал: $InstallLog" -ForegroundColor Yellow

    if ($TranscriptStarted) {
        Stop-Transcript | Out-Null
        $TranscriptStarted = $false
    }

    Read-Host "Натисніть Enter, щоб закрити вікно"
    exit 1
}

function Update-CurrentPath {
    $MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$MachinePath;$UserPath"
}

function Stop-ExistingImagesXmlRuntime {
    Write-Output "Зупинка попереднього екземпляра ImagesXML..."

    Start-Process `
        -FilePath "schtasks.exe" `
        -ArgumentList @("/End", "/TN", "ImagesXML") `
        -WindowStyle Hidden `
        -Wait | Out-Null

    $PythonScript = Join-Path $InstallDir "images_xml.py"
    $ServerScript = Join-Path $InstallDir "image_sync_server.py"
    $SupervisorScript = Join-Path $InstallDir "scripts\supervisor.ps1"
    $AutoUpdateBat = Join-Path $InstallDir "scripts\auto_update.bat"

    $ProjectProcesses = Get-CimInstance Win32_Process |
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
        }

    foreach ($Process in $ProjectProcesses) {
        try {
            Stop-Process -Id $Process.ProcessId -Force -ErrorAction Stop
            Write-Output (
                "Зупинено старий процес: $($Process.Name), " +
                "PID $($Process.ProcessId)"
            )
        }
        catch {
            Write-Warning (
                "Не вдалося зупинити PID $($Process.ProcessId): " +
                $_.Exception.Message
            )
        }
    }

    Remove-Item `
        -LiteralPath (Join-Path $InstallDir "logs\images_xml.pid") `
        -Force `
        -ErrorAction SilentlyContinue
}

function Install-WingetPackage {
    param(
        [string]$PackageId,
        [string]$DisplayName
    )

    Write-Output "Встановлення: $DisplayName"
    winget install `
        --id $PackageId `
        --exact `
        --silent `
        --accept-package-agreements `
        --accept-source-agreements

    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося встановити $DisplayName через winget."
    }
}

function Assert-ValidSignature {
    param(
        [string]$Path,
        [string]$DisplayName
    )

    $Signature = Get-AuthenticodeSignature -FilePath $Path
    if ($Signature.Status -ne "Valid") {
        throw "Недійсний цифровий підпис інсталятора $DisplayName."
    }
}

function Install-PythonDirect {
    $Version = "3.13.14"
    $Installer = Join-Path $env:TEMP "python-$Version-amd64.exe"
    $Url = "https://www.python.org/ftp/python/$Version/python-$Version-amd64.exe"

    Write-Output "Завантаження Python $Version із python.org..."
    Invoke-WebRequest -Uri $Url -OutFile $Installer -UseBasicParsing
    Assert-ValidSignature -Path $Installer -DisplayName "Python"

    $Process = Start-Process `
        -FilePath $Installer `
        -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" `
        -Wait `
        -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "Інсталятор Python завершився з кодом $($Process.ExitCode)."
    }
}

function Install-GitDirect {
    Write-Output "Отримання останньої версії Git for Windows..."
    $Release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" `
        -Headers @{ "User-Agent" = "Python-Scan-Installer" }

    $Asset = $Release.assets |
        Where-Object { $_.name -match "^Git-.+-64-bit\.exe$" } |
        Select-Object -First 1
    if ($null -eq $Asset) {
        throw "Не знайдено 64-бітний інсталятор Git for Windows."
    }

    $Installer = Join-Path $env:TEMP $Asset.name
    Write-Output "Завантаження $($Asset.name)..."
    Invoke-WebRequest `
        -Uri $Asset.browser_download_url `
        -OutFile $Installer `
        -UseBasicParsing
    Assert-ValidSignature -Path $Installer -DisplayName "Git"

    $Process = Start-Process `
        -FilePath $Installer `
        -ArgumentList "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES /SP-" `
        -Wait `
        -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "Інсталятор Git завершився з кодом $($Process.ExitCode)."
    }
}

function Test-PythonInstalled {
    foreach ($Command in @("py", "python")) {
        if (!(Get-Command $Command -ErrorAction SilentlyContinue)) {
            continue
        }

        & $Command --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
    }

    return $false
}

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @("py", "-3")
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @("python")
    }
    throw "Python 3 не знайдено після встановлення."
}

function Test-UncPath {
    param([string]$Path)
    return $Path.StartsWith("\\")
}

function Grant-LocalFolderAccess {
    param(
        [string]$Path,
        [string]$Identity,
        [ValidateSet("Read", "Modify")]
        [string]$Access
    )

    New-Item -ItemType Directory -Path $Path -Force | Out-Null

    $AclIdentity = if ($Identity -eq "SYSTEM") {
        # Language-independent SID of NT AUTHORITY\SYSTEM.
        "*S-1-5-18"
    }
    else {
        $Identity
    }
    $Rights = if ($Access -eq "Read") { "RX" } else { "M" }

    $AccessLabel = if ($Access -eq "Read") {
        "читання"
    }
    else {
        "зміна"
    }
    Write-Output (
        "Налаштування прав ($AccessLabel) для ${Identity}: $Path"
    )
    & icacls.exe $Path `
        /grant "${AclIdentity}:(OI)(CI)$Rights" `
        /T `
        /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося надати права $Identity на $Path."
    }
}

function Convert-SecureStringToPlainText {
    param([Security.SecureString]$SecureString)

    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $SecureString
    )
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

function Configure-StorageAccess {
    $ConfigPath = Join-Path $InstallDir "config.json"
    try {
        $Config = Get-Content $ConfigPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        throw (
            "config.json містить помилку JSON: $($_.Exception.Message) " +
            "У шляхах Windows використовуйте подвійні зворотні слеші, наприклад: " +
            '"D:\\ShareFiles\\public\\foto".'
        )
    }

    if (!$Config.images_dir) {
        throw "У config.json не вказано images_dir."
    }

    $ImagesPath = [string]$Config.images_dir
    $OutputPath = if ($Config.output_dir) {
        [string]$Config.output_dir
    }
    elseif ($Config.output_xml) {
        Split-Path ([string]$Config.output_xml) -Parent
    }
    else {
        throw "У config.json не вказано output_dir."
    }

    $UsesNetworkPath = (
        (Test-UncPath $ImagesPath) -or
        (Test-UncPath $OutputPath)
    )

    if ($UsesNetworkPath) {
        Write-Host ""
        Write-Host "ВИЯВЛЕНО МЕРЕЖЕВИЙ UNC-ШЛЯХ" -ForegroundColor Yellow
        Write-Host (
            "Права SMB і NTFS мають бути заздалегідь надані на файловому сервері."
        ) -ForegroundColor Yellow
        Write-Host (
            "Завдання буде створено від Windows-користувача з доступом до спільної папки."
        ) -ForegroundColor Yellow

        $DefaultUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $EnteredUser = Read-Host (
            "Користувач завдання [$DefaultUser]"
        )
        $script:TaskRunAs = if ([string]::IsNullOrWhiteSpace($EnteredUser)) {
            $DefaultUser
        }
        else {
            $EnteredUser.Trim()
        }

        $SecurePassword = Read-Host `
            "Пароль користувача $script:TaskRunAs" `
            -AsSecureString
        if ($SecurePassword.Length -eq 0) {
            throw "Для мережевої папки пароль користувача не може бути порожнім."
        }
        $script:TaskPassword = Convert-SecureStringToPlainText `
            -SecureString $SecurePassword
    }
    else {
        $ConfiguredUser = [string]$Config.task_user
        $script:TaskRunAs = if (
            [string]::IsNullOrWhiteSpace($ConfiguredUser)
        ) {
            "SYSTEM"
        }
        else {
            $ConfiguredUser.Trim()
        }

        if ($script:TaskRunAs -ne "SYSTEM") {
            $SecurePassword = Read-Host `
                "Пароль користувача $script:TaskRunAs" `
                -AsSecureString
            $script:TaskPassword = Convert-SecureStringToPlainText `
                -SecureString $SecurePassword
        }
    }

    if (!(Test-UncPath $ImagesPath)) {
        Grant-LocalFolderAccess `
            -Path $ImagesPath `
            -Identity $script:TaskRunAs `
            -Access Read
    }
    if (!(Test-UncPath $OutputPath)) {
        Grant-LocalFolderAccess `
            -Path $OutputPath `
            -Identity $script:TaskRunAs `
            -Access Modify
    }

    # The supervisor updates Git, installs dependencies and writes local PID
    # and diagnostic files, so its account needs Modify on the whole project.
    Grant-LocalFolderAccess `
        -Path $InstallDir `
        -Identity $script:TaskRunAs `
        -Access Modify

    $SafeDirectory = $InstallDir.Replace("\", "/")
    $ExistingSafeDirectories = @(
        & $GitCommand config --system --get-all safe.directory 2>$null
    )
    if ($ExistingSafeDirectories -notcontains $SafeDirectory) {
        & $GitCommand config --system --add safe.directory $SafeDirectory
        if ($LASTEXITCODE -ne 0) {
            throw "Не вдалося додати Git safe.directory: $InstallDir"
        }
    }

    Write-Output "Завдання працюватиме від: $script:TaskRunAs"
}

function Initialize-PythonEnvironment {
    Set-Location $InstallDir

    if (!(Test-Path ".venv\Scripts\python.exe")) {
        $Python = Get-PythonCommand
        Write-Output "Створення віртуального середовища..."
        if ($Python.Count -eq 2) {
            & $Python[0] $Python[1] -m venv .venv
        }
        else {
            & $Python[0] -m venv .venv
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Не вдалося створити віртуальне середовище."
        }
    }

    $VenvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося оновити pip."
    }

    Write-Output "Встановлення бібліотек із requirements.txt..."
    & $VenvPython -m pip install -r "$InstallDir\requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося встановити бібліотеки з requirements.txt."
    }

    New-Item -ItemType Directory -Path "$InstallDir\logs" -Force |
        Out-Null
    New-Item -ItemType Directory -Path "$InstallDir\data" -Force |
        Out-Null

    & $VenvPython -m compileall -q `
        "$InstallDir\images_xml.py" `
        "$InstallDir\horoshop_sync.py" `
        "$InstallDir\image_sync_server.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Перевірка Python-коду завершилася помилкою."
    }

    Write-Output "Перевірка створення XML..."
    & $VenvPython "$InstallDir\images_xml.py" --once
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося створити XML. Перевірте config.json і права доступу."
    }
}

function Create-ImagesXmlTask {
    Stop-ExistingImagesXmlRuntime

    $AutoUpdateBat = Join-Path $InstallDir "scripts\auto_update.bat"
    $TaskAction = "`"$AutoUpdateBat`" `"main`" `"5`""

    $TaskArguments = @(
        "/Create",
        "/TN", "ImagesXML",
        "/SC", "ONSTART",
        "/TR", $TaskAction,
        "/RU", $script:TaskRunAs,
        "/RL", "HIGHEST",
        "/F"
    )
    if ($script:TaskRunAs -ne "SYSTEM") {
        $TaskArguments += @("/RP", $script:TaskPassword)
    }

    & schtasks.exe @TaskArguments
    if ($LASTEXITCODE -ne 0) {
        throw (
            "Не вдалося створити завдання ImagesXML для користувача " +
            "$script:TaskRunAs. Перевірте ім'я та пароль."
        )
    }
    $script:TaskPassword = $null

    Write-Output "Вимкнення обмеження часу виконання завдання..."
    $CreatedTask = Get-ScheduledTask -TaskName "ImagesXML"
    $CreatedTask.Settings.ExecutionTimeLimit = "PT0S"
    Set-ScheduledTask -InputObject $CreatedTask | Out-Null

    $SavedLimit = (
        Get-ScheduledTask -TaskName "ImagesXML"
    ).Settings.ExecutionTimeLimit
    if ($SavedLimit -ne "PT0S") {
        throw (
            "Не вдалося вимкнути обмеження часу завдання ImagesXML. " +
            "Поточне значення: $SavedLimit"
        )
    }
    Write-Output "Обмеження часу вимкнено: завдання працює безстроково."

    $OldTask = Start-Process `
        -FilePath "schtasks.exe" `
        -ArgumentList @("/Delete", "/TN", "ImagesXMLUpdate", "/F") `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($OldTask.ExitCode -eq 0) {
        Write-Output "Старе завдання ImagesXMLUpdate видалено."
    }
}

function Get-ConfiguredWorkerName {
    $ConfigPath = Join-Path $InstallDir "config.json"
    try {
        $Config = Get-Content $ConfigPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if ($Config.server -and [bool]$Config.server.enabled) {
            return "image_sync_server.py"
        }
    }
    catch {
        Write-Warning (
            "Не вдалося визначити робочий процес із config.json: " +
            $_.Exception.Message
        )
    }
    return "images_xml.py"
}

function Configure-Firewall {
    $ConfigPath = Join-Path $InstallDir "config.json"
    try {
        $Config = Get-Content $ConfigPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        Write-Warning "Не вдалося прочитати config.json для firewall."
        return
    }
    if (!($Config.server -and [bool]$Config.server.enabled)) {
        return
    }

    $Port = if ($Config.server.port) { [int]$Config.server.port } else { 8092 }
    $RuleName = "PythonScanHoroshopSync-$Port"

    Write-Output "Відкриття Windows Firewall TCP-порту $Port..."
    $Existing = Get-NetFirewallRule `
        -DisplayName $RuleName `
        -ErrorAction SilentlyContinue
    if ($Existing) {
        Remove-NetFirewallRule -DisplayName $RuleName
    }
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port | Out-Null
}

function Start-AndVerifyService {
    $PidFile = Join-Path $InstallDir "logs\images_xml.pid"
    $DiagnosticLog = Join-Path $InstallDir "logs\supervisor-local.log"
    $WorkerName = Get-ConfiguredWorkerName
    Remove-Item $PidFile, $DiagnosticLog -Force -ErrorAction SilentlyContinue

    schtasks /Run /TN "ImagesXML" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Завдання створено, але його не вдалося запустити."
    }

    Write-Output "Перевірка запуску Python-процесу..."
    $Deadline = (Get-Date).AddSeconds(60)
    $WorkerIsRunning = $false
    $SupervisorConfirmedStart = $false

    while (
        (Get-Date) -lt $Deadline -and
        !($WorkerIsRunning -and $SupervisorConfirmedStart)
    ) {
        Start-Sleep -Seconds 2

        if (Test-Path $PidFile) {
            $SavedPid = 0
            if (
                [int]::TryParse(
                    (Get-Content $PidFile -Raw).Trim(),
                    [ref]$SavedPid
                )
            ) {
                $ProcessInfo = Get-CimInstance Win32_Process `
                    -Filter "ProcessId = $SavedPid" `
                    -ErrorAction SilentlyContinue
                $WorkerIsRunning = (
                    $null -ne $ProcessInfo -and
                    $ProcessInfo.CommandLine -like "*$WorkerName*"
                )
            }
        }

        if (Test-Path $DiagnosticLog) {
            $RecentLines = Get-Content $DiagnosticLog -Tail 10
            $SupervisorConfirmedStart = [bool](
                $RecentLines |
                    Where-Object { $_ -match "Python-скрипт успішно запущено" }
            )
            if (
                $RecentLines |
                    Where-Object {
                        $_ -match "Python-скрипт завершився одразу після запуску"
                    }
            ) {
                break
            }
        }
    }

    if (!$WorkerIsRunning -or !$SupervisorConfirmedStart) {
        $Details = "Локальний діагностичний журнал поки порожній."
        if (Test-Path $DiagnosticLog) {
            $Details = (Get-Content $DiagnosticLog -Tail 10) -join "`n"
        }

        $TaskState = "невідомо"
        $TaskResult = "невідомо"
        try {
            $ScheduledTask = Get-ScheduledTask -TaskName "ImagesXML"
            $ScheduledTaskInfo = Get-ScheduledTaskInfo -TaskName "ImagesXML"
            $TaskState = [string]$ScheduledTask.State
            $TaskResult = "0x{0:X}" -f $ScheduledTaskInfo.LastTaskResult
        }
        catch {
            $TaskState = "не вдалося прочитати"
            $TaskResult = $_.Exception.Message
        }

        throw (
            "Завдання запущено, але Python не продовжив роботу. Ймовірна " +
            "причина: $script:TaskRunAs не має доступу до images_dir або " +
            "output_dir, або Планувальник не запустив новий екземпляр. " +
            "Стан завдання: $TaskState; результат: $TaskResult. " +
            "Діагностика: $DiagnosticLog`n$Details"
        )
    }
}

function Show-TaskAutonomyStatus {
    $Task = Get-ScheduledTask -TaskName "ImagesXML"
    $ExecutionLimit = $Task.Settings.ExecutionTimeLimit

    if ($ExecutionLimit -and $ExecutionLimit -ne "PT0S") {
        $Limit = [System.Xml.XmlConvert]::ToTimeSpan($ExecutionLimit)
        Write-Host ""
        Write-Host "ПІДСУМОК: ВСТАНОВЛЕННЯ ВИКОНАНО, АЛЕ ПОТРІБНЕ ВТРУЧАННЯ" `
            -ForegroundColor Yellow
        Write-Host (
            "Планувальник зупинить завдання через " +
            "$([math]::Round($Limit.TotalHours, 2)) годин."
        ) -ForegroundColor Yellow
        Write-Host (
            "Для повторного запуску: schtasks /Run /TN ImagesXML"
        ) -ForegroundColor Yellow
        Write-Host ""
    }
    else {
        Write-Host ""
        Write-Host "ПІДСУМОК: ВСТАНОВЛЕННЯ ПОВНІСТЮ ЗАВЕРШЕНО" `
            -ForegroundColor Green
        Write-Host "Python та автооновлення працюють постійно." `
            -ForegroundColor Green
        Write-Host ""
    }
}

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
$IsAdministrator = $Principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (!$IsAdministrator) {
    throw "Запустіть PowerShell від імені адміністратора."
}

if (!(Get-Command git -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Install-WingetPackage -PackageId "Git.Git" -DisplayName "Git"
    }
    else {
        Write-Output "winget не знайдено. Використовується пряме встановлення Git."
        Install-GitDirect
    }
}
else {
    Write-Output "Git вже встановлено."
}

if (!(Test-PythonInstalled)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Install-WingetPackage `
            -PackageId "Python.Python.3.13" `
            -DisplayName "Python 3.13"
    }
    else {
        Write-Output "winget не знайдено. Використовується пряме встановлення Python."
        Install-PythonDirect
    }
}
else {
    Write-Output "Python вже встановлено."
}

Update-CurrentPath

if (!(Get-Command git -ErrorAction SilentlyContinue)) {
    $GitCommand = "C:\Program Files\Git\cmd\git.exe"
    if (!(Test-Path $GitCommand)) {
        throw "Git встановлено, але виконуваний файл не знайдено."
    }
}
else {
    $GitCommand = (Get-Command git).Source
}

Set-Location "C:\"

if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Output "Проєкт уже існує: $InstallDir"
    Stop-ExistingImagesXmlRuntime
    Set-Location $InstallDir
    & $GitCommand fetch origin main
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося отримати оновлення проєкту."
    }
    & $GitCommand reset --hard origin/main
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося оновити наявний проєкт."
    }
}
elseif (Test-Path $InstallDir) {
    throw "Папка $InstallDir існує, але не є Git-репозиторієм."
}
else {
    & $GitCommand clone $Repository $InstallDir
    if ($LASTEXITCODE -ne 0) {
        throw "Не вдалося клонувати проєкт."
    }
}

Set-Location $InstallDir

if (!(Test-Path "config.json")) {
    Copy-Item "config.example.json" "config.json"
}

Write-Output ""
Write-Output "Заповніть config.json і закрийте Блокнот для продовження встановлення."
Start-Process notepad.exe `
    -ArgumentList "`"$InstallDir\config.json`"" `
    -Wait

Write-Output "Продовження автоматичного встановлення..."
Configure-StorageAccess
Initialize-PythonEnvironment
Configure-Firewall
Create-ImagesXmlTask
Start-AndVerifyService
Show-TaskAutonomyStatus

Write-Output ""
Write-Output "Повне встановлення завершено."
Write-Output "Проєкт: $InstallDir"
Write-Output "Журнал: $InstallLog"

if ($TranscriptStarted) {
    Stop-Transcript | Out-Null
    $TranscriptStarted = $false
}

Read-Host "Натисніть Enter для завершення"
