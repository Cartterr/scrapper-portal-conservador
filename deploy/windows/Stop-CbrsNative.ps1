[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env'
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $RepoRoot 'deploy\run_with_env.py'
if (Select-String -LiteralPath $EnvFile -Pattern '^CBRS_BROWSER_OWNER_MODE=external\s*$' -Quiet) {
    # Ordinary service maintenance detaches the worker, not the browser owner.
    # Closing the owner requires its separate explicit `browser_owner stop`.
    $workerName = if (Get-ScheduledTask -TaskName 'CBRS User Worker' -ErrorAction SilentlyContinue) { 'CBRS User Worker' } else { 'CBRS Worker' }
    Disable-ScheduledTask -TaskName $workerName | Out-Null
    & $python $runner $EnvFile -- $python -m cbrs jobs endurance pause
    & $python $runner $EnvFile -- $python -m cbrs pool stop
    Write-Host 'Worker drain requested; independent owner and Chrome preserved. Dashboard remains available.'
    return
}
& $python $runner $EnvFile -- $python -m cbrs jobs endurance pause
& $python $runner $EnvFile -- $python -m cbrs pool stop
Start-Sleep -Seconds 2
foreach ($name in @(
    'CBRS Worker',
    'CBRS Dashboard',
    'CBRS User Worker',
    'CBRS User Dashboard',
    'CBRS Runtime Watchdog',
    'CBRS User Runtime Watchdog'
)) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Out-Null
}
function Stop-CbrsWorkerProcesses {
    $repoPattern = [regex]::Escape([IO.Path]::GetFullPath($RepoRoot))
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $workers = @(
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                Where-Object {
                    $_.CommandLine -match $repoPattern -and
                    $_.CommandLine -match '(?i)-m\s+cbrs\s+--headless\s+jobs\s+worker'
                }
        )
        if (-not $workers) { return }
        $parentIds = @($workers | ForEach-Object { [int]$_.ParentProcessId })
        $leaves = @($workers | Where-Object { [int]$_.ProcessId -notin $parentIds })
        foreach ($worker in $leaves) {
            Stop-Process -Id ([int]$worker.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw 'Verified CBRS worker process tree did not stop.'
}
Stop-CbrsWorkerProcesses
& $python $runner $EnvFile -- $python -c "from cbrs.jobs import WORKER_LEASE_NAME, default_job_store; s=default_job_store(); lease=s.lease(); s.release_lease(WORKER_LEASE_NAME, str(lease['owner'])) if lease else None"
& $python $runner $EnvFile -- $python -c "from cbrs.account_pool import default_pool_store; s=default_pool_store(); run=s.latest_run(dry_run=False); active=bool(run and not run.get('finished_at')); s.update_run(str(run['run_id']), status='stopped', blocked_reason='verified native stop', finished=True) if active else None"
Write-Host 'Worker and dashboard stopped; queue, completed PDFs, and endurance state were preserved.'
