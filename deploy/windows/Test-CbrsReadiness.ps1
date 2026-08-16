[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$DistroName = 'Ubuntu-24.04',
    [string]$PythonCommand = 'python',
    [string]$EnvironmentFile = '.env',
    [string]$AccountPoolConfig = '.cbrs/account-pool.json',
    [string]$ReportPath = '.cbrs/readiness/indefinite-test.json',
    [switch]$ProbeWslRuntime
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path

if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {
    throw "Python command not found: $PythonCommand"
}

$arguments = @(
    '-m', 'cbrs', 'readiness',
    '--target', 'wsl',
    '--distro', $DistroName,
    '--env-file', $EnvironmentFile,
    '--config', $AccountPoolConfig,
    '--json-report', $ReportPath
)
if ($ProbeWslRuntime) {
    $arguments += '--probe-wsl-runtime'
}

Push-Location $repoRoot
try {
    & $PythonCommand @arguments
    $readinessExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($readinessExitCode -eq 0) {
    Write-Host 'The selected WSL environment is ready for the explicit live gate.'
} else {
    Write-Host 'The live gate is not ready. No setup, browser, or CBRS traffic was started.'
}
exit $readinessExitCode
