#Requires -Version 5.1
<#
.SYNOPSIS
  Production micro-pilot: high_prob strategy, $1 (100c) fixed stake per entry.

.DESCRIPTION
  Run in a dedicated terminal alongside run_green_up_prod.ps1 for A/B comparison.
  Uses isolated prod DB + JSONL log so P&L can be compared without mixing trades.

.PARAMETER Ticker
  Trade a single ticker (skips auto-discovery). Example: KXMLBTOTAL-26MAY241420HOUCHC-6

.PARAMETER DiscoverOnly
  Run discovery table only; no orders.

.PARAMETER SkipConfirm
  Skip the production YES confirmation prompt (automation only).

.EXAMPLE
  .\scripts\run_high_prob_prod.ps1
  .\scripts\run_high_prob_prod.ps1 -DiscoverOnly
  .\scripts\run_high_prob_prod.ps1 -Ticker KXMLBTOTAL-26MAY241420HOUCHC-6
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
$StakeCents          = 100          # fixed entry stake ($1.00)
$MinYesAsk           = 70           # implied P(YES) floor (70%)
$LimitOffset         = -2           # bid - 2c when using limit_offset entry
$StopLossPct         = "0.10"       # 10% below entry fill
$TakeProfitPct       = 0.25         # 25% of (entry + vig)
$DiscoverCategory    = "Sports"
$DiscoverTop         = 3
$MaxConcurrent       = 1            # prod micro-pilot: one open position
$MonitorInterval     = 30

# ── Production profile ───────────────────────────────────────────────────────
$env:KALSHI_ENV                          = "production"
$env:KALSHI_PROD_MAX_POSITION_CENTS      = "$StakeCents"
$env:KALSHI_MAX_POSITION_CENTS             = "$StakeCents"
$env:KALSHI_PROD_MAX_CONCURRENT_POSITIONS  = "$MaxConcurrent"
$env:KALSHI_PROD_DB_PATH                   = "kalshi_bot_prod_high_prob.db"
$env:KALSHI_PROD_LOG_FILE                  = "kalshi_bot_prod_high_prob.jsonl"

# Strategy-specific (high_prob reads these at runtime)
$env:KALSHI_HP_LIMIT_OFFSET = "$LimitOffset"
$env:KALSHI_HP_STOP_LOSS    = "$StopLossPct"

# ── Safety gate ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== HIGH_PROB PRODUCTION MICRO-PILOT ===" -ForegroundColor Yellow
Write-Host "  Real money  |  stake=`$$([math]::Round($StakeCents / 100, 2)) per entry"
Write-Host "  min P(YES)  |  ${MinYesAsk}c ask"
Write-Host "  entry       |  limit_offset (bid $($LimitOffset)c)"
Write-Host "  exit        |  tp_and_stop (TP $($TakeProfitPct*100)%, stop $($StopLossPct))"
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
    "--strategy", "high_prob",
    "--hp-min-yes-ask", "$MinYesAsk",
    "--discover-min-yes-ask", "$MinYesAsk",
    "--hp-entry-mode", "limit_offset",
    "--hp-post-fill", "tp_and_stop",
    "--hp-take-profit-pct", "$TakeProfitPct",
    "--hp-stake-cents", "$StakeCents",
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
Write-Host "  Select-String -Path $($env:KALSHI_PROD_LOG_FILE) -Pattern 'HighProb|ORDER_SENT|Session complete'"
