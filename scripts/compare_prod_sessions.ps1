#Requires -Version 5.1
<#
.SYNOPSIS
  Compare green_up vs high_prob production micro-pilot sessions side by side.

.EXAMPLE
  .\scripts\compare_prod_sessions.ps1
  .\scripts\compare_prod_sessions.ps1 -Days 7
#>
param(
    [int]$Days = 1
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$env:KALSHI_ENV = "production"

$dbs = @{
    "green_up"  = "kalshi_bot_prod_green_up.db"
    "high_prob" = "kalshi_bot_prod_high_prob.db"
}

Write-Host ""
Write-Host "=== PRODUCTION A/B COMPARISON (last $Days day(s)) ===" -ForegroundColor Cyan
Write-Host ""

foreach ($name in @("green_up", "high_prob")) {
    $db = $dbs[$name]
    if (-not (Test-Path $db)) {
        Write-Host "--- $name ---  (no db: $db)" -ForegroundColor DarkYellow
        Write-Host ""
        continue
    }

    Write-Host ("=" * 60) -ForegroundColor Yellow
    Write-Host "  STRATEGY: $name" -ForegroundColor Yellow
    Write-Host ("=" * 60) -ForegroundColor Yellow

    $env:KALSHI_PROD_DB_PATH = $db
    python tools/blotter_report.py session --days $Days
    python tools/blotter.py pnl-by-strategy --days $Days
    Write-Host ""
}

Write-Host "Log grep (recent events):" -ForegroundColor Cyan
foreach ($pair in @(
    @{ name = "green_up";  log = "kalshi_bot_prod_green_up.jsonl" },
    @{ name = "high_prob"; log = "kalshi_bot_prod_high_prob.jsonl" }
)) {
    $logPath = $pair.log
    if (Test-Path $logPath) {
        Write-Host "  $($pair.name): $logPath" -ForegroundColor DarkGray
        Select-String -Path $logPath -Pattern "Session complete|entry filled|exit filled|hedge filled|STOP" |
            Select-Object -Last 5 |
            ForEach-Object { Write-Host "    $($_.Line)" }
    }
}
Write-Host ""
