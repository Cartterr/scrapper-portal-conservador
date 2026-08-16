[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$DistroName = 'Ubuntu-24.04',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path

if (-not $Apply) {
    Write-Host 'Preview only. No Ubuntu packages, files, users, or services were changed.'
    Write-Host "Planned target: $DistroName"
    Write-Host "Planned source: $repoRoot"
    Write-Host 'Planned action: run deploy/install-ubuntu.sh as root; units are installed but not enabled or started.'
    Write-Host "When approved, rerun: .\deploy\windows\Initialize-CbrsRuntime.ps1 -Apply -DistroName $DistroName"
    exit 0
}

$installed = @(
    & wsl.exe --list --quiet 2>$null |
        ForEach-Object { ([string]$_).Replace([char]0, '').Trim() } |
        Where-Object { $_ }
)
if ($installed -notcontains $DistroName) {
    throw "$DistroName is not installed or has not been initialized."
}

$linuxRepoRoot = (& wsl.exe --distribution $DistroName --exec wslpath -a $repoRoot).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxRepoRoot) {
    throw 'Could not translate the repository path into the WSL filesystem.'
}

$installer = "$linuxRepoRoot/deploy/install-ubuntu.sh"
& wsl.exe --distribution $DistroName --user root --exec bash $installer
if ($LASTEXITCODE -ne 0) {
    throw "Ubuntu runtime initialization failed with exit code $LASTEXITCODE."
}

Write-Host 'Runtime files and systemd units are installed but were not enabled or started.'
Write-Host 'Complete the protected configuration, proxy baselines, and first backup before the live start gate.'
