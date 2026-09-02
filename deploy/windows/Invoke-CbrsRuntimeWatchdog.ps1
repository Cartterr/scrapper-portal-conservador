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
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    }
    if ($task.State -ne 'Running') {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    }
}

function Stop-VerifiedWorkerChild {
    param([string]$LeaseOwner)

    $candidatePid = $null
    if ($LeaseOwner -match '-(?<pid>\d+)-[0-9a-f]+$') {
        $candidatePid = [int]$Matches.pid
    }
    $workers = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -eq 'python.exe' -and
                $_.CommandLine -like '*-m cbrs*jobs*worker*'
            }
    )
    if ($candidatePid) {
        $workers = @($workers | Where-Object ProcessId -eq $candidatePid)
    } elseif ($workers.Count -gt 0) {
        $workerParentIds = @($workers | ForEach-Object ParentProcessId)
        $workers = @($workers | Where-Object ProcessId -notin $workerParentIds)
    }
    if ($workers.Count -gt 1) {
        throw 'Refusing watchdog restart because more than one worker child is present.'
    }
    if ($workers.Count -eq 1) {
        Stop-Process -Id $workers[0].ProcessId -Force -ErrorAction Stop
    }
}

# The worker and dashboard are long-running scheduled tasks. Windows Task
# Scheduler does not consistently apply RestartCount when a nested child is
# terminated, so this short periodic task is the independent recovery path.
Ensure-TaskRunning -TaskName $DashboardTaskName
$workerTask = Get-ScheduledTask -TaskName $WorkerTaskName -ErrorAction Stop
if (-not $workerTask.Settings.Enabled) {
    Enable-ScheduledTask -TaskName $WorkerTaskName -ErrorAction Stop | Out-Null
    $workerTask = Get-ScheduledTask -TaskName $WorkerTaskName -ErrorAction Stop
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
    if ($workerTask.State -ne 'Running') {
        if ($leaseActive) {
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
        Stop-ScheduledTask -TaskName $WorkerTaskName -ErrorAction Stop
        Stop-VerifiedWorkerChild -LeaseOwner ([string]$lease.owner)
        Start-Sleep -Seconds 2
        $survivors = @(
            Get-CimInstance Win32_Process |
                Where-Object {
                    $_.Name -eq 'python.exe' -and
                    $_.CommandLine -like '*-m cbrs*jobs*worker*'
                }
        )
        if ($survivors.Count -gt 0) {
            throw 'Refusing watchdog restart while a prior worker child survives.'
        }
        Start-ScheduledTask -TaskName $WorkerTaskName -ErrorAction Stop
    }
}
catch {
    # The next one-minute watchdog pass retries. Avoid killing a healthy worker
    # because the local dashboard was briefly restarting.
}

exit 0
