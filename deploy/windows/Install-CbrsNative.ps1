[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$StateRoot = (Join-Path $RepoRoot '.cbrs\runtime'),
    [string]$BackupRepository = (Join-Path $RepoRoot '.cbrs\runtime\backup\restic'),
    [string]$EnvFile = (Join-Path $RepoRoot '.env'),
    [switch]$InstallDevelopmentRequirements,
    [switch]$PlanOnly
)

$ErrorActionPreference = 'Stop'

function Assert-LastExitCode {
    param([Parameter(Mandatory = $true)][string]$Operation)
    if ($LASTEXITCODE -ne 0) { throw "$Operation failed with exit code $LASTEXITCODE." }
}

function Resolve-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string[]]$Candidates = @()
    )
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Merge-DotEnvTemplate {
    param(
        [Parameter(Mandatory = $true)][string]$TemplatePath,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )
    if (-not (Test-Path -LiteralPath $TargetPath)) {
        Copy-Item -LiteralPath $TemplatePath -Destination $TargetPath
        return
    }
    $targetLines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in [IO.File]::ReadAllLines($TargetPath)) { $targetLines.Add($line) }
    $existingKeys = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($line in $targetLines) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=') {
            [void]$existingKeys.Add($Matches[1])
        }
    }
    $addedHeader = $false
    foreach ($line in [IO.File]::ReadAllLines($TemplatePath)) {
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=') { continue }
        if ($existingKeys.Contains($Matches[1])) { continue }
        if (-not $addedHeader) {
            $targetLines.Add('')
            $targetLines.Add('# Added by Install-CbrsNative.ps1; replace required placeholders locally.')
            $addedHeader = $true
        }
        $targetLines.Add($line)
        [void]$existingKeys.Add($Matches[1])
    }
    [IO.File]::WriteAllLines($TargetPath, $targetLines, [Text.UTF8Encoding]::new($false))
}

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $lines = [System.Collections.Generic.List[string]]::new()
    $replaced = $false
    foreach ($line in [IO.File]::ReadAllLines($TargetPath)) {
        if ($line -match "^$([regex]::Escape($Key))=") {
            if (-not $replaced) { $lines.Add("$Key=$Value") }
            $replaced = $true
        } else {
            $lines.Add($line)
        }
    }
    if (-not $replaced) { $lines.Add("$Key=$Value") }
    [IO.File]::WriteAllLines($TargetPath, $lines, [Text.UTF8Encoding]::new($false))
}

function Set-CbrsSecretAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $fileAcl = [Security.AccessControl.FileSecurity]::new()
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.SetOwner($currentIdentity.User)
    $allowedIdentities = @(
        $currentIdentity.User,
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    )
    foreach ($allowedIdentity in $allowedIdentities) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $allowedIdentity,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$fileAcl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $fileAcl
}

if ($PlanOnly) {
    Write-Host 'Plan de instalacion CBRS nativa (sin cambios):'
    Write-Host '1. Validar Windows, winget y espacio disponible en el repositorio.'
    Write-Host '2. Instalar o reutilizar Python 3.14, Google Chrome y restic.'
    Write-Host '3. Crear el entorno Python e instalar requirements.txt.'
    Write-Host '4. Crear templates DataImpulse Mobile para tres cuentas aisladas.'
    Write-Host '5. Proteger cbrs.env y restic-password con ACL local.'
    Write-Host '6. Registrar worker, dashboard, watchdog y backup deshabilitados.'
    Write-Host '7. No iniciar Chrome ni trafico CBRS hasta readiness y autorizacion explicita.'
    return
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer from an elevated native Windows PowerShell session.'
}
if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
    throw 'winget is required to install native dependencies.'
}

if (-not (Test-Path -LiteralPath 'C:\Program Files\Google\Chrome\Application\chrome.exe')) {
    & winget.exe install --exact --id Google.Chrome --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
    Assert-LastExitCode 'Google Chrome installation'
}

$resticCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\restic.exe'),
    (Join-Path $env:ProgramFiles 'WinGet\Links\restic.exe')
)
$resticPackageRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
if (Test-Path -LiteralPath $resticPackageRoot) {
    $resticCandidates += @(
        Get-ChildItem -LiteralPath $resticPackageRoot -Filter 'restic*.exe' -Recurse -File -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    )
}
$resticExecutable = Resolve-NativeCommand -Name 'restic.exe' -Candidates $resticCandidates
if (-not $resticExecutable) {
    & winget.exe install --exact --id restic.restic --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
    $resticInstallExitCode = $LASTEXITCODE
    if (Test-Path -LiteralPath $resticPackageRoot) {
        $resticCandidates += @(
            Get-ChildItem -LiteralPath $resticPackageRoot -Filter 'restic*.exe' -Recurse -File -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
        )
    }
    $resticExecutable = Resolve-NativeCommand -Name 'restic.exe' -Candidates $resticCandidates
    if (-not $resticExecutable) {
        throw "restic installation failed or restic.exe was not found (exit code $resticInstallExitCode)."
    }
}

$pythonExecutable = Resolve-NativeCommand -Name 'python.exe'
if (-not $pythonExecutable) {
    & winget.exe install --exact --id Python.Python.3.14 --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
    Assert-LastExitCode 'Python 3.14 installation'
    $pythonExecutable = Resolve-NativeCommand -Name 'python.exe' -Candidates @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe')
    )
    if (-not $pythonExecutable) {
        throw 'Python 3.14 installed but python.exe was not found; reopen PowerShell and retry.'
    }
}
$pythonVersion = & $pythonExecutable -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))'
Assert-LastExitCode 'Python version inspection'
if ([version]$pythonVersion -lt [version]'3.14') { throw 'Python 3.14 or newer is required.' }

foreach ($path in @(
    $StateRoot,
    (Join-Path $StateRoot 'accounts'),
    (Join-Path $StateRoot 'outputs'),
    (Join-Path $StateRoot 'pool'),
    (Join-Path $StateRoot 'backup'),
    (Join-Path $StateRoot 'logs'),
    (Join-Path $StateRoot 'readiness'),
    (Join-Path $StateRoot 'install'),
    (Join-Path $StateRoot 'secrets'),
    (Join-Path $StateRoot 'tmp'),
    (Join-Path $StateRoot 'cache'),
    $BackupRepository,
    (Join-Path $RepoRoot '.cbrs\runtime\bin'),
    (Split-Path -Parent $EnvFile)
)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }

$stableResticExecutable = (Join-Path $RepoRoot '.cbrs\runtime\bin\restic.exe')
if ($resticExecutable -ne $stableResticExecutable) {
    Copy-Item -LiteralPath $resticExecutable -Destination $stableResticExecutable -Force
}
$resticExecutable = $stableResticExecutable

$venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonExecutable -m venv (Join-Path $RepoRoot '.venv')
    Assert-LastExitCode 'Native Python virtual environment creation'
}
& $venvPython -m pip install --upgrade pip
Assert-LastExitCode 'pip upgrade'
& $venvPython -m pip install --requirement (Join-Path $RepoRoot 'requirements.txt')
Assert-LastExitCode 'Runtime dependency installation'
if ($InstallDevelopmentRequirements) {
    & $venvPython -m pip install --requirement (Join-Path $RepoRoot 'requirements-dev.txt')
    Assert-LastExitCode 'Development dependency installation'
}
& $venvPython -m pip check
Assert-LastExitCode 'Python dependency verification'

$poolTarget = Join-Path $StateRoot 'account-pool.json'
$enduranceTarget = Join-Path $StateRoot 'endurance-plan.json'
if (-not (Test-Path -LiteralPath $poolTarget)) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'deploy\account-pool.native.json.example') -Destination $poolTarget
}
if (-not (Test-Path -LiteralPath $enduranceTarget)) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'deploy\endurance-plan.json.example') -Destination $enduranceTarget
}
Merge-DotEnvTemplate -TemplatePath (Join-Path $RepoRoot 'deploy\cbrs-native.env.example') -TargetPath $EnvFile
Set-CbrsSecretAcl -Path $EnvFile

$resticPasswordFile = (Join-Path $RepoRoot '.cbrs\runtime\secrets\restic-password')
if (-not (Test-Path -LiteralPath $resticPasswordFile)) {
    $randomBytes = [byte[]]::new(48)
    $randomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $randomGenerator.GetBytes($randomBytes)
    } finally {
        $randomGenerator.Dispose()
    }
    [IO.File]::WriteAllText(
        $resticPasswordFile,
        [Convert]::ToBase64String($randomBytes),
        [Text.UTF8Encoding]::new($false)
    )
    [Array]::Clear($randomBytes, 0, $randomBytes.Length)
}
Set-CbrsSecretAcl -Path $resticPasswordFile
Set-CbrsSecretAcl -Path $EnvFile

$previousErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'SilentlyContinue'
    if (Test-Path -LiteralPath (Join-Path $BackupRepository 'config')) {
        & $resticExecutable --repo $BackupRepository --password-file $resticPasswordFile snapshots --json *> $null
        $resticProbeExitCode = $LASTEXITCODE
        if ($resticProbeExitCode -ne 0) {
            throw 'The existing restic repository could not be opened with the configured password. Preserve it and rerun with its matching password or a different -BackupRepository path.'
        }
    } else {
        & $resticExecutable --repo $BackupRepository --password-file $resticPasswordFile init *> $null
        $resticInitExitCode = $LASTEXITCODE
        if ($resticInitExitCode -ne 0) {
            throw "Encrypted restic repository initialization failed with exit code $resticInitExitCode."
        }
    }
} finally {
    $ErrorActionPreference = $previousErrorPreference
}

$taskScript = Join-Path $RepoRoot 'deploy\windows\Invoke-CbrsNativeTask.ps1'
$watchdogScript = Join-Path $RepoRoot 'deploy\windows\Invoke-CbrsRuntimeWatchdog.ps1'
$taskUser = $identity.Name
$principalTask = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$persistentTaskSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$backupTaskSettings = New-ScheduledTaskSettingsSet `
    -RestartCount 20 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

function New-CbrsHiddenTaskAction {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$ScriptArguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $powershellArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`" $ScriptArguments"
    $silentRun = Join-Path $env:LOCALAPPDATA 'Programs\NativeTaskLauncher\SilentRun.exe'
    if (Test-Path -LiteralPath $silentRun) {
        return New-ScheduledTaskAction `
            -Execute $silentRun `
            -Argument "--wait powershell.exe $powershellArguments" `
            -WorkingDirectory $WorkingDirectory
    }
    return New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument $powershellArguments `
        -WorkingDirectory $WorkingDirectory
}

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At '02:00'
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
foreach ($task in @(
    @{ Name = 'CBRS Worker'; Role = 'worker'; Trigger = $logonTrigger },
    @{ Name = 'CBRS Dashboard'; Role = 'dashboard'; Trigger = $logonTrigger },
    @{ Name = 'CBRS Daily Backup'; Role = 'backup'; Trigger = $dailyTrigger }
)) {
    $existingTask = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
    $preserveEnabled = $existingTask -and $existingTask.State -ne 'Disabled'
    $taskArguments = "-Role $($task.Role) -RepoRoot `"$RepoRoot`" -EnvFile `"$EnvFile`""
    $action = New-CbrsHiddenTaskAction -ScriptPath $taskScript -ScriptArguments $taskArguments -WorkingDirectory $RepoRoot
    $settings = if ($task.Role -eq 'backup') { $backupTaskSettings } else { $persistentTaskSettings }
    Register-ScheduledTask -TaskName $task.Name -Action $action -Trigger $task.Trigger -Principal $principalTask -Settings $settings -Force | Out-Null
    if ($preserveEnabled) {
        Enable-ScheduledTask -TaskName $task.Name | Out-Null
    } else {
        Disable-ScheduledTask -TaskName $task.Name | Out-Null
    }
}
$existingWatchdog = Get-ScheduledTask -TaskName 'CBRS Runtime Watchdog' -ErrorAction SilentlyContinue
$preserveWatchdogEnabled = $existingWatchdog -and $existingWatchdog.State -ne 'Disabled'
$watchdogArguments = '-TaskScope System'
$watchdogAction = New-CbrsHiddenTaskAction -ScriptPath $watchdogScript -ScriptArguments $watchdogArguments -WorkingDirectory $RepoRoot
Register-ScheduledTask -TaskName 'CBRS Runtime Watchdog' -Action $watchdogAction -Trigger $watchdogTrigger -Principal $principalTask -Settings $backupTaskSettings -Force | Out-Null
if ($preserveWatchdogEnabled) {
    Enable-ScheduledTask -TaskName 'CBRS Runtime Watchdog' | Out-Null
} else {
    Disable-ScheduledTask -TaskName 'CBRS Runtime Watchdog' | Out-Null
}

$installStatus = [ordered]@{
    schema = 'cbrs-native-install-v1'
    installed_at = [DateTimeOffset]::UtcNow.ToString('o')
    python_version = $pythonVersion
    restic_installed = $true
    chrome_installed = $true
    runtime_requirements_installed = $true
    development_requirements_installed = [bool]$InstallDevelopmentRequirements
    restic_repository_initialized = $true
    scheduled_tasks_registered = 4
    scheduled_tasks_started = $false
    live_traffic_started = $false
}
$installStatus | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $StateRoot 'install\status.json') -Encoding utf8

Write-Host 'Native CBRS runtime installed. Tasks remain disabled and no live CBRS traffic was started.'
Write-Host 'Complete the account/proxy values in the repository .env, then run readiness. Directories are automatic.'
