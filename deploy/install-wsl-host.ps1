[CmdletBinding()]
param([string]$Distro = 'Ubuntu-24.04')
$ErrorActionPreference = 'Stop'
if ($Distro -notmatch '^[a-zA-Z0-9._-]+$') { throw 'Invalid distribution name' }
# Only a host bootstrap: every CBRS service, timer and Chrome process is Linux.
$taskUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -Command "& wsl.exe -d ' + $Distro + ' -u cbrs --exec /usr/bin/sleep infinity"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
Register-ScheduledTask -TaskName 'CBRS WSL Host' -Action $action -Principal $principal -Settings $settings -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $taskUser) -Force | Out-Null
Start-ScheduledTask -TaskName 'CBRS WSL Host'
