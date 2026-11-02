<#
.SYNOPSIS
  Registers Fundus (`fundus serve`) as a Task Scheduler task that starts when the current user logs on.

.DESCRIPTION
  The server runs hidden (pythonw.exe when available) and writes its output to -LogFile. The scheduler inside the
  server runs the nightly import, tidy, embeddings and snapshots (fundus/scheduler.py), so no further tasks are needed.
  No administrator rights required. `fundus service install` calls this script with the right paths.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install-fundus-task.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install-fundus-task.ps1 -BindHost 0.0.0.0 -Port 8010
#>
param(
  [string]$Repo = "",
  [string]$Python = "",
  [string]$BindHost = "127.0.0.1",
  [int]$Port = 8010,
  [string]$TaskName = "Fundus",
  [string]$LogFile = "",
  [switch]$NoStart
)
$ErrorActionPreference = "Stop"

if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
if (-not $LogFile) { $LogFile = Join-Path $env:LOCALAPPDATA "Fundus\logs\server.log" }
if (-not $Python) {
  $candidates = @((Join-Path $Repo ".venv\Scripts\pythonw.exe"), (Join-Path $Repo ".venv\Scripts\python.exe"))
  $Python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $Python) { $Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }
  if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction Stop).Source }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null
$arguments = "-m fundus.cli serve --host $BindHost --port $Port --log-file `"$LogFile`""
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description "Fundus evidence archive: fundus serve on http://${BindHost}:$Port/portal/" -Force | Out-Null

if (-not $NoStart) { Start-ScheduledTask -TaskName $TaskName }
Write-Output "Fundus task '$TaskName' registered: $Python $arguments"
Write-Output "Log: $LogFile"
