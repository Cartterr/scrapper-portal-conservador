[CmdletBinding()]
param(
    [ValidateSet('residential', 'mobile')]
    [string]$Network = 'mobile',
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env',
    [string]$PoolConfig = 'G:\CBRS\account-pool.json',
    [string]$LocalEnvFile = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path '.env')
)

$ErrorActionPreference = 'Stop'

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

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Content,
        [System.Security.AccessControl.FileSecurity]$Acl
    )
    $parent = [IO.Path]::GetFullPath((Split-Path -Parent $LiteralPath))
    $temporary = Join-Path $parent ('.' + [IO.Path]::GetFileName($LiteralPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Content, [Text.UTF8Encoding]::new($false))
        if ($Acl) { Set-Acl -LiteralPath $temporary -AclObject $Acl }
        Move-Item -LiteralPath $temporary -Destination $LiteralPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

foreach ($path in @($EnvFile, $LocalEnvFile, $PoolConfig)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'A required CBRS configuration file is missing.'
    }
}
foreach ($taskName in @('CBRS User Worker', 'CBRS Worker')) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and [string]$task.State -eq 'Running') {
        throw "$taskName must be stopped before changing the DataImpulse plan type."
    }
}

$protectedText = [IO.File]::ReadAllText($EnvFile)
$localText = [IO.File]::ReadAllText($LocalEnvFile)
foreach ($requiredSecret in @('DATAIMPULSE_PROXY_LOGIN', 'DATAIMPULSE_PROXY_PASSWORD')) {
    if ($protectedText -notmatch "(?m)^$requiredSecret=.+$") {
        throw 'The protected environment does not contain both proxy credentials.'
    }
}

$pool = Get-Content -LiteralPath $PoolConfig -Raw | ConvertFrom-Json
$accounts = @($pool.accounts | Where-Object { $_.enabled -ne $false })
if ($accounts.Count -ne 3) {
    throw 'Exactly three enabled CBRS accounts are required.'
}
$ports = @($accounts | ForEach-Object { [int]$_.dataimpulse_port })
if (@($ports | Select-Object -Unique).Count -ne 3 -or
    @($ports | Where-Object { $_ -lt 10000 -or $_ -gt 20000 }).Count) {
    throw 'The three accounts require distinct DataImpulse sticky ports from 10000 through 20000.'
}

$timestamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$rollbackParent = [IO.Path]::GetFullPath('C:\ProgramData\CBRS\rollback')
$rollbackRoot = [IO.Path]::GetFullPath((Join-Path $rollbackParent "dataimpulse-$Network-$timestamp"))
if (-not $rollbackRoot.StartsWith($rollbackParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Rollback path escaped the protected CBRS directory.'
}
New-Item -ItemType Directory -Path $rollbackRoot -Force | Out-Null
$protectedAcl = Get-Acl -LiteralPath $EnvFile
Copy-Item -LiteralPath $EnvFile -Destination (Join-Path $rollbackRoot 'cbrs.env')
Copy-Item -LiteralPath $LocalEnvFile -Destination (Join-Path $rollbackRoot 'local.env')
Copy-Item -LiteralPath $PoolConfig -Destination (Join-Path $rollbackRoot 'account-pool.json')
Set-Acl -LiteralPath (Join-Path $rollbackRoot 'cbrs.env') -AclObject $protectedAcl

$provider = "dataimpulse_${Network}_sticky"
$brand = if ($Network -eq 'mobile') { 'DataImpulse Mobile' } else { 'DataImpulse' }
$settings = [ordered]@{
    CBRS_EGRESS_MODE = "${Network}_sticky"
    CBRS_HEADLESS = '1'
    CBRS_WINDOW_MODE = 'normal'
    DATAIMPULSE_PROXY_SCHEME = 'http'
    DATAIMPULSE_PROXY_HOST = 'gw.dataimpulse.com'
    DATAIMPULSE_COUNTRY = 'cl'
    DATAIMPULSE_STICKY_TTL_MINUTES = '120'
    DATAIMPULSE_STICKY_PORT_MIN = '10000'
    DATAIMPULSE_STICKY_PORT_MAX = '20000'
}
foreach ($entry in $settings.GetEnumerator()) {
    $protectedText = Set-EnvValue -Text $protectedText -Name $entry.Key -Value ([string]$entry.Value)
    $localText = Set-EnvValue -Text $localText -Name $entry.Key -Value ([string]$entry.Value)
}
foreach ($account in $accounts) {
    $account.proxy_provider = $provider
    $account.proxy_brand = $brand
    $account.PSObject.Properties.Remove('proxy_url_env')
}

try {
    Write-AtomicText -LiteralPath $EnvFile -Content $protectedText -Acl $protectedAcl
    Write-AtomicText -LiteralPath $LocalEnvFile -Content $localText
    Write-AtomicText -LiteralPath $PoolConfig -Content (($pool | ConvertTo-Json -Depth 20) + "`r`n") -Acl (Get-Acl -LiteralPath $PoolConfig)
}
catch {
    Copy-Item -LiteralPath (Join-Path $rollbackRoot 'cbrs.env') -Destination $EnvFile -Force
    Copy-Item -LiteralPath (Join-Path $rollbackRoot 'local.env') -Destination $LocalEnvFile -Force
    Copy-Item -LiteralPath (Join-Path $rollbackRoot 'account-pool.json') -Destination $PoolConfig -Force
    Set-Acl -LiteralPath $EnvFile -AclObject $protectedAcl
    throw
}

[pscustomobject]@{
    ok = $true
    network = $Network
    provider = $provider
    accounts = 3
    distinct_sticky_ports = 3
    headless_default = $true
    credentials_printed = $false
    rollback_path = $rollbackRoot
} | ConvertTo-Json -Compress
