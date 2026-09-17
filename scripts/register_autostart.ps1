#Requires -Version 5.1
<#
.SYNOPSIS
  Register a Windows Task Scheduler task that auto-starts the production
  StrategyInstance runner at user logon — for an unattended overnight soak.

.DESCRIPTION
  This is deliberately NOT run automatically by anything in this repo.
  Per the MVP hard rules: only register autostart when the operator
  explicitly asks for an unattended overnight run, after an attended
  session has already passed on the current code. Do not run this script
  speculatively.

  The registered task launches scripts/run_instance_prod.ps1 with
  -SkipConfirm (required for unattended start) against the given instance
  YAML. It does NOT register scripts/watch_kill_switch.ps1 automatically —
  run that separately (or in the same interactive session) if you want
  live kill-switch alerting during the soak.

  Shutdown behaviour is unchanged: the bot's own SIGINT/--max-runtime-minutes
  path cancels resting orders only. It does not flatten positions. This
  script does not add any flatten-on-exit behaviour.

.PARAMETER Instance
  Path to the production StrategyInstance YAML (must not be a *.demo.* file
  — run_instance_prod.ps1 already refuses those, this is a second guard so
  a bad task definition fails fast at registration time instead of at 3am).

.PARAMETER DurationMinutes
  Passed through to run_instance_prod.ps1 -DurationMinutes so an
  unattended run still has a hard ceiling and exits gracefully.

.PARAMETER TaskName
  Scheduled task name. Default "KalshiBotProdAutostart".

.PARAMETER Confirm
  Required. This script refuses to register anything unless -Confirm is
  passed, so it can never be run by accident (e.g. copy-pasting an example).

.EXAMPLE
  .\scripts\register_autostart.ps1 -DurationMinutes 480 -Confirm
#>
param(
    [string]$Instance = "config/instances/gu_sports_underdog.prod.yaml",
    [int]$DurationMinutes = 480,
    [string]$TaskName = "KalshiBotProdAutostart",
    [switch]$Confirm
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not $Confirm) {
    Write-Host "Refusing to register a Task Scheduler autostart without -Confirm." -ForegroundColor Red
    Write-Host "Only do this when the operator has explicitly asked for an unattended overnight soak" -ForegroundColor Red
    Write-Host "AFTER an attended session has already passed on the current code." -ForegroundColor Red
    exit 1
}

if ($Instance -match '\.demo\.') {
    Write-Host "Refusing demo instance for autostart: $Instance" -ForegroundColor Red
    exit 1
}

$runnerPath = Join-Path $ProjectRoot "scripts\run_instance_prod.ps1"
if (-not (Test-Path $runnerPath)) {
    Write-Host "Runner not found: $runnerPath" -ForegroundColor Red
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Task '$TaskName' already exists. Run scripts\unregister_autostart.ps1 first." -ForegroundColor Red
    exit 1
}

$argList = "-NoProfile -ExecutionPolicy Bypass -File `"$runnerPath`" -Instance `"$Instance`" -DurationMinutes $DurationMinutes -SkipConfirm"

$action    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argList -WorkingDirectory $ProjectRoot
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Kalshi bot production StrategyInstance autostart (real money, $1 cap). Registered $(Get-Date -Format o)." | Out-Null

Write-Host ""
Write-Host "Registered Task Scheduler task '$TaskName'." -ForegroundColor Yellow
Write-Host "  trigger:  at logon"
Write-Host "  instance: $Instance"
Write-Host "  duration: ${DurationMinutes}m (graceful --max-runtime-minutes)"
Write-Host ""
Write-Host "Remove it with: .\scripts\unregister_autostart.ps1 -TaskName $TaskName" -ForegroundColor DarkGray
