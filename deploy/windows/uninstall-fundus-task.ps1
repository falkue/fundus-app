<#
.SYNOPSIS
  Stops Fundus and removes its Task Scheduler task (registered by install-fundus-task.ps1). Data stays untouched.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall-fundus-task.ps1
#>
param(
  [string]$TaskName = "Fundus"
)
$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
  Write-Output "No task '$TaskName' registered."
  exit 0
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output "Fundus task '$TaskName' stopped and removed."
