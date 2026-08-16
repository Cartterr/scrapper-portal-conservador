[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$DistroName = 'Ubuntu-24.04'
)

$ErrorActionPreference = 'Stop'

$command = @'
set -euo pipefail
systemctl --no-pager --plain status cbrs-worker.service cbrs-dashboard.service cbrs-backup.timer || true
cd /opt/cbrs
runuser -u cbrs -- /opt/cbrs/.venv/bin/python deploy/run_with_env.py /etc/cbrs/cbrs.env -- \
  /opt/cbrs/.venv/bin/python -m cbrs jobs status \
  --config /var/lib/cbrs/account-pool.json
'@
$command = $command -replace "`r", ''

& wsl.exe --distribution $DistroName --user root --exec bash -lc $command
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to read the CBRS service status.'
}
