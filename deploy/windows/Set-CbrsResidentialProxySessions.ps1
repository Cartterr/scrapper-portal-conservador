[CmdletBinding()]
param(
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env',
    [string]$PoolConfig = 'G:\CBRS\account-pool.json',
    [string]$StateRoot = 'G:\CBRS',
    [string]$SessionFile,
    [string]$AccountId,
    [switch]$RotateCurrentSessions
)

$ErrorActionPreference = 'Stop'

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $resolvedParent = Split-Path -Parent $LiteralPath
    $temporary = Join-Path $resolvedParent ('.' + [IO.Path]::GetFileName($LiteralPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Content, [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $LiteralPath) {
            Set-Acl -LiteralPath $temporary -AclObject (Get-Acl -LiteralPath $LiteralPath)
        }
        Move-Item -LiteralPath $temporary -Destination $LiteralPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "CBRS environment file is missing."
}
if (-not (Test-Path -LiteralPath $PoolConfig -PathType Leaf)) {
    throw "CBRS account pool configuration is missing."
}

$worker = Get-ScheduledTask -TaskName 'CBRS Worker' -ErrorAction SilentlyContinue
if ($worker -and [string]$worker.State -eq 'Running') {
    throw "CBRS Worker must be stopped before installing proxy sessions."
}
try {
    $runtime = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/status' -TimeoutSec 5
    if (-not [bool]$runtime.endurance.paused) {
        throw "Endurance must be paused before installing proxy sessions."
    }
}
catch {
    throw "The loopback dashboard must be healthy and endurance paused."
}

$sessionText = if ($RotateCurrentSessions) {
    if ($SessionFile) {
        throw "SessionFile cannot be combined with RotateCurrentSessions."
    }
    if ($AccountId) {
        throw "RotateCurrentSessions currently rotates all three isolated accounts together."
    }
    $currentEnvironment = [IO.File]::ReadAllText($EnvFile)
    $rotated = foreach ($index in 1..3) {
        $name = "CBRS_EJECUTIVO_$($index)_PROXY_URL"
        $match = [regex]::Match(
            $currentEnvironment,
            "(?m)^$([regex]::Escape($name))=(?<url>[^\r\n]+)$"
        )
        if (-not $match.Success) {
            throw "The protected environment is missing a configured proxy session."
        }
        $currentUrl = [string]$match.Groups['url'].Value
        $sessionPattern = '(?i)(?<prefix>(?:session|sessid|sess)-)(?<value>[^-:@/]+)'
        $sessionMatches = [regex]::Matches($currentUrl, $sessionPattern)
        if ($sessionMatches.Count -ne 1) {
            throw "A configured proxy URL does not contain exactly one renewable session identifier."
        }
        $newSessionId = [guid]::NewGuid().ToString('N').Substring(0, 16)
        $rotatedUrl = [regex]::Replace(
            $currentUrl,
            $sessionPattern,
            [System.Text.RegularExpressions.MatchEvaluator]{
                param($sessionMatch)
                return $sessionMatch.Groups['prefix'].Value + $newSessionId
            },
            1
        )
        if ($rotatedUrl -eq $currentUrl) {
            throw "A configured proxy session identifier could not be renewed."
        }
        $rotatedUrl
    }
    $rotated -join "`n"
}
elseif ($SessionFile) {
    if (-not (Test-Path -LiteralPath $SessionFile -PathType Leaf)) {
        throw "Protected proxy session file is missing."
    }
    $sessionPayload = Get-Content -LiteralPath $SessionFile -Raw | ConvertFrom-Json
    (@($sessionPayload.sessions) -join "`n")
}
else {
    Get-Clipboard -Raw
}
$rawSessions = @(
    $sessionText -split "`r?`n" |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)
$expectedSessionCount = if ($AccountId) { 1 } else { 3 }
if ($rawSessions.Count -ne $expectedSessionCount) {
    throw "The protected input does not contain the required number of proxy sessions."
}

$proxyUrls = @(foreach ($rawSession in $rawSessions) {
    $candidate = if (
        $rawSession -match '^(?<scheme>https?)://(?<host>[^:/\s]+):(?<port>\d+):(?<user>[^:\s]+):(?<password>[^\s]+)$'
    ) {
        $escapedUser = [Uri]::EscapeDataString([string]$matches.user)
        $escapedPassword = [Uri]::EscapeDataString([string]$matches.password)
        "$([string]$matches.scheme)://$escapedUser`:$escapedPassword@$([string]$matches.host):$([string]$matches.port)"
    }
    elseif ($rawSession -match '^https?://') {
        $rawSession
    }
    else {
        "http://$rawSession"
    }
    $uri = $null
    if (-not [Uri]::TryCreate($candidate, [UriKind]::Absolute, [ref]$uri)) {
        throw "A generated proxy session is malformed."
    }
    if ($uri.Scheme -notin @('http', 'https') -or -not $uri.Host -or $uri.Port -le 0 -or -not $uri.UserInfo) {
        throw "Generated sessions must be authenticated HTTP(S) proxy URLs."
    }
    if ($uri.UserInfo -notmatch '(?i)sesstime-120') {
        throw "Every generated session must declare sessTime-120."
    }
    $uri.AbsoluteUri.TrimEnd('/')
})
if (@($proxyUrls | Select-Object -Unique).Count -ne $expectedSessionCount) {
    throw "Generated proxy sessions must be distinct."
}

$pool = Get-Content -LiteralPath $PoolConfig -Raw | ConvertFrom-Json
$accounts = @($pool.accounts | Where-Object { $_.enabled -ne $false })
if ($accounts.Count -ne 3) {
    throw "Exactly three enabled CBRS accounts are required."
}
$targetAccounts = @(if ($AccountId) {
    $accounts | Where-Object { [string]$_.id -eq $AccountId }
}
else {
    $accounts
})
if ($targetAccounts.Count -ne $expectedSessionCount) {
    throw "The requested CBRS account selection is invalid."
}
$proxyEnvNames = @($targetAccounts | ForEach-Object { [string]$_.proxy_url_env })
if ($proxyEnvNames.Count -ne $expectedSessionCount -or @($proxyEnvNames | Where-Object { -not $_ }).Count -gt 0) {
    throw "Every enabled account must have a proxy_url_env reference."
}

$timestamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$rollbackRoot = Join-Path 'C:\ProgramData\CBRS\rollback' "proxy-migration-$timestamp"
$resolvedRollbackRoot = [IO.Path]::GetFullPath($rollbackRoot)
$allowedRollbackParent = [IO.Path]::GetFullPath('C:\ProgramData\CBRS\rollback')
if (-not $resolvedRollbackRoot.StartsWith($allowedRollbackParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Rollback path escaped the protected CBRS directory."
}
New-Item -ItemType Directory -Path $resolvedRollbackRoot -Force | Out-Null

$envBackup = Join-Path $resolvedRollbackRoot 'cbrs.env'
$poolBackup = Join-Path $resolvedRollbackRoot 'account-pool.json'
Copy-Item -LiteralPath $EnvFile -Destination $envBackup
Copy-Item -LiteralPath $PoolConfig -Destination $poolBackup
$secretAcl = Get-Acl -LiteralPath $EnvFile
Set-Acl -LiteralPath $envBackup -AclObject $secretAcl
Set-Acl -LiteralPath $poolBackup -AclObject $secretAcl

foreach ($account in $accounts) {
    $profilePath = if ([string]$account.profile_dir) {
        [string]$account.profile_dir
    }
    else {
        Join-Path $StateRoot "accounts\$([string]$account.id)\chrome-profile"
    }
    $baseline = Join-Path (Split-Path -Parent $profilePath) 'fixed-egress-baseline.json'
    if (Test-Path -LiteralPath $baseline -PathType Leaf) {
        $baselineName = "fixed-egress-baseline-$([string]$account.id).json"
        $baselineBackup = Join-Path $resolvedRollbackRoot $baselineName
        Copy-Item -LiteralPath $baseline -Destination $baselineBackup
        Set-Acl -LiteralPath $baselineBackup -AclObject $secretAcl
    }
}

$environmentText = [IO.File]::ReadAllText($EnvFile)
for ($index = 0; $index -lt $expectedSessionCount; $index++) {
    $name = $proxyEnvNames[$index]
    $escapedName = [regex]::Escape($name)
    $replacement = "$name=$($proxyUrls[$index])"
    if ($environmentText -match "(?m)^$escapedName=") {
        $environmentText = [regex]::Replace(
            $environmentText,
            "(?m)^$escapedName=.*$",
            [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $replacement },
            1
        )
    }
    else {
        $environmentText = $environmentText.TrimEnd("`r", "`n") + "`r`n$replacement`r`n"
    }
    if ($targetAccounts[$index].PSObject.Properties.Name -contains 'proxy_provider') {
        $targetAccounts[$index].proxy_provider = '2captcha_residential_sticky'
    }
    else {
        $targetAccounts[$index] | Add-Member -NotePropertyName proxy_provider -NotePropertyValue '2captcha_residential_sticky'
    }
}

try {
    Write-AtomicText -LiteralPath $EnvFile -Content $environmentText
    Write-AtomicText -LiteralPath $PoolConfig -Content (($pool | ConvertTo-Json -Depth 20) + "`r`n")
}
catch {
    Copy-Item -LiteralPath $envBackup -Destination $EnvFile -Force
    Copy-Item -LiteralPath $poolBackup -Destination $PoolConfig -Force
    Set-Acl -LiteralPath $EnvFile -AclObject $secretAcl
    Set-Acl -LiteralPath $PoolConfig -AclObject $secretAcl
    throw
}
finally {
    Set-Clipboard -Value ''
    if ($SessionFile -and (Test-Path -LiteralPath $SessionFile)) {
        Remove-Item -LiteralPath $SessionFile -Force
    }
}

[pscustomobject]@{
    ok = $true
    accounts_configured = $expectedSessionCount
    provider = '2captcha_residential_sticky'
    session_minutes = 120
    rollback_path = $resolvedRollbackRoot
    secrets_printed = $false
} | ConvertTo-Json -Compress
