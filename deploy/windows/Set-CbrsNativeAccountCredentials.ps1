[CmdletBinding()]
param(
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env'
)

$ErrorActionPreference = 'Stop'

function ConvertFrom-CbrsSecureString {
    param([Security.SecureString]$Value)

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Set-CbrsEnvValue {
    param(
        [string]$Content,
        [string]$Name,
        [string]$Value
    )

    if ($Value -match "[`r`n]") {
        throw "$Name cannot contain line breaks."
    }
    $pattern = '(?m)^' + [Regex]::Escape($Name) + '=.*$'
    if (-not [Regex]::IsMatch($Content, $pattern)) {
        throw "Expected key $Name is missing from $EnvFile."
    }
    return [Regex]::Replace(
        $Content,
        $pattern,
        [Text.RegularExpressions.MatchEvaluator]{ param($match) "$Name=$Value" },
        1
    )
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Protected CBRS environment file not found: $EnvFile"
}

$entries = @()
try {
    foreach ($index in 1..3) {
        $username = (Read-Host "CBRS account $index email").Trim()
        if (-not $username -or $username -match '^REPLACE_') {
            throw "Account $index requires a non-placeholder email."
        }
        $securePassword = Read-Host "CBRS account $index password" -AsSecureString
        $password = ConvertFrom-CbrsSecureString $securePassword
        if (-not $password -or $password -match '^REPLACE_') {
            throw "Account $index requires a non-placeholder password."
        }
        $entries += [pscustomobject]@{
            Index = $index
            Username = $username
            Password = $password
        }
    }

    $content = Get-Content -LiteralPath $EnvFile -Raw
    foreach ($entry in $entries) {
        $content = Set-CbrsEnvValue $content "CBRS_EJECUTIVO_$($entry.Index)_USERNAME" $entry.Username
        $content = Set-CbrsEnvValue $content "CBRS_EJECUTIVO_$($entry.Index)_PASSWORD" $entry.Password
    }
    Set-Content -LiteralPath $EnvFile -Value $content -Encoding utf8 -NoNewline
    Write-Host 'Saved credentials for 3 CBRS accounts in the protected native environment.'
}
finally {
    foreach ($entry in $entries) {
        $entry.Password = $null
    }
    $entries = @()
    $password = $null
    $securePassword = $null
}
