#Requires -Version 5.1
<#
.SYNOPSIS
  Start a demo StrategyInstance soak (schedulable).

.DESCRIPTION
  Sets KALSHI_ENV=demo and runs main.py with an instance YAML.
  Duration uses --max-runtime-minutes so shutdown is graceful (cancel resting).

.PARAMETER Instance
  Path to StrategyInstance YAML (default: gu_sports_underdog demo).

.PARAMETER DiscoverOnly
  Preview tickers and exit (no trading).

.PARAMETER DurationMinutes
  If > 0, bot shuts down gracefully after N minutes.
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
$env:PYTHONUNBUFFERED = "1"

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

Write-Host ""
Write-Host "=== DEMO StrategyInstance soak ===" -ForegroundColor Cyan
Write-Host "  instance: $Instance"
Write-Host "  env:      $env:KALSHI_ENV"
Write-Host "  python:   $Python"
if ($DurationMinutes -gt 0) {
    Write-Host "  duration: ${DurationMinutes}m (graceful --max-runtime-minutes)"
} else {
    Write-Host "  duration: until Ctrl+C / scheduler stop"
}
Write-Host ""

$pyArgs = @("main.py", "--instance", $Instance)
if ($DiscoverOnly) {
    $pyArgs += "--discover-only"
}
if ($DurationMinutes -gt 0 -and -not $DiscoverOnly) {
    $pyArgs += @("--max-runtime-minutes", "$DurationMinutes")
}

& $Python @pyArgs
exit $LASTEXITCODE
