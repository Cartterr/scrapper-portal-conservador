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
    'worker' { @('-m', 'cbrs', '--headless', 'jobs', 'worker') }
    'dashboard' { @('-m', 'cbrs', 'jobs', 'dashboard', '--host', '127.0.0.1') }
    'backup' { @('-m', 'cbrs', 'jobs', 'backup') }
}

& $python $runner $EnvFile -- $python @arguments
exit $LASTEXITCODE
