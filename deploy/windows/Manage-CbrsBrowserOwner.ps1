[CmdletBinding()]
param(
    [ValidateSet('Install','Start','RestartWorker','Status')][string]$Action = 'Status',
    [string]$RepoRoot,
    [string]$EnvFile = 'C:\ProgramData\CBRS\cbrs.env',
    [string]$OwnerRoot = 'G:\CBRS\browser-owner',
    [string]$OwnerTask = 'CBRS User Browser Owner',
    [string]$WorkerTask = 'CBRS User Worker'
)
$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $RepoRoot 'deploy\run_with_env.py'
function Read-OwnerStatus {
    $value = & $python $runner $EnvFile -- $python -c "import json; from cbrs.jobs import default_job_store; s=default_job_store(); print(json.dumps({'owner':s.active_lease('browser_owner'),'worker':s.active_lease()}))"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read independent owner leases.' }
    return ($value | ConvertFrom-Json)
}
function Find-Workers {
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -match '-m\s+cbrs\s+(?:--headless\s+)?jobs\s+worker'
    })
}
if ($Action -eq 'Status') { Read-OwnerStatus | ConvertTo-Json -Depth 5; return }
if (-not (Select-String -LiteralPath $EnvFile -Pattern '^CBRS_BROWSER_OWNER_MODE=external\s*$' -Quiet)) {
    throw 'Independent mode must be explicitly configured after the old coupled worker is drained. No browser was stopped.'
}
if ($Action -eq 'Install') {
    New-Item -ItemType Directory -Path $OwnerRoot -Force | Out-Null
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]'S-1-5-18', [Security.Principal.SecurityIdentifier]'S-1-5-32-544')) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $OwnerRoot -AclObject $acl
    $taskUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $script = Join-Path $RepoRoot 'deploy\windows\Invoke-CbrsNativeTask.ps1'
    $args = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Role owner -RepoRoot `"$RepoRoot`" -EnvFile `"$EnvFile`""
    $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $args -WorkingDirectory $RepoRoot
    Register-ScheduledTask -TaskName $OwnerTask -Action $taskAction -Principal $principal -Settings $settings -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $taskUser) -Force | Out-Null
    Write-Host 'Independent owner task installed. No existing process was restarted.'
    return
}
$status = Read-OwnerStatus
if ($Action -eq 'Start' -and -not $status.owner) {
    $survivors = @(Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*--user-data-dir*CBRS*') -or
        ($_.Name -eq 'python.exe' -and $_.CommandLine -match '-m\s+cbrs\.browser_owner\s+run')
    })
    if ($survivors.Count -or (Find-Workers).Count) { throw 'Existing browser/owner/worker retained; migration must be completed explicitly first.' }
    Enable-ScheduledTask -TaskName $OwnerTask | Out-Null
    Start-ScheduledTask -TaskName $OwnerTask
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Seconds 1
        $status = Read-OwnerStatus
    } until ($status.owner -or (Get-Date) -ge $deadline)
}
if (-not $status.owner) { throw 'Independent browser owner is unavailable; no worker was started or stopped.' }
if ($Action -eq 'RestartWorker') {
    # Disable automatic starts while draining. A failed drain leaves the task
    # disabled and all existing processes intact for inspection.
    Disable-ScheduledTask -TaskName $WorkerTask | Out-Null
    & $python $runner $EnvFile -- $python -m cbrs pool stop
    if ($LASTEXITCODE -ne 0) { throw 'Worker drain request failed.' }
    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 2
        $status = Read-OwnerStatus
        if (-not $status.owner) { throw 'Owner health lost while draining; inspect without restarting Chrome.' }
    } until ((-not $status.worker -and (Find-Workers).Count -eq 0) -or (Get-Date) -ge $deadline)
    if ($status.worker -or (Find-Workers).Count) { throw 'Worker has not drained. Nothing was forcibly terminated.' }
}
Enable-ScheduledTask -TaskName $WorkerTask | Out-Null
if ((Find-Workers).Count -eq 0) { Start-ScheduledTask -TaskName $WorkerTask }
Write-Host 'Worker start requested; independent Chrome owner retained. Verify fresh worker lease and unchanged browser PIDs.'
