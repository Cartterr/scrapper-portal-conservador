[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env'
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $RepoRoot 'deploy\run_with_env.py'
& $python $runner $EnvFile -- $python -m cbrs jobs endurance pause
& $python $runner $EnvFile -- $python -m cbrs pool stop
Start-Sleep -Seconds 2
foreach ($name in @('CBRS Worker', 'CBRS Dashboard')) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $name | Out-Null
}
Write-Host 'Worker and dashboard stopped; queue, completed PDFs, and endurance state were preserved.'
