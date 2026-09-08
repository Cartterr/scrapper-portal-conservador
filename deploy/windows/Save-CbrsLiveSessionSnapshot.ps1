[CmdletBinding()]
param([string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path)

$ErrorActionPreference = 'Stop'
$statusUrl = 'http://127.0.0.1:8765/api/status'
$before = Invoke-RestMethod $statusUrl
if ($before.pool.browser_live_count -ne 3 -or $before.pool.browser_authenticated_count -ne 3) {
    throw 'Snapshot requires all three live authenticated sessions.'
}
$snapshotPath = Join-Path (Join-Path $RepoRoot '.cbrs\runtime') ('session-snapshot-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $snapshotPath | Out-Null
# Restrict access BEFORE copying credentials. No profile/cookie export or browser commands.
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$identities = @([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
foreach ($sid in $identities) {
    $identity = New-Object System.Security.Principal.SecurityIdentifier($sid)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $snapshotPath -AclObject $acl
$sources = @{
    'cbrs.env' = (Join-Path $RepoRoot '.env')
    'account-pool.json' = (Join-Path $RepoRoot '.cbrs\runtime\account-pool.json')
    'endurance-plan.json' = (Join-Path $RepoRoot '.cbrs\runtime\endurance-plan.json')
}
foreach ($index in 1..3) {
    $sources["ejecutivo_$index-baseline.json"] = Join-Path $RepoRoot ".cbrs\runtime\accounts\ejecutivo_$index\fixed-egress-baseline.json"
}
foreach ($entry in $sources.GetEnumerator()) {
    $destination = Join-Path $snapshotPath $entry.Key
    Copy-Item -LiteralPath $entry.Value -Destination $destination
    if ((Get-FileHash -LiteralPath $entry.Value).Hash -ne (Get-FileHash -LiteralPath $destination).Hash) {
        throw 'Configuration changed during snapshot; do not use this incomplete snapshot.'
    }
}
$fields = @('account_id', 'proxy_provider', 'proxy_endpoint', 'proxy_sticky_port',
    'proxy_generation', 'proxy_sticky_ttl_minutes', 'egress_country', 'egress_route_id',
    'browser_live', 'browser_authenticated', 'browser_auth_state', 'browser_started_at',
    'browser_mode', 'browser_engine')
$accountsBefore = @($before.accounts | Select-Object -Property $fields)
$after = Invoke-RestMethod $statusUrl
$accountsAfter = @($after.accounts | Select-Object -Property $fields)
if (($accountsBefore | ConvertTo-Json -Depth 5 -Compress) -ne ($accountsAfter | ConvertTo-Json -Depth 5 -Compress) -or
    $before.jobs.summary.worker.owner -ne $after.jobs.summary.worker.owner) {
    throw 'Session evidence changed during snapshot; keep sessions untouched and review.'
}
$manifest = @{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    worker_owner = $after.jobs.summary.worker.owner
    accounts = $accountsAfter
    complete = $true
    limitation = 'Configuration files plus dashboard route evidence; not a live-session backup or proof of in-memory credential parity.'
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $snapshotPath 'manifest.json') -Encoding UTF8
Write-Output "Protected snapshot verified: $snapshotPath"
Write-Output '3/3 session evidence and worker owner unchanged. No browsers or proxy settings modified.'
