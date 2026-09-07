[CmdletBinding()]
param(
    [ValidateSet('User', 'System')][string]$TaskScope = 'User',
    [string]$WorkerTaskName,
    [string]$DashboardTaskName,
    [string]$StatusUri = 'http://127.0.0.1:8765/api/status'
)

$ErrorActionPreference = 'Stop'

if (-not $WorkerTaskName) {
    $WorkerTaskName = if ($TaskScope -eq 'System') { 'CBRS Worker' } else { 'CBRS User Worker' }
}
if (-not $DashboardTaskName) {
    $DashboardTaskName = if ($TaskScope -eq 'System') { 'CBRS Dashboard' } else { 'CBRS User Dashboard' }
}

function Ensure-TaskRunning {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if (-not $task.Settings.Enabled) {
        return
    }
    if ($task.State -ne 'Running') {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    }
}

# The worker and dashboard are long-running scheduled tasks. Windows Task
# Scheduler does not consistently apply RestartCount when a nested child is
# terminated, so this short periodic task is the independent recovery path.
Ensure-TaskRunning -TaskName $DashboardTaskName
$workerTask = Get-ScheduledTask -TaskName $WorkerTaskName -ErrorAction Stop
if (-not $workerTask.Settings.Enabled) {
    exit 0
}

# A Scheduled Task can remain "Running" after its nested worker has died. The
# SQLite lease exposed by the loopback-only dashboard is the authoritative
# single-owner heartbeat. Never restart on an unavailable dashboard alone.
try {
    $status = Invoke-RestMethod -Uri $StatusUri -TimeoutSec 5
    $lease = $status.jobs.summary.worker
    $expiresAt = if ($lease) { [string]$lease.expires_at } else { '' }
    $leaseActive = $expiresAt -and (
        [DateTimeOffset]::Parse($expiresAt) -ge [DateTimeOffset]::UtcNow
    )
    $independentOwner = $status.runtime.owner_mode -eq 'external'
    $ownerHealthy = $status.runtime.browser_owner -and (
        [DateTimeOffset]::Parse([string]$status.runtime.browser_owner.expires_at) -ge [DateTimeOffset]::UtcNow
    )
    if ($independentOwner -and -not $ownerHealthy) {
        Write-Warning 'Independent owner unavailable: preserve all surviving browsers; operator inspection required.'
        exit 0
    }
    if ($workerTask.State -ne 'Running') {
        if ($leaseActive) {
            exit 0
        }
        $survivors = @(Get-CimInstance Win32_Process | Where-Object {
            ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*-m cbrs*jobs*worker*') -or
            (-not $independentOwner -and $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*--user-data-dir*CBRS*')
        })
        if ($survivors.Count -gt 0) {
            Write-Warning 'CBRS processes still alive: automatic start deferred to preserve sessions.'
            exit 0
        }
        Start-ScheduledTask -TaskName $WorkerTaskName -ErrorAction Stop
        exit 0
    }
    $taskInfo = Get-ScheduledTaskInfo -TaskName $WorkerTaskName -ErrorAction Stop
    $pastStartupGrace = $taskInfo.LastRunTime -and $taskInfo.LastRunTime -lt (Get-Date).AddMinutes(-2)
    $leaseExpired = $expiresAt -and [DateTimeOffset]::Parse($expiresAt) -lt [DateTimeOffset]::UtcNow
    $leaseMissing = -not $expiresAt -and $pastStartupGrace
    if ($leaseExpired -or $leaseMissing) {
        # HARD RULE: stale heartbeat does not authorize destroying live Chrome.
        Write-Warning 'CBRS heartbeat stale: worker and browser sessions preserved. Operator inspection required.'
    }
}
catch {
    # The next one-minute watchdog pass retries. Avoid killing a healthy worker
    # because the local dashboard was briefly restarting.
}

exit 0
