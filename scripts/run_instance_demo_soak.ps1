#Requires -Version 5.1
<#
.SYNOPSIS
  Start a demo StrategyInstance soak (schedulable).

.DESCRIPTION
  Sets KALSHI_ENV=demo and runs main.py with an instance YAML.
  Use with Task Scheduler or a manual terminal.

.PARAMETER Instance
  Path to StrategyInstance YAML (default: gu_sports_underdog demo).

.PARAMETER DiscoverOnly
  Preview tickers and exit (no trading).

.PARAMETER DurationMinutes
  If > 0, stop the bot after N minutes via job timeout wrapper.
  0 = run until Ctrl+C / Task Scheduler stop.

.EXAMPLE
  .\scripts\run_instance_demo_soak.ps1
  .\scripts\run_instance_demo_soak.ps1 -DiscoverOnly
  .\scripts\run_instance_demo_soak.ps1 -DurationMinutes 90
#>
param(
    [string]$Instance = "config/instances/gu_sports_underdog.demo.yaml",
    [switch]$DiscoverOnly,
    [int]$DurationMinutes = 0
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$env:KALSHI_ENV = "demo"
$env:PYTHONIOENCODING = "utf-8"

Write-Host ""
Write-Host "=== DEMO StrategyInstance soak ===" -ForegroundColor Cyan
Write-Host "  instance: $Instance"
Write-Host "  env:      $env:KALSHI_ENV"
if ($DurationMinutes -gt 0) {
    Write-Host "  duration: ${DurationMinutes}m (auto-stop)"
} else {
    Write-Host "  duration: until Ctrl+C / scheduler stop"
}
Write-Host ""

$pyArgs = @("main.py", "--instance", $Instance)
if ($DiscoverOnly) {
    $pyArgs += "--discover-only"
}

if ($DurationMinutes -gt 0 -and -not $DiscoverOnly) {
    $timeoutSec = $DurationMinutes * 60
    $proc = Start-Process -FilePath "python" -ArgumentList $pyArgs `
        -WorkingDirectory $ProjectRoot -PassThru -NoNewWindow
    Write-Host "Started PID $($proc.Id); will stop after ${DurationMinutes}m"
    if (-not $proc.WaitForExit($timeoutSec * 1000)) {
        Write-Host "Duration reached — sending Ctrl+C-equivalent stop..." -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force
        Write-Host "Process stopped."
        exit 0
    }
    exit $proc.ExitCode
}

& python @pyArgs
exit $LASTEXITCODE
