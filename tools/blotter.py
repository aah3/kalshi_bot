"""
tools/blotter.py — trade blotter query CLI

Query, filter, and export historical trades. All outputs support
terminal table display and --csv export to a file.

────────────────────────────────────────────────────────────────────────────────
COMMANDS
────────────────────────────────────────────────────────────────────────────────

  TRADE HISTORY
  ─────────────
  # All trades (last 30 days)
  python tools/blotter.py trades

  # Filter by status
  python tools/blotter.py trades --status closed
  python tools/blotter.py trades --status open

  # Filter by category, strategy, or ticker
  python tools/blotter.py trades --category Politics
  python tools/blotter.py trades --strategy kelly
  python tools/blotter.py trades --ticker PRES-2024-DEM

  # Filter by date range
  python tools/blotter.py trades --days 7
  python tools/blotter.py trades --from 2024-11-01 --to 2024-11-30

  # Show trade legs (individual fills) for a specific parent trade
  python tools/blotter.py legs --trade-id T-0042

  # Show all legs for a ticker
  python tools/blotter.py legs --ticker PRES-2024-DEM

  P&L SUMMARIES
  ─────────────
  # P&L breakdown by strategy
  python tools/blotter.py pnl-by-strategy

  # P&L breakdown by category
  python tools/blotter.py pnl-by-category

  # Best and worst trades
  python tools/blotter.py best --top 10
  python tools/blotter.py worst --top 10

  # Open positions summary
  python tools/blotter.py open

  ANNOTATIONS
  ───────────
  python tools/blotter.py note --trade-id T-0042 --text "Greened up at 68c, clean exit"

  MANUAL SETTLEMENT
  ─────────────────
  python tools/blotter.py settle --trade-id T-0042 --resolution yes
  python tools/blotter.py settle --trade-id T-0042 --resolution no --price 0

  EXPORT
  ──────
  # Append --csv <filename> to any query command
  python tools/blotter.py trades --status closed --days 30 --csv trades_nov.csv
  python tools/blotter.py pnl-by-strategy --csv strategy_pnl.csv
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from credentials.credential_manager import CredentialManager
from execution.rate_limiter import RateLimiter
from metrics.blotter import Blotter, LegRecord, ParentRecord
from metrics.settlement import SettlementWatcher
from metrics.metrics_store import MetricsStore


# ── Formatting helpers ────────────────────────────────────────────────────────

COL_W = {
    "trade_id":      9,
    "ticker":       30,
    "category":     12,
    "strategy":     22,
    "status":       12,
    "contracts":     6,
    "cost":          9,
    "pnl":          10,
    "roi":           7,
    "entry_time":   20,
    "hold":          8,
    "resolution":    6,
}


def _pnl_str(val_usd: float | None) -> str:
    if val_usd is None:
        return "—"
    sign = "+" if val_usd >= 0 else ""
    return f"{sign}${val_usd:.2f}"


def _print_trades(records: list[ParentRecord]) -> None:
    if not records:
        print("\n  No trades found.\n")
        return

    w = 130
    print(f"\n{'─' * w}")
    print(
        f"  {'TRADE ID':<{COL_W['trade_id']}}  "
        f"{'TICKER':<{COL_W['ticker']}}  "
        f"{'CATEGORY':<{COL_W['category']}}  "
        f"{'STRATEGY':<{COL_W['strategy']}}  "
        f"{'STATUS':<{COL_W['status']}}  "
        f"{'QTY':>{COL_W['contracts']}}  "
        f"{'COST':>{COL_W['cost']}}  "
        f"{'NET P&L':>{COL_W['pnl']}}  "
        f"{'ROI%':>{COL_W['roi']}}  "
        f"{'ENTRY TIME':<{COL_W['entry_time']}}  "
        f"{'HOLD':>{COL_W['hold']}}  "
        f"{'RES':<{COL_W['resolution']}}"
    )
    print(f"{'─' * w}")

    for r in records:
        pnl_str = _pnl_str(r.net_pnl_usd)
        roi_str = f"{r.roi_pct:+.1f}%" if r.roi_pct is not None else "—"
        hold_str = f"{r.hold_minutes:.0f}m" if r.hold_minutes else "—"
        entry_ts = r.entry_time[:19].replace("T", " ")

        print(
            f"  {r.trade_id:<{COL_W['trade_id']}}  "
            f"{r.ticker:<{COL_W['ticker']}}  "
            f"{r.category:<{COL_W['category']}}  "
            f"{r.strategy[:22]:<{COL_W['strategy']}}  "
            f"{r.status:<{COL_W['status']}}  "
            f"{r.total_contracts:>{COL_W['contracts']}}  "
            f"${r.total_cost_cents/100:>{COL_W['cost']-1}.2f}  "
            f"{pnl_str:>{COL_W['pnl']}}  "
            f"{roi_str:>{COL_W['roi']}}  "
            f"{entry_ts:<{COL_W['entry_time']}}  "
            f"{hold_str:>{COL_W['hold']}}  "
            f"{r.resolution or '—':<{COL_W['resolution']}}"
        )

    print(f"{'─' * w}")
    total_pnl = sum(r.net_pnl_cents or 0 for r in records)
    total_cost = sum(r.total_cost_cents for r in records)
    print(f"  {len(records)} trades  |  Total cost: ${total_cost/100:.2f}  |  Total net P&L: {_pnl_str(total_pnl/100)}\n")


def _print_legs(records: list[LegRecord]) -> None:
    if not records:
        print("\n  No legs found.\n")
        return

    w = 130
    print(f"\n{'─' * w}")
    print(
        f"  {'LEG ID':<9}  {'PARENT':<9}  {'TICKER':<30}  {'SIDE':<5}  {'TYPE':<12}  "
        f"{'QTY':>5}  {'ENTRY':>6}  {'EXIT':>6}  {'FEES':>5}  "
        f"{'P&L':>10}  {'STATUS':<10}  {'RES':<5}"
    )
    print(f"{'─' * w}")

    for l in records:
        pnl = l.realised_pnl_cents or l.settlement_pnl_cents
        pnl_str = _pnl_str(pnl / 100 if pnl is not None else None)
        print(
            f"  {l.leg_id:<9}  {l.parent_trade_id:<9}  {l.ticker:<30}  "
            f"{l.side:<5}  {l.trade_type:<12}  "
            f"{l.contracts:>5}  {l.entry_price:>5}c  "
            f"{str(l.exit_price or '—'):>5}  "
            f"{l.fees_cents:>4}c  "
            f"{pnl_str:>10}  {l.status:<10}  {l.resolution or '—':<5}"
        )

    print(f"{'─' * w}\n")


def _print_summary(rows: list[dict], group_label: str) -> None:
    if not rows:
        print(f"\n  No closed trades found.\n")
        return

    w = 95
    print(f"\n{'─' * w}")
    print(
        f"  {group_label:<24}  {'TRADES':>6}  {'WINS':>5}  {'LOSSES':>6}  "
        f"{'WIN%':>6}  {'TOTAL P&L':>10}  {'AVG P&L':>9}  {'FEES':>8}  {'AVG HOLD':>9}"
    )
    print(f"{'─' * w}")
    for r in rows:
        pnl_str = _pnl_str(r["total_pnl_usd"])
        avg_str = _pnl_str(r["avg_pnl_usd"])
        print(
            f"  {r['group'][:24]:<24}  {r['trade_count']:>6}  {r['wins']:>5}  "
            f"{r['losses']:>6}  {r['win_rate_pct']:>5.1f}%  "
            f"{pnl_str:>10}  {avg_str:>9}  "
            f"${r['total_fees_usd']:>7.2f}  {r['avg_hold_minutes']:>7.1f}m"
        )
    print(f"{'─' * w}\n")


def _print_open(rows: list[dict]) -> None:
    if not rows:
        print("\n  No open positions.\n")
        return

    w = 110
    print(f"\n{'─' * w}")
    print(
        f"  {'TRADE ID':<9}  {'TICKER':<30}  {'CATEGORY':<12}  "
        f"{'STRATEGY':<20}  {'LEGS':>4}  {'QTY':>5}  {'COST':>9}  {'ENTRY TIME':<20}"
    )
    print(f"{'─' * w}")
    for r in rows:
        entry_ts = r["entry_time"][:19].replace("T", " ")
        print(
            f"  {r['trade_id']:<9}  {r['ticker']:<30}  "
            f"{r['category']:<12}  {r['strategy'][:20]:<20}  "
            f"{r['num_legs']:>4}  {r['total_contracts']:>5}  "
            f"${r['total_cost_cents']/100:>8.2f}  {entry_ts:<20}"
        )
    total_cost = sum(r["total_cost_cents"] for r in rows)
    print(f"{'─' * w}")
    print(f"  {len(rows)} open positions  |  Total at risk: ${total_cost/100:.2f}\n")


# ── Subcommand handlers ───────────────────────────────────────────────────────

def cmd_trades(args, blotter: Blotter) -> None:
    records = blotter.query_trades(
        status=args.status,
        category=args.category,
        strategy=args.strategy,
        ticker=args.ticker,
        from_date=args.from_date,
        to_date=args.to_date,
        days=args.days,
        limit=args.limit,
    )
    _print_trades(records)
    if args.csv:
        path = blotter.export_csv(records, filepath=args.csv)
        print(f"  Exported {len(records)} rows → {path}\n")


def cmd_legs(args, blotter: Blotter) -> None:
    records = blotter.query_legs(
        parent_trade_id=args.trade_id,
        ticker=args.ticker,
        trade_type=args.trade_type,
        status=args.status,
        days=args.days,
        limit=args.limit,
    )
    _print_legs(records)
    if args.csv:
        path = blotter.export_csv(records, filepath=args.csv)
        print(f"  Exported {len(records)} rows → {path}\n")


def cmd_pnl_by_strategy(args, blotter: Blotter) -> None:
    rows = blotter.pnl_by_strategy(days=args.days or 30)
    _print_summary(rows, group_label="STRATEGY")
    if args.csv:
        import csv as csv_mod
        import io
        buf = io.StringIO()
        if rows:
            writer = csv_mod.DictWriter(buf, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        with open(args.csv, "w") as f:
            f.write(buf.getvalue())
        print(f"  Exported → {args.csv}\n")


def cmd_pnl_by_category(args, blotter: Blotter) -> None:
    rows = blotter.pnl_by_category(days=args.days or 30)
    _print_summary(rows, group_label="CATEGORY")
    if args.csv:
        import csv as csv_mod, io
        buf = io.StringIO()
        if rows:
            writer = csv_mod.DictWriter(buf, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        with open(args.csv, "w") as f:
            f.write(buf.getvalue())
        print(f"  Exported → {args.csv}\n")


def cmd_best(args, blotter: Blotter) -> None:
    records = blotter.best_trades(n=args.top, days=args.days or 30)
    print(f"\n  TOP {args.top} BEST TRADES (last {args.days or 30} days)\n")
    _print_trades(records)
    if args.csv:
        blotter.export_csv(records, filepath=args.csv)
        print(f"  Exported → {args.csv}\n")


def cmd_worst(args, blotter: Blotter) -> None:
    records = blotter.worst_trades(n=args.top, days=args.days or 30)
    print(f"\n  TOP {args.top} WORST TRADES (last {args.days or 30} days)\n")
    _print_trades(records)
    if args.csv:
        blotter.export_csv(records, filepath=args.csv)
        print(f"  Exported → {args.csv}\n")


def cmd_open(args, blotter: Blotter) -> None:
    rows = blotter.open_positions_summary()
    _print_open(rows)


def cmd_note(args, blotter: Blotter) -> None:
    if args.trade_id:
        blotter.annotate_trade(args.trade_id, args.text)
        print(f"\n  ✓ Note added to {args.trade_id}\n")
    elif args.leg_id:
        blotter.annotate_leg(args.leg_id, args.text)
        print(f"\n  ✓ Note added to {args.leg_id}\n")
    else:
        print("\n  Provide --trade-id or --leg-id\n")


async def cmd_settle(args, blotter: Blotter) -> None:
    creds   = CredentialManager()
    limiter = RateLimiter()
    store   = MetricsStore()
    watcher = SettlementWatcher(blotter, creds, limiter, store)

    import aiohttp
    watcher._session = aiohttp.ClientSession()
    try:
        result = await watcher.force_settle(
            trade_id=args.trade_id,
            resolution=args.resolution,
            exit_price=args.price,
        )
        if result and result.success:
            print(f"\n  ✓ Trade {args.trade_id} settled as '{args.resolution}'")
            print(f"    Legs settled:  {result.legs_settled}")
            print(f"    Settlement P&L: {_pnl_str(result.total_settlement_pnl_cents / 100)}\n")
        else:
            print(f"\n  ✗ Settlement failed for {args.trade_id}\n")
    finally:
        await watcher._session.close()


def cmd_detail(args, blotter: Blotter) -> None:
    """Show full detail for one parent trade including all legs."""
    trade = blotter.get_trade(args.trade_id)
    if not trade:
        print(f"\n  Trade {args.trade_id} not found.\n")
        return

    print(f"\n{'═' * 70}")
    print(f"  TRADE DETAIL  —  {trade.trade_id}")
    print(f"{'═' * 70}")
    for k, v in trade.to_dict().items():
        print(f"  {k:<30} {v}")

    legs = blotter.get_legs_for_trade(args.trade_id)
    if legs:
        print(f"\n  LEGS ({len(legs)})")
        _print_legs(legs)
    print()


# ── CLI wiring ────────────────────────────────────────────────────────────────

def _add_filter_args(p):
    p.add_argument("--status",    default=None)
    p.add_argument("--category",  default=None)
    p.add_argument("--strategy",  default=None)
    p.add_argument("--ticker",    default=None)
    p.add_argument("--from",      dest="from_date", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--to",        dest="to_date",   default=None, metavar="YYYY-MM-DD")
    p.add_argument("--days",      type=int, default=None)
    p.add_argument("--limit",     type=int, default=200)
    p.add_argument("--csv",       default=None, metavar="FILE.csv")


parser = argparse.ArgumentParser(
    description="Kalshi trade blotter — query historical trades and P&L",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
sub = parser.add_subparsers(dest="cmd", required=True)

# trades
p_t = sub.add_parser("trades", help="Query trade history")
_add_filter_args(p_t)

# legs
p_l = sub.add_parser("legs", help="Query individual fill legs")
p_l.add_argument("--trade-id",   default=None)
p_l.add_argument("--ticker",     default=None)
p_l.add_argument("--trade-type", default=None)
p_l.add_argument("--status",     default=None)
p_l.add_argument("--days",       type=int, default=None)
p_l.add_argument("--limit",      type=int, default=500)
p_l.add_argument("--csv",        default=None)

# pnl-by-strategy
p_s = sub.add_parser("pnl-by-strategy", help="P&L breakdown by strategy")
p_s.add_argument("--days", type=int, default=30)
p_s.add_argument("--csv",  default=None)

# pnl-by-category
p_c = sub.add_parser("pnl-by-category", help="P&L breakdown by category")
p_c.add_argument("--days", type=int, default=30)
p_c.add_argument("--csv",  default=None)

# best
p_b = sub.add_parser("best", help="Top N best trades")
p_b.add_argument("--top",  type=int, default=10)
p_b.add_argument("--days", type=int, default=30)
p_b.add_argument("--csv",  default=None)

# worst
p_w = sub.add_parser("worst", help="Top N worst trades")
p_w.add_argument("--top",  type=int, default=10)
p_w.add_argument("--days", type=int, default=30)
p_w.add_argument("--csv",  default=None)

# open
sub.add_parser("open", help="Show open positions")

# detail
p_det = sub.add_parser("detail", help="Full detail for one trade including all legs")
p_det.add_argument("--trade-id", required=True)

# note
p_n = sub.add_parser("note", help="Add annotation to a trade")
p_n.add_argument("--trade-id", default=None)
p_n.add_argument("--leg-id",   default=None)
p_n.add_argument("--text",     required=True)

# settle
p_se = sub.add_parser("settle", help="Manually settle a trade")
p_se.add_argument("--trade-id",   required=True)
p_se.add_argument("--resolution", required=True, choices=["yes", "no", "void"])
p_se.add_argument("--price",      type=int, default=None, help="Override exit price (cents)")


if __name__ == "__main__":
    args    = parser.parse_args()
    blotter = Blotter()
    print(f"[blotter] ENV={config.ENV}  DB={config.DB_PATH}")

    if args.cmd == "trades":           cmd_trades(args, blotter)
    elif args.cmd == "legs":           cmd_legs(args, blotter)
    elif args.cmd == "pnl-by-strategy": cmd_pnl_by_strategy(args, blotter)
    elif args.cmd == "pnl-by-category": cmd_pnl_by_category(args, blotter)
    elif args.cmd == "best":           cmd_best(args, blotter)
    elif args.cmd == "worst":          cmd_worst(args, blotter)
    elif args.cmd == "open":           cmd_open(args, blotter)
    elif args.cmd == "detail":         cmd_detail(args, blotter)
    elif args.cmd == "note":           cmd_note(args, blotter)
    elif args.cmd == "settle":         asyncio.run(cmd_settle(args, blotter))
