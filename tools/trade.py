"""
tools/trade.py — manual trading and portfolio monitor CLI

The human-in-the-loop interface for placing orders and watching P&L
after selecting markets from the screener.

────────────────────────────────────────────────────────────────────────────────
COMMANDS
────────────────────────────────────────────────────────────────────────────────

  BUY / PLACE ORDERS
  ──────────────────
  # Preview a limit order before placing (no order sent)
  python tools/trade.py preview --ticker PRES-2024-DEM --side yes --count 10 --price 32

  # Place a limit order: buy 10 YES contracts at 32c
  python tools/trade.py buy --ticker PRES-2024-DEM --side yes --count 10 --price 32

  # Place a market order (fills at best available ask)
  python tools/trade.py buy --ticker PRES-2024-DEM --side yes --count 10 --market

  # Place a NO limit order at 40c (= buying NO at 40c, YES implied at 60c)
  python tools/trade.py buy --ticker PRES-2024-DEM --side no --count 5 --price 40

  ORDER MANAGEMENT
  ─────────────────
  # Check the status of a specific order
  python tools/trade.py status --order-id abc123

  # List all your resting (open) orders
  python tools/trade.py orders

  # Cancel one order
  python tools/trade.py cancel --order-id abc123

  # Cancel all resting orders
  python tools/trade.py cancel-all

  PORTFOLIO & P&L
  ────────────────
  # Show current positions with live mark-to-market P&L
  python tools/trade.py portfolio
  python tools/trade.py positions   # alias for portfolio

  # Continuous P&L monitor, refresh every 30 seconds
  python tools/trade.py monitor --interval 30

  # Show one position in detail
  python tools/trade.py position --ticker PRES-2024-DEM

  ACCOUNT
  ────────
  python tools/trade.py balance
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from credentials.credential_manager import CredentialManager
from discovery.market_client import MarketClient
from execution.rate_limiter import RateLimiter
from trading.auth_check import verify_portfolio_credentials
from trading.order_entry import OrderEntry, OrderRequest, OrderSide, OrderType, TimeInForce
from trading.portfolio_monitor import PortfolioMonitor


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_deps():
    creds   = CredentialManager()
    limiter = RateLimiter()
    return creds, limiter


def _print_preview(preview: dict) -> None:
    if not preview.get("valid"):
        print("\n  ✗ Order invalid:")
        for e in preview.get("errors", []):
            print(f"    • {e}")
        return

    print(f"\n{'─' * 65}")
    print(f"  ORDER PREVIEW  ({preview['order_type'].upper()} {preview['side'].upper()})")
    print(f"{'─' * 65}")
    print(f"  {'Ticker:':<28} {preview['ticker']}")
    print(f"  {'Contracts:':<28} {preview['count']}")
    lp = preview.get("limit_price")
    yp = preview.get("yes_price")
    print(f"  {'Limit price:':<28} {f'{lp}c' if lp is not None else 'MARKET'}")
    print(f"  {'YES price:':<28} {f'{yp}c' if yp is not None else 'MARKET'}")
    est = preview.get("estimated_cost_usd")
    roi = preview.get("implied_roi_pct")
    print(f"  {'Estimated cost:':<28} ${est:.2f}" if est is not None else f"  {'Estimated cost:':<28} ?")
    print(f"  {'Max payout:':<28} ${preview['max_payout_usd']}")
    print(f"  {'Implied ROI:':<28} {roi:+.1f}%" if roi is not None else f"  {'Implied ROI:':<28} —")

    book = preview.get("book", {})
    bid, ask = book.get("best_bid"), book.get("best_ask")
    print(f"\n  LIVE BOOK")
    print(f"  {'Best bid / ask:':<28} {bid}c / {ask}c" if bid is not None and ask is not None else f"  {'Best bid / ask:':<28} —")
    print(f"  {'Spread:':<28} {book.get('spread')}c" if book.get("spread") is not None else f"  {'Spread:':<28} —")
    mid = book.get("mid_price")
    print(f"  {'Mid (implied %):':<28} {mid}c" if mid is not None else f"  {'Mid (implied %):':<28} —")
    print(f"  {'Book position:':<28} {preview.get('book_position') or '—'}")

    slip = preview.get("slippage_estimate")
    if slip:
        print(f"\n  MARKET ORDER SLIPPAGE ESTIMATE")
        print(f"  {'Avg fill price:':<28} {slip.get('avg_fill_price_cents')}c")
        print(f"  {'Slippage vs best ask:':<28} {slip.get('slippage_cents')}c")
        print(f"  {'Fully fillable:':<28} {slip.get('fully_fillable')}")
        print(f"  {'Fillable contracts:':<28} {slip.get('fillable_count')}")

    if preview.get("warning"):
        print(f"\n  ⚠  {preview['warning']}")
    print(f"{'─' * 65}\n")


# ── Subcommand handlers ───────────────────────────────────────────────────────

async def cmd_preview(args):
    creds, limiter = _make_deps()
    req = OrderRequest(
        ticker=args.ticker,
        side=OrderSide(args.side),
        order_type=OrderType.MARKET if args.market else OrderType.LIMIT,
        count=args.count,
        limit_price=None if args.market else args.price,
        time_in_force=TimeInForce.from_cli(args.tif),
        note=args.note or "",
    )
    async with OrderEntry(creds, limiter) as oe, \
               MarketClient(creds, limiter) as mc:
        preview = await oe.preview_order(req, mc)
    _print_preview(preview)


async def cmd_buy(args):
    creds, limiter = _make_deps()
    req = OrderRequest(
        ticker=args.ticker,
        side=OrderSide(args.side),
        order_type=OrderType.MARKET if args.market else OrderType.LIMIT,
        count=args.count,
        limit_price=None if args.market else args.price,
        market_max_price=getattr(args, "max_price", None) if args.market else None,
        time_in_force=TimeInForce.from_cli(args.tif),
        note=args.note or "",
    )

    # Always show preview first
    async with OrderEntry(creds, limiter) as oe, \
               MarketClient(creds, limiter) as mc:
        preview = await oe.preview_order(req, mc)
        ok, auth_msg = await verify_portfolio_credentials(creds, limiter, oe._session)
    _print_preview(preview)

    if not preview.get("valid"):
        sys.exit(1)

    if not ok:
        print(f"\n  ✗ Cannot place orders: {auth_msg}\n")
        sys.exit(1)

    # Confirm unless --yes flag given
    if not args.yes:
        confirm = input("  Confirm order? [y/N] ").strip().lower()
        if confirm != "y":
            print("  Order cancelled by user.")
            return

    if args.market and preview.get("market_cap") is not None:
        req.market_max_price = int(preview["market_cap"])

    async with OrderEntry(creds, limiter) as oe:
        receipt = await oe.place_order(req)

    if receipt:
        print(f"\n  ✓ Order placed successfully")
        print(f"  {receipt.summary()}")
        print(f"  Order ID: {receipt.order_id}\n")
    else:
        print("\n  ✗ Order placement failed — check logs.\n")
        sys.exit(1)


async def cmd_status(args):
    creds, limiter = _make_deps()
    async with OrderEntry(creds, limiter) as oe:
        status = await oe.get_order_status(args.order_id)

    if not status:
        print(f"\n  Order {args.order_id} not found.\n")
        return

    print(f"\n{'─' * 65}")
    print(f"  ORDER STATUS")
    print(f"{'─' * 65}")
    print(f"  {'Order ID:':<28} {status.order_id}")
    print(f"  {'Ticker:':<28} {status.ticker}")
    print(f"  {'Side:':<28} {status.side}")
    print(f"  {'Type:':<28} {status.order_type}")
    print(f"  {'Status:':<28} {status.status.value}")
    print(f"  {'Requested / Filled:':<28} {status.count} / {status.filled_count}  ({status.fill_pct:.0f}%)")
    print(f"  {'Remaining:':<28} {status.remaining_count}")
    print(f"  {'YES price:':<28} {status.yes_price}c")
    print(f"  {'Avg fill price:':<28} {status.avg_fill_price}c")
    print(f"  {'Total cost:':<28} ${status.total_cost_cents/100:.2f}")
    print(f"  {'Created:':<28} {status.created_at}")
    print(f"  {'Updated:':<28} {status.updated_at}")
    print(f"{'─' * 65}\n")


async def cmd_orders(args):
    creds, limiter = _make_deps()
    async with OrderEntry(creds, limiter) as oe:
        orders = await oe.list_open_orders()

    if not orders:
        print("\n  No resting orders.\n")
        return

    print(f"\n{'─' * 80}")
    print(f"  OPEN ORDERS  ({len(orders)} resting)")
    print(f"{'─' * 80}")
    print(f"  {'ORDER ID':<12} {'STATUS':<12} {'TICKER':<30} {'SIDE':<5} {'QTY':>5} {'FILL%':>6} {'PRICE':>6} {'COST':>8}")
    print(f"  {'─' * 76}")
    for o in orders:
        print(
            f"  {o.order_id[:8]:<12} {o.status.value:<12} {o.ticker:<30} "
            f"{o.side:<5} {o.count:>5} {o.fill_pct:>5.0f}% "
            f"{o.yes_price or '?':>5}c ${o.total_cost_cents/100:>7.2f}"
        )
    print(f"{'─' * 80}\n")


async def cmd_cancel(args):
    creds, limiter = _make_deps()
    async with OrderEntry(creds, limiter) as oe:
        ok = await oe.cancel_order(args.order_id)
    if ok:
        print(f"\n  ✓ Order {args.order_id} cancelled.\n")
    else:
        print(f"\n  ✗ Failed to cancel order {args.order_id}.\n")


async def cmd_cancel_all(args):
    creds, limiter = _make_deps()
    async with OrderEntry(creds, limiter) as oe:
        orders = await oe.list_open_orders()
        if not orders:
            print("\n  No resting orders to cancel.\n")
            return
        if not args.yes:
            confirm = input(f"  Cancel all {len(orders)} resting orders? [y/N] ").strip().lower()
            if confirm != "y":
                print("  Cancelled by user.")
                return
        results = await asyncio.gather(*[oe.cancel_order(o.order_id) for o in orders])
        ok  = sum(1 for r in results if r)
        bad = len(results) - ok
        print(f"\n  ✓ Cancelled {ok} orders.  {f'✗ Failed: {bad}' if bad else ''}\n")


async def cmd_portfolio(args):
    creds, limiter = _make_deps()
    async with PortfolioMonitor(creds, limiter) as mon:
        snapshot = await mon.refresh()
    snapshot.print_report()


async def cmd_monitor(args):
    creds, limiter = _make_deps()
    print(f"\n  Live P&L monitor — refreshing every {args.interval}s  (Ctrl-C to stop)\n")
    async with PortfolioMonitor(creds, limiter) as mon:
        try:
            async for snapshot in mon.stream(interval_seconds=args.interval):
                # Clear screen for live feel
                print("\033[2J\033[H", end="")
                snapshot.print_report()
        except KeyboardInterrupt:
            print("\n  Monitor stopped.\n")


async def cmd_position(args):
    creds, limiter = _make_deps()
    async with PortfolioMonitor(creds, limiter) as mon:
        pos = await mon.get_position(args.ticker)
    if not pos:
        print(f"\n  No open position in {args.ticker}.\n")
        return

    print(f"\n{'─' * 65}")
    print(f"  POSITION DETAIL  —  {args.ticker}")
    print(f"{'─' * 65}")
    for k, v in pos.to_dict().items():
        print(f"  {k:<30} {v}")
    print(f"{'─' * 65}\n")


async def cmd_balance(args):
    creds, limiter = _make_deps()
    async with OrderEntry(creds, limiter) as oe:
        bal = await oe.get_balance()
    balance_cents = bal.get("balance", 0)
    print(f"\n  Account balance:  ${balance_cents/100:.2f}\n")


# ── CLI wiring ────────────────────────────────────────────────────────────────

def _add_order_args(p):
    p.add_argument("--ticker",  required=True,       help="Market ticker e.g. PRES-2024-DEM")
    p.add_argument("--side",    required=True, choices=["yes", "no"])
    p.add_argument("--count",   required=True, type=int, help="Number of contracts")
    p.add_argument("--price",   type=int,      help="Limit price in cents (1-99)")
    p.add_argument("--market",  action="store_true", help="Market order (IOC at book cap)")
    p.add_argument("--max-price", type=int, metavar="CENTS",
                   help="Max YES/NO price in cents for market orders (default: from book)")
    p.add_argument("--tif",     default="gtc", choices=["gtc","ioc","fok"], help="Time in force")
    p.add_argument("--note",    default="",    help="Optional label for this trade")


parser = argparse.ArgumentParser(
    description="Kalshi manual trade entry and portfolio monitor",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
sub = parser.add_subparsers(dest="cmd", required=True)

# preview
p_prev = sub.add_parser("preview", help="Preview an order (no submission)")
_add_order_args(p_prev)

# buy
p_buy = sub.add_parser("buy", help="Place a market or limit order")
_add_order_args(p_buy)
p_buy.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

# status
p_stat = sub.add_parser("status", help="Check order status")
p_stat.add_argument("--order-id", required=True)

# orders
sub.add_parser("orders", help="List all resting orders")

# cancel
p_can = sub.add_parser("cancel", help="Cancel one order")
p_can.add_argument("--order-id", required=True)

# cancel-all
p_ca = sub.add_parser("cancel-all", help="Cancel all resting orders")
p_ca.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

# portfolio / positions
sub.add_parser("portfolio", help="Show positions with live P&L")
sub.add_parser("positions", help="Alias for portfolio")

# monitor
p_mon = sub.add_parser("monitor", help="Continuous live P&L monitor")
p_mon.add_argument("--interval", type=float, default=30.0, help="Refresh interval (seconds)")

# position
p_pos = sub.add_parser("position", help="Detail view for one position")
p_pos.add_argument("--ticker", required=True)

# balance
sub.add_parser("balance", help="Show account balance")

_HANDLERS = {
    "preview":    cmd_preview,
    "buy":        cmd_buy,
    "status":     cmd_status,
    "orders":     cmd_orders,
    "cancel":     cmd_cancel,
    "cancel-all": cmd_cancel_all,
    "portfolio":  cmd_portfolio,
    "positions":  cmd_portfolio,
    "monitor":    cmd_monitor,
    "position":   cmd_position,
    "balance":    cmd_balance,
}

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    args = parser.parse_args()
    print(f"[trade] ENV={config.ENV}  BASE={config.BASE_URL}")
    handler = _HANDLERS.get(args.cmd)
    if handler:
        asyncio.run(handler(args))
    else:
        parser.print_help()
