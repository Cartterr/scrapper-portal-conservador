[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_]+$')]
    [string]$Account,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env',
    [int]$TimeoutSeconds = 900,
    [switch]$AcknowledgeAuthorizedLiveTraffic
)

$ErrorActionPreference = 'Stop'
if (-not $AcknowledgeAuthorizedLiveTraffic) {
    throw 'Refusing to open a live CBRS recovery browser without -AcknowledgeAuthorizedLiveTraffic.'
}
& (Join-Path $PSScriptRoot 'Stop-CbrsNative.ps1') -RepoRoot $RepoRoot -EnvFile $EnvFile
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $RepoRoot 'deploy\run_with_env.py'
& $python $runner $EnvFile -- $python -m cbrs --headed pool init --account $Account --timeout $TimeoutSeconds
exit $LASTEXITCODE
