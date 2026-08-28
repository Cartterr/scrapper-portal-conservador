[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env'
)

$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $RepoRoot 'deploy\run_with_env.py'
Get-ScheduledTask -TaskName 'CBRS Worker','CBRS Dashboard','CBRS Daily Backup' |
    Select-Object TaskName, State
& $python $runner $EnvFile -- $python -m cbrs jobs status
