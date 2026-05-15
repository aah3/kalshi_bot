"""
tools/screen.py — interactive market screener CLI

Run this before starting the bot to find the best markets to trade.

Usage:
    # Screen all Politics markets
    python tools/screen.py --category Politics

    # Screen everything, show top 30
    python tools/screen.py --all --top 30

    # Full detail on one ticker
    python tools/screen.py --ticker PRES-2024-DEM

    # Screen an entire event group (also runs arb detector)
    python tools/screen.py --event PRES-2024

    # List all available categories
    python tools/screen.py --categories

    # Output results as JSON (pipe to file or jq)
    python tools/screen.py --category Economics --json
"""

import argparse
import asyncio
import json
import sys
import os

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from credentials.credential_manager import CredentialManager
from discovery.market_client import MarketClient
from discovery.screener import MarketScreener
from execution.rate_limiter import RateLimiter


async def run(args: argparse.Namespace) -> None:
    credentials  = CredentialManager()
    rate_limiter = RateLimiter()

    async with MarketClient(credentials, rate_limiter) as client:
        screener = MarketScreener(client)

        # ── List categories ───────────────────────────────────────────────────
        if args.categories:
            cats = await client.get_categories()
            print("\nAvailable Kalshi market categories:\n")
            for i, c in enumerate(cats, 1):
                print(f"  {i:>3}.  {c}")
            print()
            return

        # ── Single ticker detail ──────────────────────────────────────────────
        if args.ticker:
            detail = await screener.get_market_detail(args.ticker)
            if args.json:
                print(json.dumps(detail, indent=2, default=str))
            else:
                _print_market_detail(detail)
            return

        # ── Event group screen ────────────────────────────────────────────────
        if args.event:
            print(f"\nScreening event: {args.event}")
            results = await screener.screen_event(args.event)
            if args.json:
                print(json.dumps([_result_to_dict(r) for r in results], indent=2, default=str))
            else:
                screener.print_report(results, top_n=args.top)
                _print_reasons(results[:args.top])
            return

        # ── Category screen ───────────────────────────────────────────────────
        if args.category:
            print(f"\nScreening category: {args.category}")
            results = await screener.screen_category(
                category=args.category,
                fetch_order_books=not args.no_books,
            )
            if args.json:
                print(json.dumps([_result_to_dict(r) for r in results], indent=2, default=str))
            else:
                screener.print_report(results, top_n=args.top)
                _print_reasons(results[:args.top])
            return

        # ── Full scan ─────────────────────────────────────────────────────────
        if args.all:
            print("\nFull market scan — this may take 10–20 seconds...")
            results = await screener.screen_all(
                fetch_order_books=not args.no_books,
            )
            if args.json:
                print(json.dumps([_result_to_dict(r) for r in results], indent=2, default=str))
            else:
                screener.print_report(results, top_n=args.top)
                _print_reasons(results[:args.top])
            return

        parser.print_help()


# ── Formatting helpers ────────────────────────────────────────────────────────

def _print_market_detail(detail: dict) -> None:
    m   = detail.get("market", {})
    ob  = detail.get("order_book", {})
    sh  = detail.get("strategy_hints", {})

    print(f"\n{'═' * 70}")
    print(f"  {m.get('title', m.get('ticker', ''))}")
    print(f"  Ticker: {m.get('ticker')}  |  Category: {m.get('category')}")
    print(f"{'═' * 70}")

    print(f"\n  STATUS & PRICING")
    print(f"  {'Status:':<22} {m.get('status')}")
    print(f"  {'Implied probability:':<22} {m.get('implied_prob', 0)*100:.1f}%")
    print(f"  {'YES bid / ask:':<22} {m.get('yes_bid')}c / {m.get('yes_ask')}c")
    print(f"  {'Spread:':<22} {m.get('spread')}c")
    print(f"  {'YES decimal odds:':<22} {m.get('yes_decimal_odds')}x")
    print(f"  {'NO  decimal odds:':<22} {m.get('no_decimal_odds')}x")
    print(f"  {'Closes in:':<22} {m.get('minutes_to_close', '?')} min")

    print(f"\n  LIQUIDITY")
    print(f"  {'Volume (24h):':<22} {m.get('volume_24h', 0):,} contracts")
    print(f"  {'Open interest:':<22} {m.get('open_interest', 0):,} contracts")

    if ob:
        print(f"\n  LIVE ORDER BOOK (top 5)")
        print(f"  {'YES Bids':<20}  {'YES Asks'}")
        print(f"  {'─'*20}  {'─'*20}")
        bids = ob.get("yes_bids", [])
        asks = ob.get("yes_asks", [])
        for i in range(max(len(bids), len(asks))):
            bid_str = f"{bids[i][0]}c × {bids[i][1]}" if i < len(bids) else ""
            ask_str = f"{asks[i][0]}c × {asks[i][1]}" if i < len(asks) else ""
            print(f"  {bid_str:<20}  {ask_str}")

    if sh:
        print(f"\n  STRATEGY HINTS")
        for strategy, hint in sh.items():
            print(f"\n  [{strategy.upper()}]")
            for k, v in hint.items():
                if isinstance(v, dict):
                    print(f"    {k}:")
                    for kk, vv in v.items():
                        print(f"      {kk:<30} {vv}")
                else:
                    print(f"    {k:<32} {v}")

    print(f"\n{'═' * 70}\n")


def _print_reasons(results) -> None:
    """Print scoring reasons for each result."""
    if not results:
        return
    print("\n  SCORING DETAIL")
    print(f"  {'─' * 90}")
    for r in results:
        print(f"\n  {r.market.ticker}  [{r.strategy_fit.value}]  score={r.score:.2f}")
        for reason in r.reasons:
            print(f"    • {reason}")
        if r.arb_group:
            print(f"    • Arb group: {', '.join(r.arb_group)}")
            print(f"    • Estimated profit: {r.arb_profit_cents}c/contract")
    print()


def _result_to_dict(r) -> dict:
    return {
        "ticker":          r.market.ticker,
        "title":           r.market.title,
        "strategy_fit":    r.strategy_fit.value,
        "score":           r.score,
        "reasons":         r.reasons,
        "market":          r.market.to_dict(),
        "arb_group":       r.arb_group,
        "arb_profit_cents": r.arb_profit_cents,
    }


# ── CLI entry ─────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="Kalshi market screener — find the best bets for each strategy",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
parser.add_argument("--category",   type=str, help="Screen one category (e.g. Politics)")
parser.add_argument("--event",      type=str, help="Screen one event group by event ticker")
parser.add_argument("--ticker",     type=str, help="Full detail for one market ticker")
parser.add_argument("--all",        action="store_true", help="Screen all open markets")
parser.add_argument("--categories", action="store_true", help="List available categories")
parser.add_argument("--top",        type=int, default=20, help="Number of results to show")
parser.add_argument("--no-books",   action="store_true",  help="Skip live order book fetch")
parser.add_argument("--json",       action="store_true",  help="Output as JSON")


if __name__ == "__main__":
    args = parser.parse_args()
    print(f"[screener] ENV={config.ENV}  BASE={config.BASE_URL}")
    asyncio.run(run(args))
