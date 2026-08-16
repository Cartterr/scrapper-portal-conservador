[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$DistroName = 'Ubuntu-24.04'
)

$ErrorActionPreference = 'Stop'

$command = @'
set -euo pipefail
systemctl --no-pager --plain status cbrs-worker.service cbrs-dashboard.service cbrs-backup.timer || true
set -a
source /etc/cbrs/cbrs.env
set +a
cd /opt/cbrs
runuser -u cbrs --preserve-environment -- /opt/cbrs/.venv/bin/python -m cbrs jobs status \
  --config /var/lib/cbrs/account-pool.json
'@

& wsl.exe --distribution $DistroName --user root --exec bash -lc $command
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to read the CBRS service status.'
}
