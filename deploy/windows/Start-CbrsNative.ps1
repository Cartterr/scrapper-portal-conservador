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
& $python $runner $EnvFile -- $python -m cbrs readiness --target windows --env-file $EnvFile --config 'G:\CBRS\account-pool.json' --json-report $readiness
if ($LASTEXITCODE -ne 0) { throw 'Native readiness failed. No task was started.' }

$taskNames = @('CBRS Worker', 'CBRS Dashboard', 'CBRS Daily Backup')
$previouslyEnabled = @{}
$startedByThisRun = [Collections.Generic.List[string]]::new()
foreach ($name in $taskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
    $previouslyEnabled[$name] = [bool]$task.Settings.Enabled
}

try {
    foreach ($name in $taskNames) {
        Enable-ScheduledTask -TaskName $name | Out-Null
    }
    foreach ($name in @('CBRS Dashboard', 'CBRS Worker')) {
        if ((Get-ScheduledTask -TaskName $name).State -ne 'Running') {
            Start-ScheduledTask -TaskName $name
            $startedByThisRun.Add($name)
        }
    }

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Seconds 2
        $persistentTasksRunning = @(
            Get-ScheduledTask -TaskName 'CBRS Dashboard','CBRS Worker' |
                Where-Object State -eq 'Running'
        ).Count -eq 2
        $dashboardHealthy = $false
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 3
            $dashboardHealthy = [bool]$health.ok
        } catch {
            $dashboardHealthy = $false
        }
    } until (($persistentTasksRunning -and $dashboardHealthy) -or [DateTimeOffset]::UtcNow -ge $deadline)

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
