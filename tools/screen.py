"""
tools/screen.py — market browser and strategy screener

Two modes:

  BROWSE  — shows every available market in a human-readable table with
             probability, cost to enter, payout, and ROI. This is what you
             use to find tickers before trading.

  SCREEN  — scores markets for strategy fit (Kelly, Green Up, Arbitrage)
             and ranks them. Use after browse to narrow down candidates.

────────────────────────────────────────────────────────────────────────────────
BROWSE COMMANDS  (find tickers and understand market economics)
────────────────────────────────────────────────────────────────────────────────

  # Step 1: see all available categories
  python tools/screen.py --categories

  # Step 2: browse all markets in a category
  python tools/screen.py browse --category Politics

  # Browse with minimum 24h volume filter
  python tools/screen.py browse --category Economics --min-volume 500

  # Recently active markets (updated in last 2h) + full scan so nothing hot is missed
  python tools/screen.py browse --category Politics --activity-hours 2 --full-scan

  # Browse ALL open markets across all categories
  python tools/screen.py browse --all

  # Browse a specific event (e.g. all markets in one election)
  python tools/screen.py browse --event PRES-2028

  # Full detail on one ticker (live order book + strategy hints)
  python tools/screen.py browse --ticker PRES-2028-DEM

  # Export browse results to CSV
  python tools/screen.py browse --category Politics --csv markets.csv

  # Output as JSON (pipe to jq or save)
  python tools/screen.py browse --category Politics --json

────────────────────────────────────────────────────────────────────────────────
SCREEN COMMANDS  (score markets for your strategy)
────────────────────────────────────────────────────────────────────────────────

  # Screen a category and score for all strategies
  python tools/screen.py screen --category Politics

  # Screen all markets, show top 30
  python tools/screen.py screen --all --top 30

  # Screen an event group (also detects exhaustive-set arb)
  python tools/screen.py screen --event PRES-2028

  # Export screener results as JSON
  python tools/screen.py screen --category Economics --json

────────────────────────────────────────────────────────────────────────────────
READING THE BROWSE TABLE
────────────────────────────────────────────────────────────────────────────────

  PROB%    Implied probability (market's view of YES winning)
  BID/ASK  YES bid and ask in cents (e.g. 32c bid / 34c ask)
  YES COST Dollar cost to buy 1 YES contract (you pay ask price)
  NO COST  Dollar cost to buy 1 NO contract (you pay 100-bid)
  PAYOUT   Always $1.00 — what you collect if your side wins
  YES ROI  Return if YES wins: (1.00 - cost) / cost × 100%
  NO ROI   Return if NO wins:  (1.00 - cost) / cost × 100%
  VOL 24H  Number of contracts traded in last 24 hours
  EXP      Minutes until market closes

  Example row:
    62.5%   32/34   $0.34   $0.67   $1.00   194%    49%   1,204  PRES-2028-DEM
    → Market implies 62.5% chance YES wins
    → Buy YES for $0.34, collect $1.00 if right → $0.66 profit (194% ROI)
    → Buy NO  for $0.67, collect $1.00 if right → $0.33 profit  (49% ROI)
"""

import argparse
import asyncio
import csv
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from credentials.credential_manager import CredentialManager
from discovery.market_client import MarketClient, MarketSummary
from discovery.screener import MarketScreener
from execution.rate_limiter import RateLimiter


# ── Browse table renderer ─────────────────────────────────────────────────────

def _print_browse_table(markets: list[MarketSummary], title: str = "") -> None:
    """
    Print a trader-focused table showing probability, cost, payout, and ROI
    for every market. Sorted by 24h volume descending.
    """
    if not markets:
        print("\n  No markets found.\n")
        return

    w = 140
    print(f"\n{'═' * w}")
    if title:
        print(f"  {title}")
    print(f"  {len(markets)} markets  |  All payouts = $1.00 per contract\n")

    print(
        f"  {'PROB%':>6}  "
        f"{'BID':>4}{'ASK':>4}  "
        f"{'YES COST':>9}  "
        f"{'NO COST':>8}  "
        f"{'PAYOUT':>7}  "
        f"{'YES ROI':>8}  "
        f"{'NO ROI':>7}  "
        f"{'VOL 24H':>8}  "
        f"{'EXP':>6}  "
        f"{'TICKER':<35}  "
        f"MARKET NAME"
    )
    print(f"  {'─' * (w - 2)}")

    for m in sorted(markets, key=lambda x: x.volume_24h, reverse=True):
        d = m.to_dict()

        prob_str    = f"{d['implied_prob_pct']:.1f}%"
        bid_ask_str = f"{m.yes_bid or '?':>3}c/{m.yes_ask or '?':>3}c"
        yes_cost    = f"${d['yes_cost_usd']:.2f}" if d['yes_cost_usd'] else "  —  "
        no_cost     = f"${d['no_cost_usd']:.2f}"  if d['no_cost_usd']  else "  —  "
        yes_roi     = f"{d['yes_roi_pct']:+.0f}%"  if d['yes_roi_pct'] is not None else "—"
        no_roi      = f"{d['no_roi_pct']:+.0f}%"   if d['no_roi_pct']  is not None else "—"
        vol_str     = f"{m.volume_24h:>8,}"
        exp_str     = f"{m.minutes_to_close:.0f}m" if m.minutes_to_close is not None else "open"

        # Colour-code probability (green=likely YES, red=unlikely, white=near 50/50)
        if m.implied_prob >= 0.70:
            prob_str = f"\033[92m{prob_str}\033[0m"   # green — heavy favourite
        elif m.implied_prob <= 0.30:
            prob_str = f"\033[91m{prob_str}\033[0m"   # red — underdog
        else:
            prob_str = f"\033[97m{prob_str}\033[0m"   # white — contested

        # Flag expiry urgency
        if m.minutes_to_close is not None and m.minutes_to_close <= 60:
            exp_str = f"\033[93m{exp_str}\033[0m"
        if m.minutes_to_close is not None and m.minutes_to_close <= 10:
            exp_str = f"\033[91m{exp_str}\033[0m"

        tradeable_flag = "" if m.is_tradeable() else " ⚠"

        print(
            f"  {prob_str:>14}  "       # wider because of ANSI codes
            f"{bid_ask_str:>9}  "
            f"{yes_cost:>9}  "
            f"{no_cost:>8}  "
            f"{'$1.00':>7}  "
            f"{yes_roi:>8}  "
            f"{no_roi:>7}  "
            f"{vol_str}  "
            f"{exp_str:>10}  "
            f"{m.ticker:<35}  "
            f"{m.title[:45]}{tradeable_flag}"
        )

    print(f"\n  {'─' * (w - 2)}")
    print(f"  ⚠ = not tradeable (illiquid, closed, or expiring <5 min)")
    print(f"  PROB% = market's implied probability that YES resolves")
    print(f"  YES/NO COST = price to buy 1 contract  |  PAYOUT = $1.00 if you win")
    print(f"  ROI = (payout - cost) / cost × 100\n")
    print(f"{'═' * w}\n")


def _print_ticker_detail(detail: dict) -> None:
    """Full detail view for a single ticker."""
    m   = detail.get("market", {})
    ob  = detail.get("order_book", {})
    sh  = detail.get("strategy_hints", {})

    print(f"\n{'═' * 72}")
    print(f"  {m.get('title', m.get('ticker', ''))}")
    print(f"  Ticker: {m.get('ticker')}  |  Event: {m.get('event_ticker')}  |  Category: {m.get('category')}")
    print(f"{'═' * 72}")

    # Probability and pricing
    print(f"\n  PROBABILITY & PRICING")
    print(f"  {'Status:':<32} {m.get('status')}")
    print(f"  {'Implied prob (YES wins):':<32} {m.get('implied_prob_pct', 0):.1f}%")
    print(f"  {'YES bid / ask:':<32} {m.get('yes_bid')}c / {m.get('yes_ask')}c")
    print(f"  {'NO  bid / ask:':<32} {m.get('no_bid')}c / {m.get('no_ask')}c")
    print(f"  {'Spread:':<32} {m.get('spread')}c")
    print(f"  {'Last trade price:':<32} {m.get('last_price')}c")

    # Payout economics (the key trader view)
    print(f"\n  PAYOUT ECONOMICS  (per 1 contract)")
    print(f"  {'─' * 60}")
    yes_cost = m.get('yes_cost_usd')
    no_cost  = m.get('no_cost_usd')
    yes_prof = m.get('yes_profit_if_win_usd')
    no_prof  = m.get('no_profit_if_win_usd')
    yes_roi  = m.get('yes_roi_pct')
    no_roi   = m.get('no_roi_pct')
    yes_odds = m.get('yes_decimal_odds')
    no_odds  = m.get('no_decimal_odds')

    print(f"  {'':5}  {'YES side':>20}  {'NO side':>20}")
    print(f"  {'Cost to enter:':<20}  ${yes_cost:>18.2f}  ${no_cost:>18.2f}" if yes_cost and no_cost else "")
    print(f"  {'Payout if you win:':<20}  {'$1.00':>19}  {'$1.00':>19}")
    print(f"  {'Profit if you win:':<20}  ${yes_prof:>18.2f}  ${no_prof:>18.2f}" if yes_prof and no_prof else "")
    print(f"  {'ROI if you win:':<20}  {yes_roi:>18.1f}%  {no_roi:>18.1f}%" if yes_roi and no_roi else "")
    print(f"  {'Decimal odds:':<20}  {yes_odds:>19.3f}x  {no_odds:>19.3f}x" if yes_odds and no_odds else "")
    print(f"  {'─' * 60}")
    print(f"  Payout is always $1.00 per contract — Kalshi binary markets.")
    print(f"  You pay the ask price. If your side wins you collect $1.00.")

    # Liquidity
    print(f"\n  LIQUIDITY")
    print(f"  {'Volume (24h):':<32} {m.get('volume_24h', 0):,} contracts")
    print(f"  {'Open interest:':<32} {m.get('open_interest', 0):,} contracts")
    print(f"  {'Tradeable:':<32} {m.get('is_tradeable')}")
    print(f"  {'Closes in:':<32} {m.get('minutes_to_close', '?')} min")

    # Live order book
    if ob:
        print(f"\n  LIVE ORDER BOOK  (top 5 levels)")
        print(f"  {'YES Bids (buy YES)':^25}  {'YES Asks (sell YES)':^25}")
        print(f"  {'price × qty':^25}  {'price × qty':^25}")
        print(f"  {'─' * 54}")
        bids = ob.get("yes_bids", [])
        asks = ob.get("yes_asks", [])
        for i in range(min(5, max(len(bids), len(asks)))):
            bid_s = f"{bids[i][0]}c × {bids[i][1]:,}" if i < len(bids) else ""
            ask_s = f"{asks[i][0]}c × {asks[i][1]:,}" if i < len(asks) else ""
            print(f"  {bid_s:<25}  {ask_s:<25}")

    # Strategy hints
    if sh:
        print(f"\n  STRATEGY HINTS")
        for strategy, hint in sh.items():
            print(f"\n  [{strategy.upper()}]")
            for k, v in hint.items():
                if isinstance(v, dict):
                    print(f"    {k}:")
                    for kk, vv in v.items():
                        print(f"      {kk:<34} {vv}")
                else:
                    print(f"    {k:<36} {v}")

    print(f"\n{'═' * 72}\n")


def _browse_to_csv(markets: list[MarketSummary], filepath: str) -> None:
    """Export browse results to a CSV file."""
    rows = [m.to_dict() for m in markets]
    if not rows:
        return
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Exported {len(rows)} markets → {filepath}\n")


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_categories(args) -> None:
    """List all available market categories."""
    creds, limiter = CredentialManager(), RateLimiter()
    async with MarketClient(creds, limiter) as client:
        cats = await client.get_categories()

    print(f"\n  {'#':>3}  CATEGORY")
    print(f"  {'─' * 30}")
    for i, c in enumerate(cats, 1):
        print(f"  {i:>3}.  {c}")
    print(f"\n  Total: {len(cats)} categories")
    print(f"\n  Usage: python tools/screen.py browse --category <NAME>\n")


async def cmd_browse(args) -> None:
    """Browse markets — shows full payout economics for each market."""
    creds, limiter = CredentialManager(), RateLimiter()
    min_vol = getattr(args, "min_volume", 0) or 0
    activity_hours = getattr(args, "activity_hours", None)
    full_scan = getattr(args, "full_scan", False) or activity_hours is not None

    async with MarketClient(creds, limiter) as client:

        # ── Single ticker detail ──────────────────────────────────────────────
        if args.ticker:
            screener = MarketScreener(client)
            detail   = await screener.get_market_detail(args.ticker)
            if args.json:
                print(json.dumps(detail, indent=2, default=str))
            else:
                _print_ticker_detail(detail)
            return

        # ── Event group ───────────────────────────────────────────────────────
        if args.event:
            markets = await client.get_event_markets(args.event)
            title   = f"Event: {args.event}  ({len(markets)} markets)"
            markets = [m for m in markets if m.volume_24h >= min_vol]

        # ── Category ──────────────────────────────────────────────────────────
        elif args.category:
            markets = await client.get_markets_by_category(
                category=args.category,
                status="open",
                limit=200,
                min_volume_24h=min_vol,
                activity_hours=activity_hours,
                full_scan=full_scan,
            )
            title = f"Category: {args.category}"
            if activity_hours:
                title += f"  (active ≤{activity_hours}h)"
            if full_scan:
                title += "  [full scan]"

        # ── All open markets ──────────────────────────────────────────────────
        elif args.all:
            markets = await client.get_all_open_markets(
                limit=500,
                min_volume_24h=max(min_vol, 10),
            )
            title = "All open markets"

        else:
            print("\n  Specify --category, --event, --ticker, or --all\n")
            print("  Examples:")
            print("    python tools/screen.py browse --category Politics")
            print("    python tools/screen.py browse --event PRES-2028")
            print("    python tools/screen.py browse --ticker PRES-2028-DEM")
            print("    python tools/screen.py browse --all\n")
            return

    if args.json:
        print(json.dumps([m.to_dict() for m in markets], indent=2, default=str))
    elif args.csv:
        _browse_to_csv(markets, args.csv)
        _print_browse_table(markets, title)
    else:
        _print_browse_table(markets, title)

    # Print quick copy-paste for KALSHI_TICKERS
    tradeable = [m.ticker for m in markets if m.is_tradeable()]
    if tradeable and not args.json:
        top = tradeable[:10]
        print(f"  Quick copy — top {len(top)} tradeable tickers for KALSHI_TICKERS:")
        print(f"  export KALSHI_TICKERS=\"{','.join(top)}\"\n")


async def cmd_screen(args) -> None:
    """Score markets for strategy fit."""
    creds, limiter = CredentialManager(), RateLimiter()

    async with MarketClient(creds, limiter) as client:
        screener = MarketScreener(client)

        if args.ticker:
            detail = await screener.get_market_detail(args.ticker)
            if args.json:
                print(json.dumps(detail, indent=2, default=str))
            else:
                _print_ticker_detail(detail)
            return

        if args.event:
            results = await screener.screen_event(args.event)
        elif args.category:
            results = await screener.screen_category(
                category=args.category,
                fetch_order_books=not args.no_books,
            )
        elif args.all:
            results = await screener.screen_all(fetch_order_books=not args.no_books)
        else:
            print("\n  Specify --category, --event, --ticker, or --all\n")
            return

    if args.json:
        out = [
            {
                "ticker":       r.market.ticker,
                "title":        r.market.title,
                "strategy_fit": r.strategy_fit.value,
                "score":        r.score,
                "reasons":      r.reasons,
                "market":       r.market.to_dict(),
            }
            for r in results
        ]
        print(json.dumps(out, indent=2, default=str))
    else:
        screener.print_report(results, top_n=args.top)
        _print_score_reasons(results[:args.top])

        tradeable = [r.market.ticker for r in results if r.score >= 0.5]
        if tradeable:
            top = tradeable[:10]
            print(f"  Highest-scoring tickers (score ≥ 0.5) for KALSHI_TICKERS:")
            print(f"  export KALSHI_TICKERS=\"{','.join(top)}\"\n")


def _print_score_reasons(results) -> None:
    if not results:
        return
    print("  SCORING DETAIL")
    print(f"  {'─' * 90}")
    for r in results:
        print(f"\n  {r.market.ticker}  [{r.strategy_fit.value}]  score={r.score:.2f}")
        for reason in r.reasons:
            print(f"    • {reason}")
        if r.arb_group:
            print(f"    • Arb group: {', '.join(r.arb_group)}")
            print(f"    • Est. profit: {r.arb_profit_cents}c/contract")
    print()


# ── CLI wiring ────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="Kalshi market browser and strategy screener",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)

sub = parser.add_subparsers(dest="cmd")

# ── browse subcommand ─────────────────────────────────────────────────────────
p_browse = sub.add_parser(
    "browse",
    help="Browse markets — see tickers, probabilities, costs, and payouts",
)
p_browse.add_argument("--category",   type=str, help="Category to browse (e.g. Politics)")
p_browse.add_argument("--event",      type=str, help="Event ticker to browse (e.g. PRES-2028)")
p_browse.add_argument("--ticker",     type=str, help="Single market full detail")
p_browse.add_argument("--all",        action="store_true", help="Browse all open markets")
p_browse.add_argument("--min-volume", type=int, default=0,
                      help="Minimum 24h volume filter (default: 0)")
p_browse.add_argument("--activity-hours", type=float, default=None, metavar="HOURS",
                      help="Only markets updated within HOURS (recent-activity proxy)")
p_browse.add_argument("--full-scan", action="store_true",
                      help="Scan all open events before ranking (slower, fewer misses)")
p_browse.add_argument("--json",       action="store_true", help="Output as JSON")
p_browse.add_argument("--csv",        type=str, default=None, metavar="FILE.csv",
                      help="Export to CSV file")

# ── screen subcommand ─────────────────────────────────────────────────────────
p_screen = sub.add_parser(
    "screen",
    help="Score and rank markets for strategy fit (Kelly, Green Up, Arb)",
)
p_screen.add_argument("--category",  type=str)
p_screen.add_argument("--event",     type=str)
p_screen.add_argument("--ticker",    type=str)
p_screen.add_argument("--all",       action="store_true")
p_screen.add_argument("--top",       type=int, default=20)
p_screen.add_argument("--no-books",  action="store_true")
p_screen.add_argument("--json",      action="store_true")

# ── --categories flat flag (backward compatible) ──────────────────────────────
parser.add_argument("--categories", action="store_true",
                    help="List all available market categories")

# ── legacy flat flags (keep working for existing scripts) ────────────────────
parser.add_argument("--category",  type=str,  help=argparse.SUPPRESS)
parser.add_argument("--event",     type=str,  help=argparse.SUPPRESS)
parser.add_argument("--ticker",    type=str,  help=argparse.SUPPRESS)
parser.add_argument("--all",       action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--top",       type=int,  default=20, help=argparse.SUPPRESS)
parser.add_argument("--no-books",  action="store_true",  help=argparse.SUPPRESS)
parser.add_argument("--json",      action="store_true",  help=argparse.SUPPRESS)


async def _dispatch(args) -> None:
    if args.cmd == "browse":
        await cmd_browse(args)
    elif args.cmd == "screen":
        await cmd_screen(args)
    elif args.categories or (not args.cmd and getattr(args, "categories", False)):
        await cmd_categories(args)
    elif args.cmd is None:
        # Legacy flat-flag mode — route to screen for backward compatibility
        if args.category or args.event or args.ticker or args.all:
            await cmd_screen(args)
        else:
            parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    args = parser.parse_args()
    print(f"[screen] ENV={config.ENV}  BASE={config.BASE_URL}\n")
    asyncio.run(_dispatch(args))
