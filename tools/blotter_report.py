"""
tools/blotter_report.py — combined blotter query + performance analytics CLI

Run this daily to review paper trading session results before going live.
Combines trade history, P&L breakdown, and performance metrics in one report.

────────────────────────────────────────────────────────────────────────────────
COMMANDS
────────────────────────────────────────────────────────────────────────────────

  PERFORMANCE REPORTS
  ───────────────────
  # Full performance report (last 30 days)
  python tools/blotter_report.py performance

  # Shorter window
  python tools/blotter_report.py performance --days 7

  # Export full report to JSON
  python tools/blotter_report.py performance --json report.json

  DAILY SESSION SUMMARY
  ──────────────────────
  # Today's session summary (closed + settled trades)
  python tools/blotter_report.py session

  # Yesterday's session
  python tools/blotter_report.py session --days 1

  QUICK STATS
  ───────────
  python tools/blotter_report.py stats           # headline numbers only
  python tools/blotter_report.py calibration     # Kelly calibration check
  python tools/blotter_report.py daily-pnl       # day-by-day P&L series
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from metrics.blotter import Blotter
from metrics.performance import PerformanceAnalytics


# ── Handlers ──────────────────────────────────────────────────────────────────

def cmd_performance(args, blotter: Blotter, perf: PerformanceAnalytics) -> None:
    report = perf.full_report(days=args.days)
    perf.print_report(report)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"  Report exported → {args.json}\n")
    if args.csv:
        # Export underlying trade data to CSV
        closed  = blotter.query_trades(status="closed",  days=args.days)
        settled = blotter.query_trades(status="settled", days=args.days)
        blotter.export_csv(closed + settled, filepath=args.csv)
        print(f"  Trade data exported → {args.csv}\n")


def cmd_session(args, blotter: Blotter, perf: PerformanceAnalytics) -> None:
    """Print a concise summary of a single day's trading session."""
    days = args.days or 1
    closed  = blotter.query_trades(status="closed",  days=days)
    settled = blotter.query_trades(status="settled", days=days)
    open_t  = blotter.query_trades(status="open")
    all_closed = closed + settled

    total_pnl     = sum(t.net_pnl_cents or 0 for t in all_closed)
    total_cost    = sum(t.total_cost_cents for t in all_closed)
    total_fees    = sum(t.total_fees_cents for t in all_closed)
    wins          = sum(1 for t in all_closed if (t.net_pnl_cents or 0) > 0)
    win_rate      = wins / len(all_closed) * 100 if all_closed else 0.0

    pf = perf.profit_factor(all_closed)
    ex = perf.expectancy(all_closed)
    so = perf.sortino_ratio(all_closed)

    w = 65
    day_label = "TODAY" if days == 1 else f"LAST {days} DAYS"
    print(f"\n{'═' * w}")
    print(f"  SESSION SUMMARY — {day_label}  "
          f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")
    print(f"{'═' * w}")
    print(f"  {'Closed trades:':<30} {len(all_closed)}"
          f"  ({wins}W / {len(all_closed)-wins}L)")
    print(f"  {'Win rate:':<30} {win_rate:.1f}%")
    print(f"  {'Total invested:':<30} ${total_cost/100:.2f}")
    print(f"  {'Total fees paid:':<30} ${total_fees/100:.2f}")
    print(f"  {'Net P&L:':<30} ${total_pnl/100:+.2f}")
    print(f"  {'Profit factor:':<30} {pf:.3f}" if pf else f"  {'Profit factor:':<30} —")
    print(f"  {'Expectancy:':<30} ${ex:.4f}/trade" if ex else f"  {'Expectancy:':<30} —")
    print(f"  {'Sortino ratio:':<30} {so:.4f}" if so else f"  {'Sortino ratio:':<30} —")
    print(f"  {'Still open:':<30} {len(open_t)} trades")

    if all_closed:
        print(f"\n  CLOSED TRADES")
        for t in sorted(all_closed, key=lambda x: x.entry_time, reverse=True):
            pnl_str  = f"${t.net_pnl_cents/100:+.2f}" if t.net_pnl_cents else "—"
            hold_str = f"{t.hold_minutes:.0f}m" if t.hold_minutes else "—"
            status   = "✓" if (t.net_pnl_cents or 0) > 0 else ("✗" if (t.net_pnl_cents or 0) < 0 else "—")
            print(f"  {status} {t.trade_id:<9} {t.ticker:<30} {t.strategy[:18]:<18} "
                  f"{pnl_str:>10}  hold={hold_str}  res={t.resolution or '?'}")

    print(f"{'═' * w}\n")


def cmd_stats(args, blotter: Blotter, perf: PerformanceAnalytics) -> None:
    """Print headline statistics only — quick morning check."""
    days   = args.days or 30
    report = perf.full_report(days=days)
    wr     = report["win_rate"]
    pf     = report.get("profit_factor")
    ex     = report.get("expectancy_usd")
    so     = report.get("sortino_ratio")
    risk   = report.get("open_risk", {})

    print(f"\n  HEADLINE STATS — last {days} days  ({wr['total']} trades)\n")
    print(f"  Win rate:       {wr['win_rate_pct']:.1f}%  ({wr['wins']}W/{wr['losses']}L)")
    print(f"  Profit factor:  {pf:.3f}" if pf else "  Profit factor:  —")
    print(f"  Expectancy:     ${ex:.4f}/trade" if ex else "  Expectancy:     —")
    print(f"  Sortino ratio:  {so:.4f}" if so else "  Sortino ratio:  —")
    print(f"  Open positions: {risk.get('open_trade_count',0)}  "
          f"(${risk.get('total_at_risk_usd',0):.2f} at risk)\n")

    # Strategy breakdown as one-liner per strategy
    for row in report.get("pnl_by_strategy", []):
        pnl_str = f"${row['total_pnl_usd']:+.2f}"
        print(f"  [{row['group'][:20]:<20}]  {row['trade_count']:>3} trades  "
              f"{row['win_rate_pct']:>5.1f}% win  {pnl_str:>10}")
    print()


def cmd_calibration(args, blotter: Blotter, perf: PerformanceAnalytics) -> None:
    days  = args.days or 30
    calib = perf.kelly_calibration(days=days)
    if not calib:
        print(f"\n  No calibration data (need ≥5 closed trades per strategy).\n")
        return

    print(f"\n  KELLY CALIBRATION — last {days} days\n")
    for c in calib:
        ratio_str = f"{c['calibration_ratio']:.2f}"
        ok = 0.80 <= c['calibration_ratio'] <= 1.10
        status = "✓ OK" if ok else "⚠  CHECK"
        print(f"  {status}  {c['strategy']}")
        print(f"         Model win rate:  {c['model_win_rate']*100:.1f}%")
        print(f"         Actual win rate: {c['actual_win_rate']*100:.1f}%")
        print(f"         Ratio:           {ratio_str}")
        print(f"         → {c['recommendation']}\n")


def cmd_daily_pnl(args, blotter: Blotter, perf: PerformanceAnalytics) -> None:
    days   = args.days or 30
    series = perf.daily_pnl_series(days=days)
    if not series:
        print("\n  No closed trade data.\n")
        return

    print(f"\n  DAILY P&L — last {days} days\n")
    print(f"  {'DATE':<12}  {'DAILY P&L':>12}  {'CUMULATIVE':>12}  CHART")
    print(f"  {'─'*55}")
    max_abs = max(abs(r["pnl_usd"]) for r in series) or 1
    for r in series:
        bar_len = int(abs(r["pnl_usd"]) / max_abs * 20)
        bar     = ("█" * bar_len) if r["pnl_usd"] >= 0 else ("░" * bar_len)
        sign    = "+" if r["pnl_usd"] >= 0 else ""
        cum_sgn = "+" if r["cumulative_usd"] >= 0 else ""
        print(f"  {r['date']:<12}  {sign}${r['pnl_usd']:>10.2f}  "
              f"{cum_sgn}${r['cumulative_usd']:>10.2f}  {bar}")
    print()
    if args.csv:
        import csv as csv_mod, io
        buf = io.StringIO()
        writer = csv_mod.DictWriter(buf, fieldnames=["date","pnl_usd","cumulative_usd"])
        writer.writeheader()
        writer.writerows(series)
        with open(args.csv, "w") as f:
            f.write(buf.getvalue())
        print(f"  Exported → {args.csv}\n")


# ── CLI wiring ────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="Kalshi blotter report + performance analytics",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
sub = parser.add_subparsers(dest="cmd", required=True)

p_perf = sub.add_parser("performance", help="Full performance report")
p_perf.add_argument("--days", type=int, default=30)
p_perf.add_argument("--json", default=None, metavar="FILE.json")
p_perf.add_argument("--csv",  default=None, metavar="FILE.csv")

p_sess = sub.add_parser("session", help="Session summary (today or last N days)")
p_sess.add_argument("--days", type=int, default=1)

p_stat = sub.add_parser("stats", help="Headline stats only")
p_stat.add_argument("--days", type=int, default=30)

p_cal = sub.add_parser("calibration", help="Kelly calibration check")
p_cal.add_argument("--days", type=int, default=30)

p_dp = sub.add_parser("daily-pnl", help="Day-by-day P&L series")
p_dp.add_argument("--days", type=int, default=30)
p_dp.add_argument("--csv",  default=None)


if __name__ == "__main__":
    args    = parser.parse_args()
    blotter = Blotter()
    perf    = PerformanceAnalytics(blotter)

    print(f"[blotter-report] ENV={config.ENV}  DB={config.DB_PATH}")

    if args.cmd == "performance":  cmd_performance(args, blotter, perf)
    elif args.cmd == "session":    cmd_session(args, blotter, perf)
    elif args.cmd == "stats":      cmd_stats(args, blotter, perf)
    elif args.cmd == "calibration": cmd_calibration(args, blotter, perf)
    elif args.cmd == "daily-pnl":  cmd_daily_pnl(args, blotter, perf)
