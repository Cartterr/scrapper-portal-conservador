[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [string]$DistroName = '',
    [switch]$PlanOnly,
    [switch]$Resume
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# Compatibility entrypoint retained for older operator shortcuts. The supported
# deployment is native Windows; this wrapper never enables WSL, Docker, a VM,
# or an Ubuntu service.
$nativeInstaller = Join-Path $RepoRoot 'deploy\windows\Install-CbrsNative.ps1'
if (-not (Test-Path -LiteralPath $nativeInstaller)) {
    throw "Native installer not found: $nativeInstaller"
}

if ($DistroName) {
    Write-Warning '-DistroName is deprecated and ignored; CBRS now installs natively on Windows.'
}
if ($Resume) {
    Write-Warning '-Resume is no longer required; the native installer is idempotent.'
}

$arguments = @('-RepoRoot', $RepoRoot)
if ($PlanOnly) { $arguments += '-PlanOnly' }

& $nativeInstaller @arguments
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    throw "Native installer failed with exit code $LASTEXITCODE."
}
