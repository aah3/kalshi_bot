"""
tools/orderbook.py — fetch and display the live order book for one ticker

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

  # Formatted table (default depth 10)
  python tools/orderbook.py --ticker PRES-2028-DEM

  # More levels
  python tools/orderbook.py --ticker PRES-2028-DEM --depth 20

  # JSON output (pipe to jq or save)
  python tools/orderbook.py --ticker PRES-2028-DEM --json

  # Raw Kalshi API response
  python tools/orderbook.py --ticker PRES-2028-DEM --raw
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from credentials.credential_manager import CredentialManager
from discovery.market_client import MarketClient
from discovery.orderbook_parse import OrderBookSnapshot
from execution.rate_limiter import RateLimiter


def _format_level(price: int, qty: int) -> str:
    return f"{price}c × {qty:,}"


def _print_order_book(
    book: OrderBookSnapshot,
    *,
    title: str | None = None,
    depth: int,
) -> None:
    print(f"\n{'═' * 72}")
    if title:
        print(f"  {title}")
    print(f"  Ticker: {book.ticker}")
    print(f"  Fetched: {book.fetched_at.isoformat()}")
    print(f"{'═' * 72}")

    bid = book.best_bid
    ask = book.best_ask
    spread = book.spread
    mid = book.mid_price

    print(f"\n  SUMMARY")
    print(f"  {'Best bid / ask:':<24} {f'{bid}c / {ask}c' if bid is not None and ask is not None else '—'}")
    print(f"  {'Spread:':<24} {f'{spread}c' if spread is not None else '—'}")
    if mid is not None:
        print(f"  {'Mid (implied %):':<24} {mid:.1f}c ({mid:.1f}%)")

    if not book.yes_bids and not book.yes_asks:
        print("\n  Order book is empty (no resting liquidity).\n")
        return

    levels = min(depth, max(len(book.yes_bids), len(book.yes_asks)))
    print(f"\n  ORDER BOOK  (top {levels} levels)")
    print(f"  {'YES Bids (buy YES)':^25}  {'YES Asks (sell YES)':^25}")
    print(f"  {'price × qty':^25}  {'price × qty':^25}")
    print(f"  {'─' * 54}")
    for i in range(levels):
        bid_s = _format_level(*book.yes_bids[i]) if i < len(book.yes_bids) else ""
        ask_s = _format_level(*book.yes_asks[i]) if i < len(book.yes_asks) else ""
        print(f"  {bid_s:<25}  {ask_s:<25}")
    print()


def _book_to_dict(book: OrderBookSnapshot, *, depth: int) -> dict:
    return {
        "ticker": book.ticker,
        "fetched_at": book.fetched_at.isoformat(),
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "spread": book.spread,
        "mid_price": book.mid_price,
        "yes_bids": book.yes_bids[:depth],
        "yes_asks": book.yes_asks[:depth],
    }


async def cmd_orderbook(args) -> None:
    creds = CredentialManager()
    limiter = RateLimiter()

    async with MarketClient(creds, limiter) as client:
        if args.raw:
            raw = await client._get(
                f"/markets/{args.ticker}/orderbook",
                params={"depth": args.depth},
            )
            print(json.dumps(raw, indent=2))
            return

        market = await client.get_market(args.ticker)
        book = await client.get_order_book(args.ticker, depth=args.depth)

        if market is None:
            print(f"\n  Market not found: {args.ticker}\n")
            return

        if book is None:
            print(f"\n  {args.ticker}")
            if market.title:
                print(f"  {market.title}")
            print("\n  Order book is empty (no resting liquidity).\n")
            return

        if args.json:
            payload = {
                "market": {
                    "ticker": market.ticker,
                    "title": market.title,
                    "status": market.status,
                },
                "order_book": _book_to_dict(book, depth=args.depth),
            }
            print(json.dumps(payload, indent=2, default=str))
            return

        _print_order_book(book, title=market.title, depth=args.depth)


parser = argparse.ArgumentParser(
    description="Fetch and display the Kalshi order book for one ticker",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
parser.add_argument("--ticker", required=True, help="Market ticker")
parser.add_argument(
    "--depth",
    type=int,
    default=10,
    help="Number of price levels per side (default: 10, max: 200)",
)
parser.add_argument("--json", action="store_true", help="Output parsed book as JSON")
parser.add_argument("--raw", action="store_true", help="Output raw Kalshi API JSON")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    args = parser.parse_args()
    if args.depth < 1 or args.depth > 200:
        parser.error("--depth must be between 1 and 200")
    print(f"[orderbook] ENV={config.ENV}  BASE={config.BASE_URL}")
    asyncio.run(cmd_orderbook(args))
