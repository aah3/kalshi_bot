#Requires -Version 5.1
<#
.SYNOPSIS
  Production micro-pilot: green_up strategy, $1 (100c) max position cap per entry leg.

.DESCRIPTION
  Run in a dedicated terminal alongside run_high_prob_prod.ps1 for A/B comparison.
  Uses isolated prod DB + JSONL log. Entry sizing is fractional Kelly capped at
  100c (may be less than $1 on a given signal); high_prob uses a fixed 100c stake.

.PARAMETER Ticker
  Trade a single ticker (skips auto-discovery).

.PARAMETER DiscoverOnly
  Run discovery table only; no orders.

.PARAMETER SkipConfirm
  Skip the production YES confirmation prompt (automation only).

.EXAMPLE
  .\scripts\run_green_up_prod.ps1
  .\scripts\run_green_up_prod.ps1 -DiscoverOnly
  .\scripts\run_green_up_prod.ps1 -Ticker KXNBAGAME-26MAY23NYKCLE-NYK
#>
param(
    [string]$Ticker = "",
    [switch]$DiscoverOnly,
    [switch]$SkipConfirm
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# ── Tunables (edit here) ─────────────────────────────────────────────────────
$MaxPositionCents    = 100          # Kelly cap = $1.00 max entry leg
$EntryMax            = 30           # enter when YES ask <= 30c
$HedgeTrigger        = 44           # hedge when YES bid >= 44c
$HedgeMode           = "stake_back" # full_green | stake_back | partial
$StopLoss            = 0.35         # fraction below entry (sell YES path)
$LimitOffset         = -2           # bid - 2c entry (parity with high_prob)
$MaxCycles           = 1            # one round-trip per ticker per session
$DiscoverCategory    = "Sports"
$DiscoverTop         = 3
$MaxConcurrent       = 1
$MonitorInterval     = 30

# ── Production profile ───────────────────────────────────────────────────────
$env:KALSHI_ENV                          = "production"
$env:KALSHI_PROD_MAX_POSITION_CENTS      = "$MaxPositionCents"
$env:KALSHI_MAX_POSITION_CENTS             = "$MaxPositionCents"
$env:KALSHI_PROD_MAX_CONCURRENT_POSITIONS  = "$MaxConcurrent"
$env:KALSHI_PROD_DB_PATH                   = "kalshi_bot_prod_green_up.db"
$env:KALSHI_PROD_LOG_FILE                  = "kalshi_bot_prod_green_up.jsonl"
$env:KALSHI_MAX_SECTOR_CONCENTRATION       = "1.0"

# ── Safety gate ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== GREEN_UP PRODUCTION MICRO-PILOT ===" -ForegroundColor Yellow
Write-Host "  Real money  |  max entry leg `$$( [math]::Round($MaxPositionCents / 100, 2) ) (Kelly-sized, capped)"
Write-Host "  entry       |  ask <= ${EntryMax}c, limit_offset (bid $($LimitOffset)c)"
Write-Host "  hedge       |  $HedgeMode @ ${HedgeTrigger}c bid"
Write-Host "  stop        |  $($StopLoss * 100)% below entry"
Write-Host "  db / log    |  $($env:KALSHI_PROD_DB_PATH) / $($env:KALSHI_PROD_LOG_FILE)"
Write-Host ""

if (-not $SkipConfirm) {
    $confirm = Read-Host "Type YES to trade on PRODUCTION"
    if ($confirm -ne "YES") {
        Write-Host "Aborted." -ForegroundColor DarkGray
        exit 0
    }
}

# ── Build command ────────────────────────────────────────────────────────────
$argsList = @(
    "main.py",
    "--strategy", "green_up",
    "--entry-max", "$EntryMax",
    "--hedge-trigger", "$HedgeTrigger",
    "--hedge-mode", $HedgeMode,
    "--stop-loss", "$StopLoss",
    "--gu-entry-mode", "limit_offset",
    "--gu-limit-offset", "$LimitOffset",
    "--gu-exit-mode", "cross_spread",
    "--gu-max-cycles", "$MaxCycles",
    "--max-concurrent-positions", "$MaxConcurrent",
    "--monitor-interval", "$MonitorInterval",
    "--quiet"
)

if ($Ticker) {
    $argsList += @("--tickers", $Ticker)
} else {
    $argsList += @(
        "--discover",
        "--discover-category", $DiscoverCategory,
        "--discover-top", "$DiscoverTop"
    )
}

if ($DiscoverOnly) {
    $argsList += "--discover-only"
}

Write-Host "Starting: python $($argsList -join ' ')" -ForegroundColor Cyan
Write-Host ""

python @argsList

Write-Host ""
Write-Host "Session ended. Review P&L:" -ForegroundColor Green
Write-Host "  `$env:KALSHI_PROD_DB_PATH = '$($env:KALSHI_PROD_DB_PATH)'"
Write-Host "  python tools/blotter_report.py session"
Write-Host "  python tools/blotter.py pnl-by-strategy --days 1"
Write-Host "  Select-String -Path $($env:KALSHI_PROD_LOG_FILE) -Pattern 'GreenUp|ORDER_SENT|hedge filled|STOP|Session complete'"
