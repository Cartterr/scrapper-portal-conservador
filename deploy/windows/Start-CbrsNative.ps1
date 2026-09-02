[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env',
    [switch]$AcknowledgeAuthorizedLiveTraffic
)

$ErrorActionPreference = 'Stop'
if (-not $AcknowledgeAuthorizedLiveTraffic) {
    throw 'Refusing to start authorized CBRS traffic. Rerun with -AcknowledgeAuthorizedLiveTraffic.'
}
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $RepoRoot 'deploy\run_with_env.py'
$readiness = Join-Path 'G:\CBRS' 'readiness\pre-live.json'
$operationalReadiness = Join-Path 'G:\CBRS' 'readiness\operational.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $readiness) -Force | Out-Null
& $python $runner $EnvFile -- $python -m cbrs jobs recover
if ($LASTEXITCODE -ne 0) { throw 'Expired worker-state recovery failed. No task was started.' }
& $python $runner $EnvFile -- $python -m cbrs readiness --target windows --env-file $EnvFile --config 'G:\CBRS\account-pool.json' --json-report $readiness
if ($LASTEXITCODE -ne 0) { throw 'Native readiness failed. No task was started.' }

$persistentTaskSettings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
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

$legacyTaskNames = @('CBRS Worker', 'CBRS Dashboard', 'CBRS Daily Backup', 'CBRS Runtime Watchdog')
$userTaskNames = @('CBRS User Worker', 'CBRS User Dashboard', 'CBRS User Daily Backup', 'CBRS User Runtime Watchdog')
$taskNames = $legacyTaskNames
$persistentTaskNames = @($taskNames[0], $taskNames[1])
$previouslyEnabled = @{}
$startedByThisRun = [Collections.Generic.List[string]]::new()
$headedRuntime = [bool](
    Select-String `
        -LiteralPath $EnvFile `
        -Pattern '^CBRS_HEADLESS\s*=\s*(0|false|no|off)\s*$' `
        -ErrorAction SilentlyContinue
)
$legacyConfigured = $false

if (-not $headedRuntime) {
    try {
        foreach ($name in $legacyTaskNames) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
            $previouslyEnabled[$name] = [bool]$task.Settings.Enabled
        }
        foreach ($name in $legacyTaskNames[0..1]) {
            Set-ScheduledTask -TaskName $name -Settings $persistentTaskSettings -ErrorAction Stop | Out-Null
        }
        Set-ScheduledTask -TaskName $legacyTaskNames[2] -Settings $backupTaskSettings -ErrorAction Stop | Out-Null
        $legacyConfigured = $true
    } catch {
        $legacyConfigured = $false
    }
}

if (-not $legacyConfigured) {
    $taskNames = $userTaskNames
    $persistentTaskNames = @($taskNames[0], $taskNames[1])
    $previouslyEnabled = @{}
    foreach ($name in $taskNames) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        $previouslyEnabled[$name] = [bool]($existing -and $existing.Settings.Enabled)
    }
    $taskScript = Join-Path $RepoRoot 'deploy\windows\Invoke-CbrsNativeTask.ps1'
    $watchdogScript = Join-Path $RepoRoot 'deploy\windows\Invoke-CbrsRuntimeWatchdog.ps1'
    $taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
    $dailyTrigger = New-ScheduledTaskTrigger -Daily -At '02:00'
    $watchdogTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    foreach ($task in @(
        @{ Name = $taskNames[0]; Role = 'worker'; Trigger = $logonTrigger; Settings = $persistentTaskSettings },
        @{ Name = $taskNames[1]; Role = 'dashboard'; Trigger = $logonTrigger; Settings = $persistentTaskSettings },
        @{ Name = $taskNames[2]; Role = 'backup'; Trigger = $dailyTrigger; Settings = $backupTaskSettings }
    )) {
        $arguments = "-Role $($task.Role) -RepoRoot `"$RepoRoot`" -EnvFile `"$EnvFile`""
        $action = New-CbrsHiddenTaskAction -ScriptPath $taskScript -ScriptArguments $arguments -WorkingDirectory $RepoRoot
        Register-ScheduledTask -TaskName $task.Name -Action $action -Trigger $task.Trigger -Principal $principal -Settings $task.Settings -Force -ErrorAction Stop | Out-Null
    }
    # Keep the SilentRun argument vector free of quoted values. Its native
    # parser treats a quoted multi-word option value as a child-launch error.
    $watchdogArguments = '-TaskScope User'
    $watchdogAction = New-CbrsHiddenTaskAction -ScriptPath $watchdogScript -ScriptArguments $watchdogArguments -WorkingDirectory $RepoRoot
    Register-ScheduledTask -TaskName $taskNames[3] -Action $watchdogAction -Trigger $watchdogTrigger -Principal $principal -Settings $backupTaskSettings -Force -ErrorAction Stop | Out-Null
}

try {
    foreach ($name in $taskNames) {
        Enable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null
    }
    foreach ($name in @($persistentTaskNames[1], $persistentTaskNames[0])) {
        if ((Get-ScheduledTask -TaskName $name).State -ne 'Running') {
            Start-ScheduledTask -TaskName $name -ErrorAction Stop
            $startedByThisRun.Add($name)
        }
    }
    Start-ScheduledTask -TaskName $taskNames[3] -ErrorAction Stop

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Seconds 2
        $persistentTasksAvailable = @(
            Get-ScheduledTask -TaskName $persistentTaskNames |
                Where-Object {
                    $_.Settings.Enabled -and $_.State -in @('Ready', 'Running')
                }
        ).Count -eq 2
        $dashboardHealthy = $false
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 3
            $dashboardHealthy = [bool]$health.ok
        } catch {
            $dashboardHealthy = $false
        }
    } until (($persistentTasksAvailable -and $dashboardHealthy) -or [DateTimeOffset]::UtcNow -ge $deadline)

    $browserDeadline = [DateTimeOffset]::UtcNow.AddMinutes(3)
    $browserRuntimeReady = $false
    do {
        Start-Sleep -Seconds 3
        try {
            $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/status' -TimeoutSec 5
            $expected = [int]($status.pool.browser_expected_count)
            $live = [int]($status.pool.browser_live_count)
            $authenticated = [int]($status.pool.browser_authenticated_count)
            $protectedForms = @(
                $status.accounts |
                    Where-Object browser_auth_state -eq 'authenticated_form'
            ).Count
            $browserRuntimeReady = (
                $expected -eq 3 -and
                $live -eq $expected -and
                $authenticated -eq $expected -and
                $protectedForms -eq $expected
            )
        } catch {
            $browserRuntimeReady = $false
        }
    } until ($browserRuntimeReady -or [DateTimeOffset]::UtcNow -ge $browserDeadline)
    if (-not $browserRuntimeReady) {
        throw 'Three Chrome contexts did not prove their protected forms in time.'
    }

    & $python $runner $EnvFile -- $python -m cbrs readiness --target windows --require-active-runtime --env-file $EnvFile --config 'G:\CBRS\account-pool.json' --json-report $operationalReadiness
    if ($LASTEXITCODE -ne 0) {
        throw 'Operational readiness failed after startup.'
    }
    Write-Host 'CBRS native endurance runtime started and verified. Dashboard: http://127.0.0.1:8765'
} catch {
    foreach ($name in $startedByThisRun) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    }
    foreach ($name in $taskNames) {
        if (-not $previouslyEnabled[$name]) {
            Disable-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Out-Null
        }
    }
    throw
}
