[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('worker', 'dashboard', 'backup', 'owner')]
    [string]$Role,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = (Join-Path $RepoRoot '.env')
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $RepoRoot 'deploy\run_with_env.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Native virtual environment is missing." }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "CBRS environment file is missing." }

if ($Role -eq 'owner' -and -not (Select-String -LiteralPath $EnvFile -Pattern '^CBRS_BROWSER_OWNER_MODE=external\s*$' -Quiet)) {
    throw 'Independent owner task requires explicit external mode; refusing mixed ownership.'
}

if ($Role -eq 'worker' -and (Select-String -LiteralPath $EnvFile -Pattern '^CBRS_BROWSER_OWNER_MODE=external\s*$' -Quiet)) {
    # Owner and worker have independent logon triggers; wait for the owner,
    # never silently fall back to worker-owned Chrome.
    $deadline = (Get-Date).AddSeconds(60)
    do {
        & $python $runner $EnvFile -- $python -c "from cbrs.jobs import default_job_store; import sys; sys.exit(0 if default_job_store().active_lease('browser_owner') else 1)"
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 2
    } until ((Get-Date) -ge $deadline)
    if ($LASTEXITCODE -ne 0) { throw 'Independent owner unavailable; refusing coupled fallback.' }
}

if ($Role -eq 'dashboard') {
    # Stopping a scheduled-task wrapper can leave its nested Python server
    # alive. Windows permits reused listeners; a second server then serves
    # alternating old/new UI versions. Preserve the existing server instead.
    $existingDashboard = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m\s+cbrs\s+jobs\s+dashboard' })
    if ($existingDashboard.Count -gt 0) {
        Write-Warning 'Existing CBRS dashboard process preserved; duplicate launch skipped.'
        exit 0
    }
}

$arguments = switch ($Role) {
    'owner' { @('-m', 'cbrs.browser_owner', 'run') }
    # The worker owns one long-lived Chrome context per account and
    # reuses it until the worker stops.  Account profiles and proxy routes stay
    # isolated; Chrome is not relaunched between jobs.
    # Read CBRS_HEADLESS from the protected environment; CLI flags must not
    # silently override an operator's headed/headless selection.
    'worker' { @('-m', 'cbrs', 'jobs', 'worker') }
    'dashboard' { @('-m', 'cbrs', 'jobs', 'dashboard', '--host', '127.0.0.1') }
    'backup' { @('-m', 'cbrs', 'jobs', 'backup') }
}

if ($Role -eq 'worker') {
    # At-logon launches bypass Start-CbrsNative.ps1. Clear only expired leases
    # and recover abandoned jobs before every worker start so a reboot cannot
    # leave the queue stranded behind pre-reboot state.
    & $python $runner $EnvFile -- $python -m cbrs jobs recover
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$logDirectory = Join-Path $RepoRoot '.cbrs\runtime\logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$roleLog = Join-Path $logDirectory ($Role + '-service.log')
& $python $runner $EnvFile -- $python @arguments >> $roleLog 2>&1
exit $LASTEXITCODE
