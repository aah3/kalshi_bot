"""
discovery/ticker_selector.py

Resolve a ranked list of tickers from Kalshi market metadata using
category + liquidity / price / activity filters.

Used by main.py (--discover) and tools/screen.py browse flows share the
same MarketClient.fetch path via get_markets_by_category().
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from credentials.credential_manager import CredentialManager
from discovery.market_client import MarketClient, MarketSummary
from execution.rate_limiter import RateLimiter
from logging_.structured_logger import logger


@dataclass(frozen=True)
class TickerCriteria:
    """Filters for automatic ticker selection at bot startup."""

    category: str
    top_n: int = 10
    min_volume_24h: int = 0
    max_yes_ask: int | None = None
    min_yes_ask: int | None = None
    max_spread: int | None = None
    activity_hours: float | None = None
    full_scan: bool = False
    tradeable_only: bool = True
    status: str = "open"


def filter_markets(
    markets: list[MarketSummary],
    criteria: TickerCriteria,
) -> list[MarketSummary]:
    """Apply price, spread, volume, and tradeability filters (no ranking)."""
    out: list[MarketSummary] = []
    for m in markets:
        if criteria.tradeable_only and not m.is_tradeable():
            continue
        if m.volume_24h < criteria.min_volume_24h:
            continue
        if criteria.max_yes_ask is not None:
            if m.yes_ask is None or m.yes_ask > criteria.max_yes_ask:
                continue
        if criteria.min_yes_ask is not None:
            if m.yes_ask is None or m.yes_ask < criteria.min_yes_ask:
                continue
        if criteria.max_spread is not None and m.spread > criteria.max_spread:
            continue
        out.append(m)
    return out


def rank_markets(markets: list[MarketSummary]) -> list[MarketSummary]:
    """Sort by 24h volume descending (primary discovery ranking)."""
    return sorted(markets, key=lambda m: m.volume_24h, reverse=True)


def select_tickers(
    markets: list[MarketSummary],
    criteria: TickerCriteria,
) -> list[str]:
    """Filter, rank by volume, return top N tickers."""
    filtered = rank_markets(filter_markets(markets, criteria))
    return [m.ticker for m in filtered[: criteria.top_n]]


def criteria_from_env() -> TickerCriteria | None:
    """
    Build criteria from KALSHI_DISCOVER_* env vars.

    Returns None if discovery is not enabled (KALSHI_DISCOVER unset / false).
    """
    flag = os.getenv("KALSHI_DISCOVER", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None

    category = os.getenv("KALSHI_DISCOVER_CATEGORY", "").strip()
    if not category:
        return None

    def _opt_int(key: str) -> int | None:
        raw = os.getenv(key, "").strip()
        return int(raw) if raw else None

    def _opt_float(key: str) -> float | None:
        raw = os.getenv(key, "").strip()
        return float(raw) if raw else None

    full_scan = os.getenv("KALSHI_DISCOVER_FULL_SCAN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    activity = _opt_float("KALSHI_DISCOVER_ACTIVITY_HOURS")

    return TickerCriteria(
        category=category,
        top_n=int(os.getenv("KALSHI_DISCOVER_TOP", "10")),
        min_volume_24h=int(os.getenv("KALSHI_DISCOVER_MIN_VOLUME", "0")),
        max_yes_ask=_opt_int("KALSHI_DISCOVER_MAX_YES_ASK"),
        min_yes_ask=_opt_int("KALSHI_DISCOVER_MIN_YES_ASK"),
        max_spread=_opt_int("KALSHI_DISCOVER_MAX_SPREAD"),
        activity_hours=activity,
        full_scan=full_scan or activity is not None,
        tradeable_only=os.getenv("KALSHI_DISCOVER_TRADEABLE", "1").strip().lower()
        not in ("0", "false", "no", "off"),
    )


async def fetch_category_markets(
    client: MarketClient,
    criteria: TickerCriteria,
    *,
    fetch_limit: int | None = None,
) -> list[MarketSummary]:
    """
    Pull open markets for a category (activity / full_scan passed to API layer).
    """
    pool = fetch_limit if fetch_limit is not None else max(criteria.top_n * 20, 200)
    return await client.get_markets_by_category(
        category=criteria.category,
        status=criteria.status,
        limit=pool,
        min_volume_24h=criteria.min_volume_24h,
        activity_hours=criteria.activity_hours,
        full_scan=criteria.full_scan,
    )


async def discover_tickers(
    credentials: CredentialManager,
    rate_limiter: RateLimiter,
    criteria: TickerCriteria,
) -> list[str]:
    """
    Fetch markets from Kalshi, apply filters, return top N tickers by volume.
    """
    async with MarketClient(credentials, rate_limiter) as client:
        markets = await fetch_category_markets(client, criteria)
        tickers = select_tickers(markets, criteria)

    logger.info(
        "Ticker discovery complete",
        category=criteria.category,
        fetched=len(markets),
        selected=len(tickers),
        tickers=tickers,
        top_n=criteria.top_n,
        max_yes_ask=criteria.max_yes_ask,
        activity_hours=criteria.activity_hours,
    )
    return tickers


def format_discovery_table(
    markets: list[MarketSummary],
    tickers: list[str],
    criteria: TickerCriteria,
) -> str:
    """Human-readable summary for --discover-only."""
    by_ticker = {m.ticker: m for m in markets}
    lines = [
        f"Discovery: {criteria.category}  →  {len(tickers)} ticker(s)",
        f"  filters: top={criteria.top_n}  min_vol_24h={criteria.min_volume_24h}"
        + (f"  max_yes_ask={criteria.max_yes_ask}c" if criteria.max_yes_ask else "")
        + (f"  activity_hours={criteria.activity_hours}" if criteria.activity_hours else ""),
        "",
        f"  {'VOL24H':>8}  {'ASK':>4}  {'BID':>4}  {'TICKER':<40}  TITLE",
        f"  {'─' * 90}",
    ]
    for tk in tickers:
        m = by_ticker.get(tk)
        if not m:
            lines.append(f"  {'?':>8}  {'?':>4}  {'?':>4}  {tk}")
            continue
        lines.append(
            f"  {m.volume_24h:>8,}  {m.yes_ask or '?':>4}  {m.yes_bid or '?':>4}  "
            f"{m.ticker:<40}  {m.title[:42]}"
        )
    return "\n".join(lines)


async def discover_with_details(
    credentials: CredentialManager,
    rate_limiter: RateLimiter,
    criteria: TickerCriteria,
) -> tuple[list[str], list[MarketSummary]]:
    """Like discover_tickers but also returns the fetched market pool."""
    async with MarketClient(credentials, rate_limiter) as client:
        markets = await fetch_category_markets(client, criteria)
        tickers = select_tickers(markets, criteria)
    return tickers, markets
