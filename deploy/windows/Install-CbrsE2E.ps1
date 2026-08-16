[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$DistroName = 'Ubuntu-24.04',
    [switch]$PlanOnly,
    [switch]$Resume
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:Step = 0
$script:LogPath = $null
$script:BatPath = Join-Path $RepoRoot 'INSTALL-CBRS.bat'

function Write-InstallerMessage {
    param(
        [string]$Message,
        [ValidateSet('INFO', 'PASS', 'WARN', 'FAIL')]
        [string]$Level = 'INFO'
    )
    $colors = @{ INFO = 'Cyan'; PASS = 'Green'; WARN = 'Yellow'; FAIL = 'Red' }
    Write-Host ("[{0}] {1}" -f $Level, $Message) -ForegroundColor $colors[$Level]
    if ($script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Encoding UTF8 -Value (
            "{0:o} [{1}] {2}" -f (Get-Date), $Level, $Message
        )
    }
}

function Write-InstallerStep {
    param([string]$Message)
    $script:Step += 1
    Write-Host ''
    Write-InstallerMessage ("PASO {0}: {1}" -f $script:Step, $Message)
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Read-YesNo {
    param(
        [string]$Prompt,
        [bool]$Default = $false
    )
    $suffix = if ($Default) { '[S/n]' } else { '[s/N]' }
    while ($true) {
        $answer = (Read-Host "$Prompt $suffix").Trim()
        if (-not $answer) { return $Default }
        if ($answer -match '^(s|si|sí|y|yes)$') { return $true }
        if ($answer -match '^(n|no)$') { return $false }
        Write-InstallerMessage 'Responda S o N.' 'WARN'
    }
}

function ConvertFrom-ProtectedInput {
    param([Security.SecureString]$SecureValue)
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Read-RequiredValue {
    param([string]$Prompt)
    while ($true) {
        $value = (Read-Host $Prompt).Trim()
        if ($value -and $value.IndexOfAny([char[]]"`r`n`0") -lt 0) { return $value }
        Write-InstallerMessage 'El valor es obligatorio y debe ocupar una sola linea.' 'WARN'
    }
}

function Read-ProtectedValue {
    param([string]$Prompt)
    while ($true) {
        $secure = Read-Host $Prompt -AsSecureString
        $plain = ConvertFrom-ProtectedInput $secure
        if ($plain -and $plain.IndexOfAny([char[]]"`r`n`0") -lt 0) { return $plain }
        Write-InstallerMessage 'El valor protegido no puede estar vacio.' 'WARN'
    }
}

function Read-ConfirmedProtectedValue {
    param([string]$Prompt)
    while ($true) {
        $first = Read-ProtectedValue $Prompt
        $second = Read-ProtectedValue 'Repita el valor para confirmar'
        if ($first -ceq $second) { return $first }
        Write-InstallerMessage 'Los valores no coinciden. Intente nuevamente.' 'WARN'
    }
}

function Read-ProxyValue {
    param([string]$Prompt)
    while ($true) {
        $value = Read-ProtectedValue $Prompt
        [Uri]$uri = $null
        $valid = [Uri]::TryCreate($value, [UriKind]::Absolute, [ref]$uri)
        $authorityHost = if ($valid) { ($uri.Authority -split '@')[-1] } else { '' }
        if (
            $valid -and
            $uri.Scheme -in @('http', 'https') -and
            $uri.Host -and
            $authorityHost -match ':\d+$'
        ) {
            return $value
        }
        Write-InstallerMessage 'El proxy debe ser una URL HTTP(S) con host y puerto explicito.' 'WARN'
    }
}

function Invoke-NativeChecked {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$AllowFailure
    )
    $nativeOutput = @(& $FilePath @Arguments)
    $code = $LASTEXITCODE
    foreach ($line in $nativeOutput) { Write-Host ([string]$line) }
    if (-not $AllowFailure -and $code -ne 0) {
        throw "$FilePath termino con codigo $code."
    }
    return $code
}

function Get-WslDistributions {
    $raw = & wsl.exe --list --quiet 2>$null
    return @(
        $raw |
            ForEach-Object { ([string]$_).Replace([char]0, '').Trim() } |
            Where-Object { $_ }
    )
}

function Invoke-WslCapture {
    param([string[]]$Arguments)
    $output = & wsl.exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "wsl.exe termino con codigo $LASTEXITCODE."
    }
    return (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
}

function Invoke-WslRootScript {
    param(
        [string]$Script,
        [string[]]$Arguments = @(),
        [switch]$AllowFailure
)
    # Here-strings read from a Windows checkout use CRLF. Bash accepts LF only
    # for control keywords such as `set -o pipefail`.
    $Script = $Script -replace "`r", ''
    $oldEncoding = $OutputEncoding
    try {
        $OutputEncoding = New-Object Text.UTF8Encoding($false)
        $nativeOutput = @(
            $Script | & wsl.exe --distribution $DistroName --user root --exec bash -s -- @Arguments
        )
        $code = $LASTEXITCODE
    } finally {
        $OutputEncoding = $oldEncoding
    }
    foreach ($line in $nativeOutput) { Write-Host ([string]$line) }
    if (-not $AllowFailure -and $code -ne 0) {
        throw "El script Ubuntu termino con codigo $code."
    }
    return $code
}

function Test-WslRootScript {
    param([string]$Script)
    return (Invoke-WslRootScript -Script $Script -AllowFailure) -eq 0
}

function Set-ResumeAfterRestart {
    $command = 'cmd.exe /c ""{0}" --resume"' -f $script:BatPath
    $runOnce = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce'
    New-Item -Path $runOnce -Force | Out-Null
    New-ItemProperty -Path $runOnce -Name 'CBRSInstallResume' -Value $command -PropertyType String -Force | Out-Null
    Write-InstallerMessage 'La instalacion quedo registrada para continuar despues del reinicio.' 'PASS'
}

function Request-RestartAndExit {
    Set-ResumeAfterRestart
    Write-InstallerMessage 'Windows debe reiniciarse para continuar con WSL2.' 'WARN'
    if (Read-YesNo '¿Reiniciar Windows ahora?' $false) {
        shutdown.exe /r /t 15 /c "CBRS continuara la instalacion al iniciar sesion."
        exit 3010
    }
    Write-InstallerMessage 'Reinicie cuando sea conveniente; el instalador continuara al iniciar sesion.' 'WARN'
    exit 3010
}

function Assert-RepositoryAssets {
    $required = @(
        'PREREQUISITES.txt',
        'requirements.txt',
        'cbrs\__main__.py',
        'deploy\install-ubuntu.sh',
        'deploy\configure_runtime.py',
        'deploy\run_with_env.py',
        'deploy\cbrs.env.example',
        'deploy\account-pool.json.example',
        'deploy\cbrs-worker.service',
        'deploy\cbrs-dashboard.service',
        'deploy\cbrs-worker-resume.path',
        'deploy\cbrs-worker-resume.service',
        'deploy\cbrs-configuration-apply.path',
        'deploy\cbrs-configuration-apply.service',
        'deploy\cbrs-dashboard-wsl.conf',
        'deploy\cbrs-worker-wsl.conf',
        'deploy\windows\Start-CbrsWslHidden.vbs'
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $RepoRoot $_) -PathType Leaf) })
    if ($missing.Count -gt 0) {
        throw 'El checkout esta incompleto. Faltan: ' + ($missing -join ', ')
    }
}

function Test-TcpEndpoint {
    param(
        [string]$HostName,
        [int]$Port = 443,
        [int]$TimeoutMilliseconds = 5000
    )
    $client = New-Object Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if (-not $task.Wait($TimeoutMilliseconds)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Assert-HostPrerequisites {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'CBRS requiere Windows x64.'
    }
    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    if ($build -lt 22000) {
        throw "Se requiere Windows 11 (build 22000 o superior); build detectada: $build."
    }

    $systemDriveName = $env:SystemDrive.TrimEnd(':')
    $systemDrive = Get-PSDrive -Name $systemDriveName -PSProvider FileSystem
    $freeGiB = [math]::Round($systemDrive.Free / 1GB, 1)
    if ($systemDrive.Free -lt 20GB) {
        throw "Se requieren al menos 20 GiB libres en $($env:SystemDrive); disponibles: $freeGiB GiB."
    }
    Write-InstallerMessage "Windows 11 x64 y $freeGiB GiB libres en $($env:SystemDrive)." 'PASS'

    $computer = Get-CimInstance Win32_ComputerSystem
    $processors = @(Get-CimInstance Win32_Processor)
    $virtualizationReady = [bool]$computer.HypervisorPresent -or [bool](
        $processors | Where-Object { $_.VirtualizationFirmwareEnabled }
    )
    if (-not $virtualizationReady) {
        throw 'La virtualizacion de CPU no aparece habilitada en BIOS/UEFI.'
    }
    Write-InstallerMessage 'Virtualizacion de CPU disponible.' 'PASS'

    $endpoints = @('archive.ubuntu.com', 'dl.google.com', 'pypi.org')
    $unreachable = @($endpoints | Where-Object { -not (Test-TcpEndpoint -HostName $_) })
    if ($unreachable.Count -gt 0) {
        throw 'No hay salida TCP 443 hacia: ' + ($unreachable -join ', ')
    }
    Write-InstallerMessage 'Conectividad de instalacion HTTPS verificada.' 'PASS'
}

function Ensure-WindowsFeatures {
    $features = @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')
    $changed = $false
    foreach ($name in $features) {
        $feature = Get-WindowsOptionalFeature -Online -FeatureName $name
        if ($feature.State -ne 'Enabled') {
            Write-InstallerMessage "Habilitando caracteristica Windows: $name"
            Enable-WindowsOptionalFeature -Online -FeatureName $name -All -NoRestart | Out-Null
            $changed = $true
        } else {
            Write-InstallerMessage "$name ya esta habilitado." 'PASS'
        }
    }
    if ($changed) { Request-RestartAndExit }
}

function Ensure-WslDistribution {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw 'wsl.exe no esta disponible aun. Reinicie Windows y vuelva a ejecutar el instalador.'
    }
    Invoke-NativeChecked -FilePath 'wsl.exe' -Arguments @('--update') | Out-Null
    $installed = Get-WslDistributions
    if ($installed -notcontains $DistroName) {
        Write-InstallerMessage "Instalando $DistroName. La descarga puede tardar varios minutos."
        $code = Invoke-NativeChecked -FilePath 'wsl.exe' -Arguments @(
            '--install', '--distribution', $DistroName, '--no-launch'
        ) -AllowFailure
        if ($code -eq 3010) { Request-RestartAndExit }
        if ($code -ne 0) {
            throw "No se pudo instalar $DistroName (codigo $code)."
        }
        Start-Sleep -Seconds 3
    } else {
        Write-InstallerMessage "$DistroName ya esta instalada." 'PASS'
    }

    Invoke-NativeChecked -FilePath 'wsl.exe' -Arguments @(
        '--set-version', $DistroName, '2'
    ) | Out-Null
    Invoke-NativeChecked -FilePath 'wsl.exe' -Arguments @(
        '--distribution', $DistroName, '--user', 'root', '--exec', '/bin/true'
    ) | Out-Null

    $systemdReady = Test-WslRootScript 'test "$(cat /proc/1/comm)" = systemd'
    if (-not $systemdReady) {
        Write-InstallerMessage 'Habilitando systemd dentro de Ubuntu.'
        $systemdScript = @'
set -euo pipefail
if [ -f /etc/wsl.conf ]; then cp -a /etc/wsl.conf /etc/wsl.conf.cbrs-backup; fi
if [ -f /etc/wsl.conf ] && grep -qE '^[[:space:]]*systemd[[:space:]]*=' /etc/wsl.conf; then
  sed -i -E 's/^[[:space:]]*systemd[[:space:]]*=.*/systemd=true/' /etc/wsl.conf
else
  printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf
fi
'@
        Invoke-WslRootScript -Script $systemdScript | Out-Null
        Invoke-NativeChecked -FilePath 'wsl.exe' -Arguments @('--terminate', $DistroName) | Out-Null
        Start-Sleep -Seconds 3
        Invoke-NativeChecked -FilePath 'wsl.exe' -Arguments @(
            '--distribution', $DistroName, '--user', 'root', '--exec', '/bin/true'
        ) | Out-Null
        if (-not (Test-WslRootScript 'test "$(cat /proc/1/comm)" = systemd')) {
            throw 'Ubuntu no inicio con systemd despues de reiniciarse.'
        }
    }
    Write-InstallerMessage 'Ubuntu WSL2 y systemd estan listos.' 'PASS'
}

function Install-UbuntuRuntime {
    $linuxRepo = Invoke-WslCapture @(
        '--distribution', $DistroName, '--user', 'root', '--exec',
        'wslpath', '-a', $RepoRoot
    )
    if (-not $linuxRepo) { throw 'No se pudo traducir la ruta del repositorio a WSL.' }
    Write-InstallerMessage 'Instalando/actualizando paquetes y runtime Ubuntu. Esto puede tardar.'
    Invoke-NativeChecked -FilePath 'wsl.exe' -Arguments @(
        '--distribution', $DistroName, '--user', 'root', '--exec',
        'bash', "$linuxRepo/deploy/install-ubuntu.sh"
    ) | Out-Null

    $dropInScript = @'
set -euo pipefail
repo="$1"
install -d -m 0755 /etc/systemd/system/cbrs-dashboard.service.d
install -m 0644 "$repo/deploy/cbrs-dashboard-wsl.conf" /etc/systemd/system/cbrs-dashboard.service.d/wsl-local.conf
install -d -m 0755 /etc/systemd/system/cbrs-worker.service.d
install -m 0644 "$repo/deploy/cbrs-worker-wsl.conf" /etc/systemd/system/cbrs-worker.service.d/wsl-dashboard.conf
systemctl daemon-reload
systemctl enable cbrs-display.service cbrs-x11vnc.service cbrs-novnc.service cbrs-dashboard.service cbrs-backup.timer cbrs-worker-resume.path cbrs-configuration-apply.path
systemctl disable cbrs-worker.service >/dev/null 2>&1 || true
'@
    Invoke-WslRootScript -Script $dropInScript -Arguments @($linuxRepo) | Out-Null
    Write-InstallerMessage 'Runtime Ubuntu instalado de forma idempotente.' 'PASS'
    return $linuxRepo
}

function Install-WindowsStartupBridge {
    $startup = [Environment]::GetFolderPath('Startup')
    if (-not $startup) { throw 'Windows no devolvio la carpeta de Inicio del usuario.' }
    $vbsSource = Join-Path $RepoRoot 'deploy\windows\Start-CbrsWslHidden.vbs'
    Copy-Item -LiteralPath $vbsSource -Destination (Join-Path $startup 'CBRS Dashboard.vbs') -Force

    $shell = New-Object -ComObject WScript.Shell
    $shortcutPath = Join-Path $startup 'CBRS Ubuntu Runtime.lnk'
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $env:WINDIR 'System32\wsl.exe'
    $shortcut.Arguments = "-d $DistroName --exec /bin/true"
    $shortcut.WorkingDirectory = Join-Path $env:WINDIR 'System32'
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'Inicia el runtime Ubuntu CBRS al iniciar sesion'
    $shortcut.Save()
    Write-InstallerMessage 'Inicio automatico y dashboard nativo configurados.' 'PASS'
}

function Test-ProtectedConfigurationExists {
    $probe = @'
set -e
test -s /etc/cbrs/cbrs.env
test -s /var/lib/cbrs/account-pool.json
grep -qE '^CBRS_ACCOUNT_[0-9]+_USERNAME=.+$' /etc/cbrs/cbrs.env
grep -qE '^RESTIC_REPOSITORY=.+$' /etc/cbrs/cbrs.env
'@
    return Test-WslRootScript $probe
}

function Get-DefaultBackupRepository {
    $drive = Get-PSDrive -PSProvider FileSystem |
        Where-Object { $_.Name -ne 'C' -and $_.Free -gt 5GB } |
        Sort-Object Free -Descending |
        Select-Object -First 1
    if (-not $drive) { return '/srv/cbrs-backup/restic' }
    $windowsPath = Join-Path $drive.Root 'cbrs-backup\restic'
    try {
        $linuxPath = Invoke-WslCapture @(
            '--distribution', $DistroName, '--user', 'root', '--exec',
            'wslpath', '-a', $windowsPath
        )
        if ($linuxPath) { return $linuxPath }
    } catch {
        return '/srv/cbrs-backup/restic'
    }
    return '/srv/cbrs-backup/restic'
}

function Read-AccountConfiguration {
    Write-Host ''
    Write-InstallerMessage 'Los valores protegidos no se mostraran ni se escribiran en el log.' 'WARN'
    while ($true) {
        $countText = (Read-Host 'Cantidad de cuentas autorizadas [3]').Trim()
        if (-not $countText) { $countText = '3' }
        $accountCount = 0
        if ([int]::TryParse($countText, [ref]$accountCount) -and $accountCount -ge 1 -and $accountCount -le 50) {
            break
        }
        Write-InstallerMessage 'Ingrese un numero entre 1 y 50.' 'WARN'
    }

    $accounts = @()
    $usedProxyUrls = @{}
    for ($index = 1; $index -le $accountCount; $index++) {
        Write-Host ''
        Write-InstallerMessage "Configurando cuenta $index de $accountCount"
        $username = Read-RequiredValue "Correo/usuario de la cuenta $index"
        $password = Read-ProtectedValue "Contrasena de la cuenta $index"
        while ($true) {
            $proxy = Read-ProxyValue "Proxy HTTP(S) completo de la cuenta $index"
            if (-not $usedProxyUrls.ContainsKey($proxy)) {
                $usedProxyUrls[$proxy] = $true
                break
            }
            Write-InstallerMessage 'Ese proxy ya fue asignado. El modo normal exige una ruta distinta por cuenta.' 'WARN'
        }
        while ($true) {
            $quotaText = (Read-Host "Cuota diaria autorizada de la cuenta $index [20]").Trim()
            if (-not $quotaText) { $quotaText = '20' }
            $quota = 0
            if ([int]::TryParse($quotaText, [ref]$quota) -and $quota -gt 0) { break }
            Write-InstallerMessage 'La cuota debe ser un entero mayor que cero.' 'WARN'
        }
        $accounts += [ordered]@{
            id = "ejecutivo_$index"
            label = "Ejecutivo $index"
            username = $username
            password = $password
            proxy_url = $proxy
            daily_quota = $quota
        }
    }

    $defaultRepository = Get-DefaultBackupRepository
    $repository = (Read-Host "Repositorio restic [$defaultRepository]").Trim()
    if (-not $repository) { $repository = $defaultRepository }
    $resticPassword = Read-ConfirmedProtectedValue 'Contrasena nueva/existente de restic (guardela en el gestor aprobado)'
    return [ordered]@{
        accounts = $accounts
        backup = [ordered]@{
            repository = $repository
            password = $resticPassword
            password_file = '/etc/cbrs/restic-password'
        }
    }
}

function Send-ProtectedConfiguration {
    param([object]$Configuration)
    $json = $Configuration | ConvertTo-Json -Depth 8 -Compress
    $oldEncoding = $OutputEncoding
    try {
        $OutputEncoding = New-Object Text.UTF8Encoding($false)
        $wslArguments = @(
            '--distribution', $DistroName, '--user', 'root', '--exec',
            '/opt/cbrs/.venv/bin/python', '/opt/cbrs/deploy/configure_runtime.py'
        )
        $result = $json | & wsl.exe @wslArguments
        $code = $LASTEXITCODE
    } finally {
        $OutputEncoding = $oldEncoding
        $json = $null
    }
    if ($code -ne 0) { throw 'Ubuntu rechazo la configuracion protegida.' }
    $summary = ($result -join '') | ConvertFrom-Json
    if (-not $summary.ok) { throw 'La configuracion protegida no pudo aplicarse.' }
    Write-InstallerMessage (
        'Configuracion aplicada: {0} cuenta(s), capacidad {1}/dia.' -f
        $summary.accounts_configured, $summary.capacity_per_day
    ) 'PASS'
}

function Initialize-ResticAndBackup {
    $script = @'
set -euo pipefail
env_runner=/opt/cbrs/deploy/run_with_env.py
python=/opt/cbrs/.venv/bin/python
if ! runuser -u cbrs -- "$python" "$env_runner" /etc/cbrs/cbrs.env -- restic snapshots >/dev/null 2>&1; then
  runuser -u cbrs -- "$python" "$env_runner" /etc/cbrs/cbrs.env -- restic init
fi
systemctl start cbrs-display.service cbrs-x11vnc.service cbrs-novnc.service cbrs-dashboard.service
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8765/ >/dev/null; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8765/ >/dev/null
systemctl start cbrs-backup.service
systemctl is-active --quiet cbrs-dashboard.service
'@
    Invoke-WslRootScript -Script $script | Out-Null
    Write-InstallerMessage 'Repositorio restic y primer backup verificados.' 'PASS'
}

function Invoke-OfflineReadiness {
    $script = @'
set -euo pipefail
install -d -o cbrs -g cbrs -m 0750 /var/lib/cbrs/readiness
cd /opt/cbrs
runuser -u cbrs -- .venv/bin/python -m cbrs readiness \
  --target ubuntu \
  --env-file /etc/cbrs/cbrs.env \
  --config /var/lib/cbrs/account-pool.json \
  --json-report /var/lib/cbrs/readiness/pre-live.json
'@
    return Invoke-WslRootScript -Script $script -AllowFailure
}

function Invoke-AuthorizedProxyGate {
    $script = @'
set -euo pipefail
cd /opt/cbrs
systemctl start cbrs-display.service
runuser -u cbrs -- .venv/bin/python deploy/run_with_env.py /etc/cbrs/cbrs.env -- \
  .venv/bin/python -m cbrs pool proxy-health \
  --config /var/lib/cbrs/account-pool.json \
  --approve-egress-baseline
'@
    Invoke-WslRootScript -Script $script | Out-Null
}

function Enable-SafeServices {
    $script = @'
set -euo pipefail
systemctl enable --now cbrs-display.service cbrs-x11vnc.service cbrs-novnc.service
systemctl enable --now cbrs-dashboard.service cbrs-backup.timer
systemctl is-active --quiet cbrs-display.service
systemctl is-active --quiet cbrs-dashboard.service
'@
    Invoke-WslRootScript -Script $script | Out-Null
}

function Enable-LiveWorker {
    $script = @'
set -euo pipefail
systemctl enable --now cbrs-worker.service
systemctl is-active --quiet cbrs-worker.service
'@
    Invoke-WslRootScript -Script $script | Out-Null
}

function Write-Plan {
    Assert-RepositoryAssets
    Write-Host @'

PLAN SEGURO DEL INSTALADOR CBRS
-------------------------------
1. Elevar una sola vez con UAC.
2. Verificar/habilitar WSL2 y VirtualMachinePlatform.
3. Instalar o reutilizar Ubuntu-24.04 y systemd.
4. Instalar Python 3.14, Chrome Linux, Xvfb, noVNC, restic y dependencias.
5. Preservar cualquier SQLite, PDF, perfil, baseline y configuracion existente.
6. Recibir cuentas/proxies mediante campos protegidos y enviarlos por stdin.
7. Inicializar respaldo cifrado y ejecutar el gate offline.
8. Solicitar autorizacion separada antes de proxy-health o trafico CBRS.
9. Instalar el dashboard local con configuracion protegida de cuentas, cola,
   ejemplos comprobados y vista previa local de PDFs.
10. Abrir el dashboard mediante el navegador nativo de Windows.
11. Entregar un reporte sanitizado PASS/FAIL.

El modo plan no modifica Windows, WSL, archivos, servicios ni red.
'@
}

try {
    $RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
    $script:BatPath = Join-Path $RepoRoot 'INSTALL-CBRS.bat'
    if ($PlanOnly) {
        Write-Plan
        exit 0
    }
    if (-not (Test-IsAdministrator)) {
        throw 'Ejecute INSTALL-CBRS.bat como administrador.'
    }

    $logDirectory = Join-Path $env:ProgramData 'CBRS\install'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $script:LogPath = Join-Path $logDirectory ("install-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType File -Path $script:LogPath -Force | Out-Null

    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host '        INSTALADOR E2E - PLATAFORMA CBRS' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-InstallerMessage "Log sanitizado: $script:LogPath"
    Write-InstallerMessage 'El instalador no iniciara trafico CBRS sin una confirmacion separada.' 'WARN'

    Write-InstallerStep 'Validar checkout, equipo y privilegios'
    Assert-RepositoryAssets
    Assert-HostPrerequisites
    Write-InstallerMessage 'Checkout completo y permisos de administrador confirmados.' 'PASS'

    Write-InstallerStep 'Preparar WSL2'
    Ensure-WindowsFeatures
    Ensure-WslDistribution

    Write-InstallerStep 'Instalar runtime Ubuntu'
    $linuxRepo = Install-UbuntuRuntime

    Write-InstallerStep 'Configurar inicio y dashboard nativo'
    Install-WindowsStartupBridge

    Write-InstallerStep 'Configurar cuentas, proxies y respaldo'
    $preserve = $false
    if (Test-ProtectedConfigurationExists) {
        $preserve = Read-YesNo 'Existe configuracion protegida. ¿Conservarla sin sobrescribir?' $true
    }
    if (-not $preserve) {
        $configuration = Read-AccountConfiguration
        Send-ProtectedConfiguration -Configuration $configuration
        $configuration = $null
    } else {
        Write-InstallerMessage 'Configuracion existente preservada.' 'PASS'
    }

    Write-InstallerStep 'Inicializar backup y servicios locales seguros'
    Enable-SafeServices
    Initialize-ResticAndBackup

    Write-InstallerStep 'Ejecutar readiness offline'
    $readinessCode = Invoke-OfflineReadiness
    if ($readinessCode -eq 0) {
        Write-InstallerMessage 'Readiness offline/live gate completo.' 'PASS'
    } else {
        Write-InstallerMessage 'Readiness detecto gates vivos pendientes; no se iniciara el worker.' 'WARN'
    }

    $proxyApproved = $false
    if (Read-YesNo '¿Existe autorizacion para comprobar proxies y aprobar ahora los baselines CL?' $false) {
        Write-InstallerStep 'Ejecutar proxy-health autorizado'
        Invoke-AuthorizedProxyGate
        $proxyApproved = $true
        Write-InstallerMessage 'Proxy-health y baselines completados.' 'PASS'
        $readinessCode = Invoke-OfflineReadiness
    }

    $workerStarted = $false
    if ($readinessCode -eq 0) {
        Write-InstallerStep 'Gate final de operacion'
        Write-InstallerMessage 'Iniciar el worker puede procesar jobs durables ya encolados y contactar CBRS.' 'WARN'
        if (Read-YesNo '¿Autoriza habilitar e iniciar ahora el worker CBRS?' $false) {
            Enable-LiveWorker
            $workerStarted = $true
            Write-InstallerMessage 'Worker habilitado y activo.' 'PASS'
        } else {
            Write-InstallerMessage 'Worker instalado pero detenido por decision del operador.' 'WARN'
        }
    }

    Remove-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce' `
        -Name 'CBRSInstallResume' -ErrorAction SilentlyContinue

    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Green
    Write-Host '                INSTALACION COMPLETADA' -ForegroundColor Green
    Write-Host '============================================================' -ForegroundColor Green
    Write-InstallerMessage 'Dashboard: http://127.0.0.1:8765/' 'PASS'
    Write-InstallerMessage 'CAPTCHA visual: http://127.0.0.1:6080/vnc.html'
    Write-InstallerMessage 'Reporte: /var/lib/cbrs/readiness/pre-live.json'
    if ($readinessCode -eq 0) {
        Write-InstallerMessage 'Estado: LIVE READY' 'PASS'
    } else {
        Write-InstallerMessage 'Estado: INSTALADO, CON GATES VIVOS PENDIENTES' 'WARN'
    }
    if (-not $workerStarted) {
        Write-InstallerMessage 'El worker no fue iniciado; no se generara trafico CBRS.' 'WARN'
    }
    exit 0
} catch {
    Write-Host ''
    Write-InstallerMessage $_.Exception.Message 'FAIL'
    if ($script:LogPath) {
        Write-InstallerMessage "Revise el log sanitizado: $script:LogPath" 'WARN'
    }
    exit 1
}
