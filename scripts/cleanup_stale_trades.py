"""
scripts/cleanup_stale_trades.py

Maintenance utility to reconcile or cancel stale OPEN blotter trades.

Two classes of cruft accumulate in the blotter:

  1. Pre-fix stopped/exited trades — the entry leg was left ``open`` (no realised
     P&L) and the closing fill was booked as a phantom second leg. These are now
     prevented at the source (closing fills call ``close_leg``), but older rows
     remain. ``--reconcile-stops`` closes the entry leg at the phantom close
     leg's price, cancels the phantom leg, and rolls the parent up to closed.

  2. Abandoned / unit-test seed trades (e.g. ticker ``T-B``) left ``open``.
     ``--cancel T-XXXX ...`` marks them (and any open legs) ``cancelled`` so they
     stop showing up as open positions in shutdown logs and reports.

SAFE BY DEFAULT: prints a dry-run plan and writes nothing. Pass ``--apply`` to
persist changes.

Usage:
    python scripts/cleanup_stale_trades.py                       # list + dry-run plan
    python scripts/cleanup_stale_trades.py --reconcile-stops --apply
    python scripts/cleanup_stale_trades.py --cancel T-0002 T-0004 --apply
    python scripts/cleanup_stale_trades.py --db kalshi_bot_prod.db --reconcile-stops
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from metrics.blotter import Blotter

_ENTRY_TYPES = ("entry", "leg_1", "leg_2")
_CLOSE_TYPES = ("stop_loss", "exit")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _open_parents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM parent_trades WHERE status = 'open' ORDER BY trade_id"
    ).fetchall()


def _open_legs(conn: sqlite3.Connection, trade_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trades WHERE parent_trade_id = ? AND status = 'open' "
        "ORDER BY leg_id",
        (trade_id,),
    ).fetchall()


def list_open(conn: sqlite3.Connection) -> None:
    parents = _open_parents(conn)
    if not parents:
        print("No open parent trades.")
        return
    print(f"Open parent trades ({len(parents)}):")
    for p in parents:
        print(
            f"  {p['trade_id']:7} {p['ticker']:32} {p['strategy']:28} "
            f"contracts={p['total_contracts']} entry={p['entry_time']}"
        )
        for leg in _open_legs(conn, p["trade_id"]):
            print(
                f"      {leg['leg_id']:7} {leg['side']:3} {leg['trade_type']:10} "
                f"x{leg['contracts']} @ {leg['entry_price']}c"
            )


def plan_reconcile_stops(conn: sqlite3.Connection) -> list[dict]:
    """Find open parents with an open entry leg + an open stop/exit leg."""
    actions: list[dict] = []
    for p in _open_parents(conn):
        legs = _open_legs(conn, p["trade_id"])
        entries = [l for l in legs if l["trade_type"] in _ENTRY_TYPES]
        closes  = [l for l in legs if l["trade_type"] in _CLOSE_TYPES]
        if len(entries) == 1 and len(closes) == 1:
            actions.append({
                "trade_id":   p["trade_id"],
                "ticker":     p["ticker"],
                "entry_leg":  entries[0]["leg_id"],
                "entry_px":   entries[0]["entry_price"],
                "close_leg":  closes[0]["leg_id"],
                "exit_px":    closes[0]["entry_price"],
                "close_type": closes[0]["trade_type"],
                "contracts":  entries[0]["contracts"],
            })
    return actions


def apply_reconcile(blotter: Blotter, conn: sqlite3.Connection, action: dict) -> None:
    # Void the phantom closing leg first so the parent rollup ignores it.
    conn.execute(
        "UPDATE trades SET status = 'cancelled', "
        "notes = 'voided by cleanup_stale_trades (phantom close leg)' "
        "WHERE leg_id = ?",
        (action["close_leg"],),
    )
    conn.commit()
    # Close the real entry leg at the observed exit price -> realised P&L.
    blotter.close_leg(
        action["entry_leg"],
        exit_price=action["exit_px"],
        close_type=action["close_type"],
    )
    blotter.close_trade(action["trade_id"], notes="reconciled by cleanup_stale_trades")


def cancel_trades(conn: sqlite3.Connection, trade_ids: list[str]) -> None:
    for tid in trade_ids:
        conn.execute(
            "UPDATE trades SET status = 'cancelled', "
            "notes = 'cancelled by cleanup_stale_trades' "
            "WHERE parent_trade_id = ? AND status = 'open'",
            (tid,),
        )
        conn.execute(
            "UPDATE parent_trades SET status = 'cancelled', "
            "notes = 'cancelled by cleanup_stale_trades' WHERE trade_id = ?",
            (tid,),
        )
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Reconcile/cancel stale open blotter trades")
    ap.add_argument("--db", default=config.DB_PATH, help="SQLite DB path (default: config.DB_PATH)")
    ap.add_argument("--reconcile-stops", action="store_true",
                    help="Close entry legs that have a phantom stop/exit leg")
    ap.add_argument("--cancel", nargs="*", default=[], metavar="TRADE_ID",
                    help="Parent trade ids to mark cancelled")
    ap.add_argument("--apply", action="store_true", help="Persist changes (otherwise dry-run)")
    args = ap.parse_args()

    print(f"DB: {args.db}   mode: {'APPLY' if args.apply else 'DRY-RUN'}\n")
    conn = _connect(args.db)

    list_open(conn)

    if args.reconcile_stops:
        plan = plan_reconcile_stops(conn)
        print(f"\nReconcile-stops plan ({len(plan)}):")
        for a in plan:
            pnl = (a["exit_px"] - a["entry_px"]) * a["contracts"]
            print(
                f"  {a['trade_id']}: close {a['entry_leg']} @ {a['exit_px']}c "
                f"({a['close_type']}, ~{pnl:+d}c), void {a['close_leg']}"
            )
        if args.apply and plan:
            blotter = Blotter(db_path=args.db)
            for a in plan:
                apply_reconcile(blotter, conn, a)
            print("  -> applied.")

    if args.cancel:
        print(f"\nCancel plan: {', '.join(args.cancel)}")
        if args.apply:
            cancel_trades(conn, args.cancel)
            print("  -> applied.")

    if not args.apply:
        print("\n(dry-run; re-run with --apply to persist)")

    conn.close()


if __name__ == "__main__":
    main()
