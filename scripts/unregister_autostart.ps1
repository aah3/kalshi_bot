#Requires -Version 5.1
<#
.SYNOPSIS
  Remove the Task Scheduler autostart task created by register_autostart.ps1.

.DESCRIPTION
  Unregisters the scheduled task only. It does not touch a currently
  running bot process — if one is running, stop it the normal way
  (Ctrl+C in its window, which cancels resting orders only; never
  Stop-Process).

.PARAMETER TaskName
  Scheduled task name. Default "KalshiBotProdAutostart".

.EXAMPLE
  .\scripts\unregister_autostart.ps1
#>
param(
    [string]$TaskName = "KalshiBotProdAutostart"
)

$ErrorActionPreference = "Stop"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "No task named '$TaskName' is registered. Nothing to do." -ForegroundColor DarkGray
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Unregistered Task Scheduler task '$TaskName'. Nothing will autostart at logon." -ForegroundColor Yellow
