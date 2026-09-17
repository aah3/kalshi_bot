#Requires -Version 5.1
<#
.SYNOPSIS
  Watch a running bot instance for a kill-switch trip and alert the operator.

.DESCRIPTION
  Polls two independent signals so a trip is never missed even if one of
  them lags:
    1. The out-of-band sentinel file written by risk/kill_switch_alert.py
       (fsync'd the instant the kill switch fires — survives even a
       process exit right after the trip).
    2. The instance's JSONL log, grepped for "kill switch" / "RISK_BREACH"
       (in case the sentinel file itself is unavailable for any reason).

  This script is READ-ONLY with respect to the trading process:
    - It NEVER calls Stop-Process.
    - It NEVER cancels orders or flattens positions — that is already done
      by the bot's own CircuitBreaker -> ExecutionManager.cancel_all_orders()
      before this script would even see the alert.
    - It only observes and notifies (console banner + beep; best-effort
      Windows toast if BurntToast is installed).

.PARAMETER DbPath
  Path to the instance's SQLite DB (same value as KALSHI_DB_PATH / the
  instance YAML's persistence.db_path). Used to locate the sentinel file
  at "<db_stem>.kill_switch_alert.json" next to it — mirrors
  risk.kill_switch_alert.kill_switch_alert_path() in Python.

.PARAMETER LogPath
  Path to the instance's JSONL log (fallback signal).

.PARAMETER PollSeconds
  Seconds between checks. Default 5.

.PARAMETER Loop
  Keep watching after the first alert (useful for an unattended overnight
  soak). Default: exit after the first alert so an attended session gets
  a clear "watcher done" signal.

.EXAMPLE
  .\scripts\watch_kill_switch.ps1 -DbPath kalshi_bot_prod_instance.db -LogPath kalshi_bot_prod_instance.jsonl
.EXAMPLE
  .\scripts\watch_kill_switch.ps1 -DbPath kalshi_bot_prod_instance.db -Loop
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$DbPath,

    [string]$LogPath = "",

    [int]$PollSeconds = 5,

    [switch]$Loop
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Get-KillSwitchAlertPath {
    param([string]$DbPath)
    $dbFull = Resolve-Path -LiteralPath $DbPath -ErrorAction SilentlyContinue
    if (-not $dbFull) {
        # DB may not exist yet (bot not started) — resolve relative to CWD by hand.
        $dbFull = Join-Path (Get-Location) $DbPath
    }
    $dir  = Split-Path -Parent $dbFull
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($dbFull)
    return Join-Path $dir "$stem.kill_switch_alert.json"
}

function Send-KillSwitchAlert {
    param([string]$Reason, [string]$Source)

    Write-Host ""
    Write-Host "########################################################" -ForegroundColor Red
    Write-Host "#  KILL SWITCH TRIPPED                                 #" -ForegroundColor Red
    Write-Host "########################################################" -ForegroundColor Red
    Write-Host "  source: $Source"
    Write-Host "  reason: $Reason"
    Write-Host "  time:   $(Get-Date -Format o)"
    Write-Host "  Orders were cancelled by the bot itself; this watcher did not"
    Write-Host "  touch the process. Positions are NOT flattened — check"
    Write-Host "  'tools/trade.py portfolio' and decide manually."
    Write-Host ""

    try { [console]::beep(1000, 400); [console]::beep(1000, 400) } catch {}

    try {
        if (Get-Module -ListAvailable -Name BurntToast) {
            Import-Module BurntToast -ErrorAction Stop
            New-BurntToastNotification -Text "Kalshi bot kill switch", $Reason
        }
    } catch {
        # Toast notifications are best-effort only; never fail the watcher over this.
    }
}

$alertPath = Get-KillSwitchAlertPath -DbPath $DbPath
Write-Host "Watching for kill switch:"
Write-Host "  sentinel: $alertPath"
if ($LogPath) { Write-Host "  log:      $LogPath" }
Write-Host "  poll:     every ${PollSeconds}s"
Write-Host ""

$seenLogBytes = 0
if ($LogPath -and (Test-Path $LogPath)) {
    $seenLogBytes = (Get-Item $LogPath).Length
}

while ($true) {
    if (Test-Path $alertPath) {
        $payload = Get-Content -Raw -LiteralPath $alertPath | ConvertFrom-Json
        Send-KillSwitchAlert -Reason $payload.reason -Source "sentinel file"
        if (-not $Loop) { break }
        # Avoid re-alerting on the same sentinel every poll.
        Remove-Item -LiteralPath $alertPath -ErrorAction SilentlyContinue
    }

    if ($LogPath -and (Test-Path $LogPath)) {
        $len = (Get-Item $LogPath).Length
        if ($len -gt $seenLogBytes) {
            $newText = Get-Content -Raw -LiteralPath $LogPath
            $tail = $newText.Substring([Math]::Min($seenLogBytes, $newText.Length))
            $seenLogBytes = $len
            if ($tail -match "kill switch activated" -or $tail -match '"event":\s*"RISK_BREACH"') {
                Send-KillSwitchAlert -Reason "see log for detail" -Source "log tail"
                if (-not $Loop) { break }
            }
        }
    }

    Start-Sleep -Seconds $PollSeconds
}
