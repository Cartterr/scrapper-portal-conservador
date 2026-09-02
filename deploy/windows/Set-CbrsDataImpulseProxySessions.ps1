[CmdletBinding()]
param(
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env',
    [string]$PoolConfig = 'G:\CBRS\account-pool.json',
    [string]$LocalEnvFile = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path '.env')
)

$ErrorActionPreference = 'Stop'

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Content,
        [System.Security.AccessControl.FileSecurity]$Acl
    )
    $parent = Split-Path -Parent $LiteralPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = Join-Path $parent ('.' + [IO.Path]::GetFileName($LiteralPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Content, [Text.UTF8Encoding]::new($false))
        if ($Acl) { Set-Acl -LiteralPath $temporary -AclObject $Acl }
        Move-Item -LiteralPath $temporary -Destination $LiteralPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Read-EnvMap {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $values = [ordered]@{}
    foreach ($line in [IO.File]::ReadAllLines($LiteralPath)) {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
            $value = $matches[2]
            if ($value.StartsWith('"') -and $value.EndsWith('"')) {
                try { $value = $value | ConvertFrom-Json } catch { }
            }
            $values[$matches[1]] = [string]$value
        }
    }
    return $values
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $line = "$Name=$($Value | ConvertTo-Json -Compress)"
    if ($Text -match "(?m)^$([regex]::Escape($Name))=") {
        return [regex]::Replace(
            $Text,
            "(?m)^$([regex]::Escape($Name))=.*$",
            [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $line },
            1
        )
    }
    return $Text.TrimEnd("`r", "`n") + "`r`n$line`r`n"
}

foreach ($requiredFile in @($EnvFile, $PoolConfig)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw 'A required protected CBRS file is missing.'
    }
}
foreach ($taskName in @('CBRS User Worker', 'CBRS Worker')) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and [string]$task.State -eq 'Running') {
        throw "$taskName must be stopped before migrating DataImpulse routes."
    }
}

$environment = Read-EnvMap -LiteralPath $EnvFile
$pool = Get-Content -LiteralPath $PoolConfig -Raw | ConvertFrom-Json
$accounts = @($pool.accounts | Where-Object { $_.enabled -ne $false })
if ($accounts.Count -ne 3) { throw 'Exactly three enabled CBRS accounts are required.' }

$routes = @()
for ($index = 0; $index -lt 3; $index++) {
    $reference = [string]$accounts[$index].proxy_url_env
    if (-not $reference -or -not $environment.Contains($reference)) {
        throw 'Every legacy account must reference one existing proxy URL.'
    }
    $uri = $null
    if (-not [Uri]::TryCreate([string]$environment[$reference], [UriKind]::Absolute, [ref]$uri)) {
        throw 'A configured DataImpulse route is malformed.'
    }
    if ($uri.Scheme -ne 'http' -or $uri.Host -ne 'gw.dataimpulse.com' -or -not $uri.UserInfo) {
        throw 'Every route must be an authenticated DataImpulse HTTP proxy.'
    }
    if ($uri.Port -lt 10000 -or $uri.Port -gt 20000) {
        throw 'Every DataImpulse route must use a sticky port from 10000 through 20000.'
    }
    $credentialParts = $uri.UserInfo -split ':', 2
    if ($credentialParts.Count -ne 2) { throw 'A DataImpulse route is missing proxy credentials.' }
    $routedLogin = [Uri]::UnescapeDataString($credentialParts[0])
    $routes += [pscustomobject]@{
        Login = ($routedLogin -split '__', 2)[0]
        Password = [Uri]::UnescapeDataString($credentialParts[1])
        Port = $uri.Port
        EnvReference = $reference
    }
}
if (@($routes.Port | Select-Object -Unique).Count -ne 3) {
    throw 'The three DataImpulse sticky ports must be distinct.'
}
if (@($routes.Login | Select-Object -Unique).Count -ne 1 -or @($routes.Password | Select-Object -Unique).Count -ne 1) {
    throw 'The three routes must belong to the same DataImpulse proxy plan.'
}

$timestamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$rollbackParent = [IO.Path]::GetFullPath('C:\ProgramData\CBRS\rollback')
$rollbackRoot = [IO.Path]::GetFullPath((Join-Path $rollbackParent "dataimpulse-runtime-$timestamp"))
if (-not $rollbackRoot.StartsWith($rollbackParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Rollback path escaped the protected CBRS directory.'
}
New-Item -ItemType Directory -Path $rollbackRoot -Force | Out-Null
$secretAcl = Get-Acl -LiteralPath $EnvFile
foreach ($source in @($EnvFile, $PoolConfig)) {
    $destination = Join-Path $rollbackRoot ([IO.Path]::GetFileName($source))
    Copy-Item -LiteralPath $source -Destination $destination
    Set-Acl -LiteralPath $destination -AclObject $secretAcl
}
if (Test-Path -LiteralPath $LocalEnvFile) {
    Copy-Item -LiteralPath $LocalEnvFile -Destination (Join-Path $rollbackRoot 'local.env')
}

$environmentText = [IO.File]::ReadAllText($EnvFile)
foreach ($route in $routes) {
    $environmentText = [regex]::Replace(
        $environmentText,
        "(?m)^$([regex]::Escape($route.EnvReference))=.*(?:\r?\n|$)",
        ''
    )
}
$settings = [ordered]@{
    'DATAIMPULSE_PROXY_LOGIN' = [string]$routes[0].Login
    'DATAIMPULSE_PROXY_PASSWORD' = [string]$routes[0].Password
    'DATAIMPULSE_PROXY_HOST' = 'gw.dataimpulse.com'
    'DATAIMPULSE_COUNTRY' = 'cl'
    'DATAIMPULSE_STICKY_TTL_MINUTES' = '120'
    'DATAIMPULSE_STICKY_PORT_MIN' = '10000'
    'DATAIMPULSE_STICKY_PORT_MAX' = '20000'
    'CBRS_DATAIMPULSE_ROTATION_COOLDOWN_SECONDS' = '300'
    'CBRS_DATAIMPULSE_MAX_ROTATIONS_PER_HOUR' = '3'
    'CBRS_DATAIMPULSE_TEMP_UNAVAILABLE_THRESHOLD' = '2'
    'CBRS_BROWSER_HEALTHCHECK_SECONDS' = '30'
    'CBRS_BROWSER_REAUTH_BACKOFF_SECONDS' = '60'
    'CBRS_HEADLESS' = '1'
    'CBRS_WINDOW_MODE' = 'normal'
    'CBRS_EGRESS_MODE' = 'residential_sticky'
    'CBRS_EXPECTED_EGRESS_COUNTRY' = 'CL'
}
foreach ($entry in $settings.GetEnumerator()) {
    $environmentText = Set-EnvValue -Text $environmentText -Name $entry.Key -Value $entry.Value
}

for ($index = 0; $index -lt 3; $index++) {
    $account = $accounts[$index]
    $account.PSObject.Properties.Remove('proxy_url_env')
    foreach ($pair in @{
        proxy_provider = 'dataimpulse_residential_sticky'
        proxy_brand = 'DataImpulse'
        dataimpulse_port = [int]$routes[$index].Port
    }.GetEnumerator()) {
        if ($account.PSObject.Properties.Name -contains $pair.Key) {
            $account.($pair.Key) = $pair.Value
        }
        else {
            $account | Add-Member -NotePropertyName $pair.Key -NotePropertyValue $pair.Value
        }
    }
}

try {
    Write-AtomicText -LiteralPath $EnvFile -Content $environmentText -Acl $secretAcl
    Write-AtomicText -LiteralPath $PoolConfig -Content (($pool | ConvertTo-Json -Depth 20) + "`r`n") -Acl $secretAcl
    Write-AtomicText -LiteralPath $LocalEnvFile -Content $environmentText
}
catch {
    Copy-Item -LiteralPath (Join-Path $rollbackRoot 'cbrs.env') -Destination $EnvFile -Force
    Copy-Item -LiteralPath (Join-Path $rollbackRoot 'account-pool.json') -Destination $PoolConfig -Force
    Set-Acl -LiteralPath $EnvFile -AclObject $secretAcl
    Set-Acl -LiteralPath $PoolConfig -AclObject $secretAcl
    throw
}

[pscustomobject]@{
    ok = $true
    accounts_configured = 3
    provider = 'dataimpulse_residential_sticky'
    country = 'CL'
    session_minutes = 120
    distinct_routes = 3
    rollback_path = $rollbackRoot
    secrets_printed = $false
} | ConvertTo-Json -Compress
