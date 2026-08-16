[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$DistroName = 'Ubuntu-24.04',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'wsl.exe is unavailable. Enable the Windows Subsystem for Linux feature first.'
}

$installed = @(
    & wsl.exe --list --quiet 2>$null |
        ForEach-Object { ([string]$_).Replace([char]0, '').Trim() } |
        Where-Object { $_ }
)
if ($installed -contains $DistroName) {
    Write-Host "$DistroName is already installed. No changes made."
    exit 0
}

if (-not $Apply) {
    Write-Host 'Preview only. No setup was started.'
    Write-Host "Planned command: wsl.exe --install --distribution $DistroName --no-launch"
    Write-Host "When approved, rerun: .\deploy\windows\Install-CbrsWsl.ps1 -Apply -DistroName $DistroName"
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdministrator) {
    throw 'The -Apply operation requires an elevated PowerShell session.'
}

& wsl.exe --install --distribution $DistroName --no-launch
if ($LASTEXITCODE -ne 0) {
    throw "WSL installation failed with exit code $LASTEXITCODE."
}

Write-Host "$DistroName was installed without launching it."
Write-Host 'A reboot may be requested by Windows. Initialize the distro only during the approved setup stage.'
