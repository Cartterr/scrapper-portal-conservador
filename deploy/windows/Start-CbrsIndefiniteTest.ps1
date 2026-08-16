[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$DistroName = 'Ubuntu-24.04',
    [switch]$AcknowledgeAuthorizedLiveTraffic
)

$ErrorActionPreference = 'Stop'

if (-not $AcknowledgeAuthorizedLiveTraffic) {
    throw @'
Refusing to start. This command can launch Chrome and send authorized traffic to CBRS.
Rerun only at the approved test window with -AcknowledgeAuthorizedLiveTraffic.
'@
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'wsl.exe is unavailable.'
}

$installed = @(
    & wsl.exe --list --quiet 2>$null |
        ForEach-Object { ([string]$_).Replace([char]0, '').Trim() } |
        Where-Object { $_ }
)
if ($installed -notcontains $DistroName) {
    throw "$DistroName is not installed. Run the deferred setup stage first."
}

$readinessCommand = @'
set -euo pipefail
set -a
source /etc/cbrs/cbrs.env
set +a
cd /opt/cbrs
install -d -o cbrs -g cbrs -m 0750 /var/lib/cbrs/readiness
runuser -u cbrs --preserve-environment -- /opt/cbrs/.venv/bin/python -m cbrs readiness \
  --target ubuntu \
  --env-file /etc/cbrs/cbrs.env \
  --config /var/lib/cbrs/account-pool.json \
  --json-report /var/lib/cbrs/readiness/pre-live.json
'@
& wsl.exe --distribution $DistroName --user root --exec bash -lc $readinessCommand
if ($LASTEXITCODE -ne 0) {
    throw 'Live readiness failed. No CBRS services were started.'
}

$startCommand = @'
set -euo pipefail
systemctl start cbrs-display.service
systemctl start cbrs-x11vnc.service cbrs-novnc.service
systemctl start cbrs-dashboard.service cbrs-backup.timer
systemctl start cbrs-worker.service
systemctl --no-pager --plain status cbrs-worker.service cbrs-dashboard.service
'@
& wsl.exe --distribution $DistroName --user root --exec bash -lc $startCommand
if ($LASTEXITCODE -ne 0) {
    throw 'One or more CBRS services failed to start. Inspect the systemd status output.'
}

Write-Host 'The durable worker is running indefinitely and will process only explicitly enqueued jobs.'
Write-Host 'Dashboard: http://127.0.0.1:8765'
Write-Host 'CAPTCHA recovery (loopback only): http://127.0.0.1:6080/vnc.html'
