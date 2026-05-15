"""
tools/session_report.py — end-of-day session report

Generates a complete, structured summary of a trading session combining:
  - Trade blotter (all fills, legs, P&L)
  - Performance analytics (win rate, profit factor, Sortino, Kelly calibration)
  - Risk events (circuit breaker trips, alert firings from log)
  - Open position inventory (still at risk going into next session)
  - Fee reconciliation (exchange fees paid vs P&L)

Outputs to terminal and optionally saves a JSON report file for archiving.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

  # Today's session
  python tools/session_report.py

  # Yesterday
  python tools/session_report.py --days 1

  # Specific date
  python tools/session_report.py --date 2024-11-15

  # Save to JSON
  python tools/session_report.py --json reports/session_2024-11-15.json

  # Full 30-day review
  python tools/session_report.py --days 30 --json reports/monthly.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from metrics.blotter import Blotter
from metrics.performance import PerformanceAnalytics


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(blotter: Blotter, perf: PerformanceAnalytics, days: int, date: str | None) -> dict:
    """
    Assemble the full session report dict.

    Args:
        blotter:  Blotter instance
        perf:     PerformanceAnalytics instance
        days:     Look-back window in days
        date:     Optional specific date string "YYYY-MM-DD" (overrides days)
    """
    now = datetime.now(timezone.utc)

    # ── Date filtering ────────────────────────────────────────────────────────
    if date:
        from_date = date
        to_date   = date
        label     = date
    else:
        from_date = None
        to_date   = None
        label     = f"Last {days} day{'s' if days != 1 else ''}"

    # ── Trade data ────────────────────────────────────────────────────────────
    closed  = blotter.query_trades(status="closed",  days=days, from_date=from_date, to_date=to_date)
    settled = blotter.query_trades(status="settled", days=days, from_date=from_date, to_date=to_date)
    open_t  = blotter.open_positions_summary()
    all_done = closed + settled

    # ── P&L aggregates ────────────────────────────────────────────────────────
    total_net_pnl   = sum(t.net_pnl_cents or 0 for t in all_done)
    total_fees      = sum(t.total_fees_cents for t in all_done)
    total_cost      = sum(t.total_cost_cents for t in all_done)
    gross_profit    = sum(t.net_pnl_cents or 0 for t in all_done if (t.net_pnl_cents or 0) > 0)
    gross_loss      = sum(t.net_pnl_cents or 0 for t in all_done if (t.net_pnl_cents or 0) < 0)
    wins            = sum(1 for t in all_done if (t.net_pnl_cents or 0) > 0)
    losses          = sum(1 for t in all_done if (t.net_pnl_cents or 0) < 0)

    # ── Performance metrics ───────────────────────────────────────────────────
    pf       = perf.profit_factor(all_done)
    ex       = perf.expectancy(all_done)
    sortino  = perf.sortino_ratio(all_done)
    hold     = perf.avg_hold_time(all_done)
    streak   = perf.streak_analysis(all_done)
    best_d   = perf.best_day(all_done)
    worst_d  = perf.worst_day(all_done)
    daily    = perf.daily_pnl_series(all_done)
    calib    = perf.kelly_calibration(all_done)
    by_strat = blotter.pnl_by_strategy(days=days)
    by_cat   = blotter.pnl_by_category(days=days)
    best_t   = blotter.best_trades(n=5, days=days)
    worst_t  = blotter.worst_trades(n=5, days=days)

    # ── Risk events (from structured log) ─────────────────────────────────────
    risk_events = _parse_risk_events_from_log(days=days)

    # ── Open position inventory ───────────────────────────────────────────────
    open_at_risk = sum(t["total_cost_cents"] for t in open_t)

    # ── Fee reconciliation ────────────────────────────────────────────────────
    fee_rate = config.FEE_PER_CONTRACT_CENTS
    total_contracts = sum(t.total_contracts for t in all_done)
    expected_fees   = int(fee_rate * total_contracts)
    fee_discrepancy = total_fees - expected_fees

    return {
        "report_generated_at": now.isoformat(),
        "period":              label,
        "days":                days,

        # ── Summary ───────────────────────────────────────────────────────────
        "summary": {
            "total_trades":      len(all_done),
            "closed_trades":     len(closed),
            "settled_trades":    len(settled),
            "open_positions":    len(open_t),
            "wins":              wins,
            "losses":            losses,
            "win_rate_pct":      round(wins / len(all_done) * 100, 2) if all_done else 0.0,
            "total_net_pnl_usd": round(total_net_pnl / 100, 2),
            "total_cost_usd":    round(total_cost / 100, 2),
            "roi_pct":           round(total_net_pnl / total_cost * 100, 2) if total_cost else 0.0,
            "gross_profit_usd":  round(gross_profit / 100, 2),
            "gross_loss_usd":    round(gross_loss / 100, 2),
            "profit_factor":     pf,
            "expectancy_usd":    ex,
            "sortino_ratio":     sortino,
            "open_at_risk_usd":  round(open_at_risk / 100, 2),
        },

        # ── Fees ──────────────────────────────────────────────────────────────
        "fees": {
            "total_contracts_traded": total_contracts,
            "fee_per_contract_cents": fee_rate,
            "expected_fees_usd":      round(expected_fees / 100, 2),
            "recorded_fees_usd":      round(total_fees / 100, 2),
            "discrepancy_cents":      fee_discrepancy,
            "note": (
                "Discrepancy > 0 means more fees were recorded than expected. "
                "Check for partial fills recorded at wrong contract counts."
                if abs(fee_discrepancy) > 10
                else "Fees reconcile correctly."
            ),
        },

        # ── Hold time ─────────────────────────────────────────────────────────
        "hold_time": hold,

        # ── Streak ────────────────────────────────────────────────────────────
        "streak": streak,

        # ── Best / worst day ──────────────────────────────────────────────────
        "best_day":  best_d,
        "worst_day": worst_d,

        # ── Daily P&L series ──────────────────────────────────────────────────
        "daily_pnl": daily,

        # ── P&L by dimension ──────────────────────────────────────────────────
        "by_strategy": by_strat,
        "by_category": by_cat,

        # ── Top 5 best / worst ────────────────────────────────────────────────
        "best_trades":  [t.to_dict() for t in best_t],
        "worst_trades": [t.to_dict() for t in worst_t],

        # ── Kelly calibration ─────────────────────────────────────────────────
        "kelly_calibration": calib,

        # ── Risk events ───────────────────────────────────────────────────────
        "risk_events": risk_events,

        # ── Open positions ────────────────────────────────────────────────────
        "open_positions": open_t,
    }


def _parse_risk_events_from_log(days: int) -> list[dict]:
    """
    Parse RISK_BREACH and ALERT events from the structured JSON log.
    Returns a list of event dicts from the last `days` days.
    """
    log_path = config.LOG_FILE
    if not os.path.exists(log_path):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events = []

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = entry.get("event", "")
                if event_type not in ("RISK_BREACH", "SHUTDOWN"):
                    # Also capture WARNING-level alert entries
                    if entry.get("level") != "WARNING":
                        continue
                    alert_type = entry.get("alert_type", "")
                    if not alert_type:
                        continue

                # Convert microsecond timestamp to datetime
                ts_us = entry.get("ts_us", 0)
                if ts_us:
                    ts = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)
                    if ts < cutoff:
                        continue

                events.append({
                    "ts":         datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc).isoformat() if ts_us else "",
                    "event":      entry.get("event", ""),
                    "level":      entry.get("level", ""),
                    "alert_type": entry.get("alert_type", ""),
                    "ticker":     entry.get("ticker", ""),
                    "msg":        entry.get("msg", ""),
                })
    except Exception:
        pass

    return events[-50:]   # cap at 50 most recent


# ── Terminal printer ──────────────────────────────────────────────────────────

def print_report(report: dict) -> None:
    w = 75
    period   = report["period"]
    summary  = report["summary"]
    fees     = report["fees"]
    streak   = report.get("streak", {})
    hold     = report.get("hold_time", {})
    risk_evs = report.get("risk_events", [])

    pnl_sign = "+" if summary["total_net_pnl_usd"] >= 0 else ""

    print(f"\n{'═' * w}")
    print(f"  SESSION REPORT — {period}")
    print(f"  Generated: {report['report_generated_at'][:19]} UTC")
    print(f"{'═' * w}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n  SUMMARY")
    print(f"  {'Trades (closed / settled):':<35} "
          f"{summary['closed_trades']} / {summary['settled_trades']}")
    print(f"  {'Win / Loss / Open:':<35} "
          f"{summary['wins']}W  {summary['losses']}L  {summary['open_positions']} open")
    print(f"  {'Win rate:':<35} {summary['win_rate_pct']:.1f}%")
    print(f"  {'Total invested:':<35} ${summary['total_cost_usd']:.2f}")
    print(f"  {'Net P&L:':<35} {pnl_sign}${summary['total_net_pnl_usd']:.2f}  "
          f"(ROI {summary['roi_pct']:+.1f}%)")
    print(f"  {'Gross profit / loss:':<35} "
          f"+${summary['gross_profit_usd']:.2f}  /  -${abs(summary['gross_loss_usd']):.2f}")
    pf = summary.get("profit_factor")
    print(f"  {'Profit factor:':<35} {pf:.3f}" if pf else f"  {'Profit factor:':<35} —")
    ex = summary.get("expectancy_usd")
    print(f"  {'Expectancy:':<35} ${ex:.4f} / trade" if ex else f"  {'Expectancy:':<35} —")
    so = summary.get("sortino_ratio")
    print(f"  {'Sortino ratio:':<35} {so:.4f}" if so else f"  {'Sortino ratio:':<35} —")
    print(f"  {'Still open (at risk):':<35} ${summary['open_at_risk_usd']:.2f}")

    # ── Fees ──────────────────────────────────────────────────────────────────
    print(f"\n  FEE RECONCILIATION")
    print(f"  {'Contracts traded:':<35} {fees['total_contracts_traded']}")
    print(f"  {'Fee rate:':<35} {fees['fee_per_contract_cents']}c / contract")
    print(f"  {'Expected fees:':<35} ${fees['expected_fees_usd']:.2f}")
    print(f"  {'Recorded fees:':<35} ${fees['recorded_fees_usd']:.2f}")
    disc = fees["discrepancy_cents"]
    disc_str = f"{'+' if disc >= 0 else ''}{disc}c"
    print(f"  {'Discrepancy:':<35} {disc_str}")
    print(f"  {fees['note']}")

    # ── Hold time ─────────────────────────────────────────────────────────────
    print(f"\n  HOLD TIME")
    overall = hold.get("overall_minutes")
    print(f"  {'Avg hold (all strategies):':<35} "
          f"{overall:.0f} min" if overall else f"  {'Avg hold:':<35} —")
    for strat, vals in (hold.get("by_strategy") or {}).items():
        print(f"  {'  ' + strat[:31]:<35} "
              f"avg={vals['avg_minutes']:.0f}m  "
              f"min={vals['min_minutes']:.0f}m  "
              f"max={vals['max_minutes']:.0f}m  "
              f"({vals['trade_count']} trades)")

    # ── Streak ────────────────────────────────────────────────────────────────
    print(f"\n  STREAK")
    cur      = streak.get("current_streak", 0)
    cur_type = streak.get("current_type", "none")
    print(f"  {'Current streak:':<35} {abs(cur)} {cur_type}")
    print(f"  {'Longest win streak:':<35} {streak.get('longest_win_streak', 0)}")
    print(f"  {'Longest loss streak:':<35} {streak.get('longest_loss_streak', 0)}")
    last10 = streak.get("last_10", [])
    if last10:
        print(f"  {'Last 10 results:':<35} {' '.join(last10)}")

    # ── Best / Worst day ──────────────────────────────────────────────────────
    bd = report.get("best_day")
    wd = report.get("worst_day")
    if bd or wd:
        print(f"\n  DAILY EXTREMES")
        if bd:
            print(f"  {'Best day:':<35} {bd['date']}  ${bd['pnl_usd']:+.2f}")
        if wd:
            print(f"  {'Worst day:':<35} {wd['date']}  ${wd['pnl_usd']:+.2f}")

    # ── P&L by strategy ───────────────────────────────────────────────────────
    by_strat = report.get("by_strategy", [])
    if by_strat:
        print(f"\n  P&L BY STRATEGY")
        print(f"  {'STRATEGY':<24}  {'TRADES':>6}  {'WIN%':>6}  "
              f"{'TOTAL P&L':>11}  {'AVG P&L':>9}  {'FEES':>8}")
        print(f"  {'─' * 70}")
        for r in by_strat:
            sign = "+" if r["total_pnl_usd"] >= 0 else ""
            print(f"  {r['group'][:24]:<24}  {r['trade_count']:>6}  "
                  f"{r['win_rate_pct']:>5.1f}%  "
                  f"{sign}${r['total_pnl_usd']:>9.2f}  "
                  f"${r['avg_pnl_usd']:>8.2f}  "
                  f"${r['total_fees_usd']:>7.2f}")

    # ── P&L by category ───────────────────────────────────────────────────────
    by_cat = report.get("by_category", [])
    if by_cat:
        print(f"\n  P&L BY CATEGORY")
        print(f"  {'CATEGORY':<24}  {'TRADES':>6}  {'WIN%':>6}  {'TOTAL P&L':>11}")
        print(f"  {'─' * 52}")
        for r in by_cat:
            sign = "+" if r["total_pnl_usd"] >= 0 else ""
            print(f"  {r['group'][:24]:<24}  {r['trade_count']:>6}  "
                  f"{r['win_rate_pct']:>5.1f}%  {sign}${r['total_pnl_usd']:>9.2f}")

    # ── Best / worst trades ───────────────────────────────────────────────────
    best = report.get("best_trades", [])
    worst = report.get("worst_trades", [])
    if best:
        print(f"\n  TOP 5 BEST TRADES")
        for t in best:
            pnl = t.get("net_pnl_usd") or 0
            print(f"  ✓  {t['trade_id']:<9}  {t['ticker']:<30}  "
                  f"{t['strategy'][:18]:<18}  +${pnl:.2f}")
    if worst:
        print(f"\n  TOP 5 WORST TRADES")
        for t in worst:
            pnl = t.get("net_pnl_usd") or 0
            print(f"  ✗  {t['trade_id']:<9}  {t['ticker']:<30}  "
                  f"{t['strategy'][:18]:<18}  ${pnl:.2f}")

    # ── Kelly calibration ─────────────────────────────────────────────────────
    calib = report.get("kelly_calibration", [])
    if calib:
        print(f"\n  KELLY CALIBRATION")
        for c in calib:
            ok     = 0.80 <= c["calibration_ratio"] <= 1.10
            status = "✓" if ok else "⚠"
            print(f"  {status}  {c['strategy'][:30]:<30}  "
                  f"model={c['model_win_rate']*100:.0f}%  "
                  f"actual={c['actual_win_rate']*100:.0f}%  "
                  f"ratio={c['calibration_ratio']:.2f}")
            if not ok:
                print(f"     → {c['recommendation']}")

    # ── Risk events ───────────────────────────────────────────────────────────
    if risk_evs:
        print(f"\n  RISK EVENTS ({len(risk_evs)} found)")
        for ev in risk_evs[-10:]:   # show last 10
            ts  = ev.get("ts", "")[:19]
            msg = ev.get("msg", ev.get("alert_type", ""))[:55]
            print(f"  ⚠  {ts}  {msg}")
    else:
        print(f"\n  RISK EVENTS   No risk events logged.")

    # ── Open positions ────────────────────────────────────────────────────────
    open_pos = report.get("open_positions", [])
    if open_pos:
        print(f"\n  OPEN POSITIONS (carry into next session)")
        for p in open_pos:
            print(f"  →  {p['trade_id']:<9}  {p['ticker']:<30}  "
                  f"{p['strategy'][:18]:<18}  "
                  f"${p['total_cost_cents']/100:.2f} at risk")

    print(f"\n{'═' * w}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="Kalshi end-of-session report",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
parser.add_argument("--days", type=int, default=1,
                    help="Look-back window (default: 1 = today)")
parser.add_argument("--date", type=str, default=None,
                    help="Specific date YYYY-MM-DD")
parser.add_argument("--json", type=str, default=None,
                    metavar="FILE.json", help="Save report to JSON file")


if __name__ == "__main__":
    args    = parser.parse_args()
    blotter = Blotter()
    perf    = PerformanceAnalytics(blotter)

    print(f"[session-report] ENV={config.ENV}  DB={config.DB_PATH}")

    report = build_report(blotter, perf, days=args.days, date=args.date)
    print_report(report)

    if args.json:
        os.makedirs(os.path.dirname(args.json) if os.path.dirname(args.json) else ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"  Report saved → {args.json}\n")
