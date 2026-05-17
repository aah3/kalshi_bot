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
from typing import Literal

from credentials.credential_manager import CredentialManager
from discovery.live_market import LiveMarketRules, is_market_live
from discovery.market_client import MarketClient, MarketSummary
from discovery.market_math import fee_adjusted_roi_if_yes_wins_pct
from execution.rate_limiter import RateLimiter
from logging_.structured_logger import logger

RankBy = Literal["volume", "fee_adjusted_roi", "screener"]

# Kalshi UI "Trending" tab — not an API category; cross-category volume scan.
DEFAULT_DISCOVER_CATEGORY = "Trending"


def resolve_discover_category(category: str | None) -> str:
    """Use Trending when CLI/env omit a category (matches Kalshi home default)."""
    name = (category or "").strip()
    return name or DEFAULT_DISCOVER_CATEGORY


@dataclass(frozen=True)
class TickerCriteria:
    """Filters for automatic ticker selection at bot startup."""

    category: str
    top_n: int = 10
    min_volume_24h: int = 0
    max_yes_ask: int | None = None
    min_yes_ask: int | None = None
    max_spread: int | None = None
    min_fee_adjusted_roi_pct: float | None = None
    rank_by: RankBy = "volume"
    activity_hours: float | None = None
    full_scan: bool = False
    tradeable_only: bool = True
    status: str = "open"
    preset_name: str | None = None
    screener_strategy: str | None = None
    live_only: bool = True
    max_minutes_to_close: float | None = None
    max_minutes_since_update: float | None = None


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
        if criteria.min_fee_adjusted_roi_pct is not None:
            if m.yes_ask is None:
                continue
            roi = fee_adjusted_roi_if_yes_wins_pct(
                m.yes_ask,
                round_trip_fees=False,
            )
            if roi < criteria.min_fee_adjusted_roi_pct:
                continue
        if criteria.live_only:
            max_since = criteria.max_minutes_since_update
            if max_since is None and criteria.activity_hours is not None:
                max_since = criteria.activity_hours * 60.0
            rules = LiveMarketRules(
                enabled=True,
                max_minutes_since_update=max_since,
                max_minutes_to_close=criteria.max_minutes_to_close,
                min_volume_24h=criteria.min_volume_24h,
            )
            ok, _ = is_market_live(m, rules)
            if not ok:
                continue
        out.append(m)
    return out


def _market_sort_key(market: MarketSummary, rank_by: RankBy) -> float:
    if rank_by == "fee_adjusted_roi" and market.yes_ask is not None:
        return fee_adjusted_roi_if_yes_wins_pct(market.yes_ask, round_trip_fees=False)
    return float(market.volume_24h)


def rank_markets(
    markets: list[MarketSummary],
    *,
    rank_by: RankBy = "volume",
    screener_strategy: str | None = None,
) -> list[MarketSummary]:
    """Sort markets for discovery (volume, fee-adjusted ROI, or screener score)."""
    if rank_by == "screener":
        from discovery.screener import score_for_strategy

        strat = screener_strategy or "green_up"
        return sorted(
            markets,
            key=lambda m: score_for_strategy(m, strat),
            reverse=True,
        )
    return sorted(
        markets,
        key=lambda m: _market_sort_key(m, rank_by),
        reverse=True,
    )


def select_tickers(
    markets: list[MarketSummary],
    criteria: TickerCriteria,
) -> list[str]:
    """Filter, rank by volume, return top N tickers."""
    filtered = rank_markets(
        filter_markets(markets, criteria),
        rank_by=criteria.rank_by,
        screener_strategy=criteria.screener_strategy or criteria.preset_name,
    )
    return [m.ticker for m in filtered[: criteria.top_n]]


def criteria_from_env() -> TickerCriteria | None:
    """
    Build criteria from KALSHI_DISCOVER_* env vars.

    Returns None if discovery is not enabled (KALSHI_DISCOVER unset / false).
    """
    flag = os.getenv("KALSHI_DISCOVER", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None

    category = resolve_discover_category(os.getenv("KALSHI_DISCOVER_CATEGORY"))

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
        min_yes_ask=criteria.min_yes_ask,
        rank_by=criteria.rank_by,
        preset=criteria.preset_name,
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
    preset_line = f"  preset={criteria.preset_name}  " if criteria.preset_name else ""
    lines = [
        f"Discovery: {criteria.category}  →  {len(tickers)} ticker(s)",
        f"  filters: top={criteria.top_n}  min_vol_24h={criteria.min_volume_24h}"
        + (f"  min_yes_ask={criteria.min_yes_ask}c" if criteria.min_yes_ask else "")
        + (f"  max_yes_ask={criteria.max_yes_ask}c" if criteria.max_yes_ask else "")
        + (
            f"  min_fee_roi={criteria.min_fee_adjusted_roi_pct}%"
            if criteria.min_fee_adjusted_roi_pct is not None
            else ""
        )
        + f"  rank_by={criteria.rank_by}"
        + (
            f"  screener={criteria.screener_strategy}"
            if criteria.rank_by == "screener" and criteria.screener_strategy
            else ""
        )
        + preset_line
        + (f"  activity_hours={criteria.activity_hours}" if criteria.activity_hours else ""),
        "",
        (
            f"  {'VOL24H':>8}  {'SCORE':>5}  {'ASK':>4}  {'ROI%':>6}  "
            f"{'TICKER':<40}  TITLE"
            if criteria.rank_by == "screener"
            else f"  {'VOL24H':>8}  {'ASK':>4}  {'ROI%':>6}  {'TICKER':<40}  TITLE"
        ),
        f"  {'─' * 95}",
    ]
    strat = criteria.screener_strategy or criteria.preset_name or "green_up"
    if criteria.rank_by == "screener":
        from discovery.screener import score_for_strategy
    for tk in tickers:
        m = by_ticker.get(tk)
        if not m:
            lines.append(f"  {'?':>8}  {'?':>4}  {'?':>6}  {tk}")
            continue
        roi = (
            f"{fee_adjusted_roi_if_yes_wins_pct(m.yes_ask):.1f}"
            if m.yes_ask is not None
            else "?"
        )
        if criteria.rank_by == "screener":
            sc = score_for_strategy(m, strat)
            lines.append(
                f"  {m.volume_24h:>8,}  {sc:>5.2f}  {m.yes_ask or '?':>4}  {roi:>6}  "
                f"{m.ticker:<40}  {m.title[:42]}"
            )
        else:
            lines.append(
                f"  {m.volume_24h:>8,}  {m.yes_ask or '?':>4}  {roi:>6}  "
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
