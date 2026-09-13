#Requires -Version 5.1
<#
.SYNOPSIS
  Start a production StrategyInstance micro-pilot ($1 cap in the YAML).

.DESCRIPTION
  Forces KALSHI_ENV=production. Requires typing YES unless -SkipConfirm.
  Refuses demo YAML paths. Duration uses --max-runtime-minutes (graceful stop).

.PARAMETER Instance
  Path to production StrategyInstance YAML.

.PARAMETER DiscoverOnly
  Preview tickers and exit (no orders).

.PARAMETER DurationMinutes
  If > 0, bot shuts down gracefully after N minutes.

.PARAMETER SkipConfirm
  Skip the YES gate (automation only; still production).

.EXAMPLE
  .\scripts\run_instance_prod.ps1 -DiscoverOnly
  .\scripts\run_instance_prod.ps1 -DurationMinutes 90
#>
param(
    [string]$Instance = "config/instances/gu_sports_underdog.prod.yaml",
    [switch]$DiscoverOnly,
    [int]$DurationMinutes = 0,
    [switch]$SkipConfirm
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ($Instance -match '\.demo\.') {
    Write-Host "Refusing demo instance on the production runner: $Instance" -ForegroundColor Red
    exit 1
}

$env:KALSHI_ENV = "production"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

Write-Host ""
Write-Host "=== PRODUCTION StrategyInstance ===" -ForegroundColor Yellow
Write-Host "  instance: $Instance"
Write-Host "  env:      $env:KALSHI_ENV"
Write-Host "  python:   $Python"
if ($DurationMinutes -gt 0) {
    Write-Host "  duration: ${DurationMinutes}m (graceful --max-runtime-minutes)"
} else {
    Write-Host "  duration: until Ctrl+C"
}
Write-Host "  Real money. Shutdown cancels resting orders; it does not flatten positions."
Write-Host ""

if (-not $SkipConfirm -and -not $DiscoverOnly) {
    $confirm = Read-Host "Type YES to trade on PRODUCTION"
    if ($confirm -ne "YES") {
        Write-Host "Aborted." -ForegroundColor DarkGray
        exit 0
    }
}

$pyArgs = @("main.py", "--instance", $Instance)
if ($DiscoverOnly) {
    $pyArgs += "--discover-only"
}
if ($DurationMinutes -gt 0 -and -not $DiscoverOnly) {
    $pyArgs += @("--max-runtime-minutes", "$DurationMinutes")
}

& $Python @pyArgs
exit $LASTEXITCODE
