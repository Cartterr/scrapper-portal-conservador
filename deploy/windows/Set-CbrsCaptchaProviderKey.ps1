[CmdletBinding()]
param(
    [ValidateSet('capsolver')]
    [string]$Provider = 'capsolver',
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env'
)

$ErrorActionPreference = 'Stop'

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
    if ([Regex]::IsMatch($Content, $pattern)) {
        return [Regex]::Replace(
            $Content,
            $pattern,
            [Text.RegularExpressions.MatchEvaluator]{ param($match) "$Name=$Value" },
            1
        )
    }
    $separator = if ($Content.EndsWith("`n")) { '' } else { "`r`n" }
    return "$Content$separator$Name=$Value`r`n"
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Protected CBRS environment file not found: $EnvFile"
}

$apiKey = [string](Get-Clipboard -Raw)
$apiKey = $apiKey.Trim()
if ($Provider -eq 'capsolver' -and $apiKey -notmatch '^CAP-[A-Za-z0-9_-]{16,250}$') {
    throw 'Clipboard does not contain a valid CapSolver API key.'
}

try {
    $content = Get-Content -LiteralPath $EnvFile -Raw
    $content = Set-CbrsEnvValue $content 'CBRS_CAPSOLVER_API_KEY' $apiKey
    $content = Set-CbrsEnvValue $content 'CBRS_CAPSOLVER_TIMEOUT_SECONDS' '120'
    $content = Set-CbrsEnvValue $content 'CBRS_CAPSOLVER_POLL_SECONDS' '3'
    $content = Set-CbrsEnvValue $content 'CBRS_CAPTCHA_SOLVER_MODE' 'capsolver_manual'
    Set-Content -LiteralPath $EnvFile -Value $content -Encoding utf8 -NoNewline

    $saved = Get-Content -LiteralPath $EnvFile -Raw
    if ($saved -notmatch '(?m)^CBRS_CAPSOLVER_API_KEY=CAP-[A-Za-z0-9_-]{16,250}\r?$') {
        throw 'CapSolver API key read-back validation failed.'
    }
    Write-Host 'Saved the CapSolver key and manual solver mode in the protected CBRS environment.'
}
finally {
    $apiKey = $null
    Set-Clipboard -Value ''
}
