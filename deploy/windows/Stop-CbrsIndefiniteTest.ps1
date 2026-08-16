[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$DistroName = 'Ubuntu-24.04',
    [switch]$KeepDashboard
)

$ErrorActionPreference = 'Stop'

$command = if ($KeepDashboard) {
    'systemctl stop cbrs-worker.service'
} else {
    @'
systemctl stop cbrs-worker.service
systemctl stop cbrs-dashboard.service cbrs-novnc.service cbrs-x11vnc.service cbrs-display.service
'@
}

& wsl.exe --distribution $DistroName --user root --exec bash -lc $command
if ($LASTEXITCODE -ne 0) {
    throw 'The graceful stop command failed. Inspect systemd state before retrying.'
}
Write-Host 'Stop completed. Durable jobs, SQLite state, profiles, and PDFs were preserved.'
