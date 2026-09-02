[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('worker', 'dashboard', 'backup')]
    [string]$Role,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env'
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $RepoRoot 'deploy\run_with_env.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Native virtual environment is missing." }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "CBRS environment file is missing." }

$arguments = switch ($Role) {
    # The worker owns one long-lived headless Chrome context per account and
    # reuses it until the worker stops.  Account profiles and proxy routes stay
    # isolated; Chrome is not relaunched between jobs.
    'worker' { @('-m', 'cbrs', '--headless', 'jobs', 'worker') }
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

& $python $runner $EnvFile -- $python @arguments
exit $LASTEXITCODE
