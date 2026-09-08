[CmdletBinding()]
param(
    [ValidateSet('publish','status','rollback')][string]$Action = 'publish',
    [string]$Release = 'builtin',
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [ValidateSet('worker','owner')][string]$Target = 'worker',
    [string]$StateRoot
)
$ErrorActionPreference = 'Stop'
if (-not $StateRoot) {
    $StateRoot = if ($Target -eq 'owner') { (Join-Path $RepoRoot '.cbrs\runtime\browser-owner\runtime-updates') } else { (Join-Path $RepoRoot '.cbrs\runtime\pool\runtime-updates') }
}
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$arguments = @('-m', 'cbrs.runtime_updates', $Action, '--root', $StateRoot,
    '--source', (Join-Path $RepoRoot 'cbrs'))
if ($Action -eq 'rollback') { $arguments += @('--release', $Release) }
Push-Location $RepoRoot
try {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Runtime update command failed; inspect locally without exposing secrets.' }
} finally { Pop-Location }
# Publication is not activation. Read status after the owner reaches a safe
# boundary. Never restart a worker or clear a cooldown from this helper.
