"""
discovery/live_market.py

Filters for markets that are actively trading *right now* (not just open).

Used at discovery time (REST metadata) and before each entry order (WS book freshness).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from discovery.market_client import MarketSummary


@dataclass(frozen=True)
class LiveMarketRules:
    """
  Rules for "live" tradeable markets.

  Discovery (REST):
    - status open, tradeable spread/volume
    - updated within max_minutes_since_update
    - closes within max_minutes_to_close (event soon — Sports in-play window)

  Entry (WebSocket):
    - order book snapshot/delta within max_book_stale_minutes
    - optional: last trade on tape within max_trade_stale_minutes
    """

    enabled: bool = True
    max_minutes_since_update: float | None = 120.0
    max_minutes_to_close: float | None = 360.0
    min_minutes_to_close: float = 5.0
    max_book_stale_minutes: float = 30.0
    max_trade_stale_minutes: float | None = 120.0
    min_volume_24h: int = 0


def rules_from_activity_hours(
    activity_hours: float | None,
    *,
    enabled: bool = True,
    max_minutes_to_close: float | None = None,
) -> LiveMarketRules:
    """Build rules from discovery ``activity_hours`` (API updated_time proxy)."""
    max_since = activity_hours * 60.0 if activity_hours is not None else None
    return LiveMarketRules(
        enabled=enabled,
        max_minutes_since_update=max_since,
        max_minutes_to_close=max_minutes_to_close,
    )


def is_market_live(
    market: MarketSummary,
    rules: LiveMarketRules,
) -> tuple[bool, str]:
    """REST metadata check for discovery / periodic refresh."""
    if not rules.enabled:
        return True, "live check disabled"

    if market.status != "open":
        return False, f"status={market.status!r}"

    if rules.min_volume_24h and market.volume_24h < rules.min_volume_24h:
        return False, f"volume_24h={market.volume_24h} < {rules.min_volume_24h}"

    if rules.max_minutes_since_update is not None:
        if market.minutes_since_update is None:
            return False, "no updated_at from API"
        if market.minutes_since_update > rules.max_minutes_since_update:
            return False, (
                f"last API update {market.minutes_since_update:.0f}m ago "
                f"(max {rules.max_minutes_since_update:.0f}m)"
            )

    if market.minutes_to_close is not None:
        if market.minutes_to_close < rules.min_minutes_to_close:
            return False, f"closes in {market.minutes_to_close:.0f}m (< {rules.min_minutes_to_close:.0f}m)"
        if (
            rules.max_minutes_to_close is not None
            and market.minutes_to_close > rules.max_minutes_to_close
        ):
            return False, (
                f"closes in {market.minutes_to_close:.0f}m "
                f"(> {rules.max_minutes_to_close:.0f}m — not in live window)"
            )

    return True, "live"


def is_tick_live(
    tick: dict[str, Any],
    book: Any | None,
    rules: LiveMarketRules,
    market: MarketSummary | None = None,
) -> tuple[bool, str]:
    """
    Runtime check before placing an entry.

    Uses WebSocket book freshness; optionally re-checks cached discovery metadata.
    """
    if not rules.enabled:
        return True, "live check disabled"

    if market is not None:
        ok, reason = is_market_live(market, rules)
        if not ok:
            return False, f"metadata: {reason}"

    if book is None:
        return False, "no WebSocket order book"

    now_us = int(time.time() * 1_000_000)
    updated_us = getattr(book, "updated_at_us", 0) or 0
    if updated_us <= 0:
        return False, "order book never updated on WebSocket"

    book_age_min = (now_us - updated_us) / 60_000_000.0
    if book_age_min > rules.max_book_stale_minutes:
        return False, (
            f"WS book stale ({book_age_min:.0f}m old, max {rules.max_book_stale_minutes:.0f}m)"
        )

    if rules.max_trade_stale_minutes is not None:
        trade_us = getattr(book, "last_trade_at_us", 0) or 0
        if trade_us <= 0:
            return False, "no recent trade on WebSocket tape"
        trade_age_min = (now_us - trade_us) / 60_000_000.0
        if trade_age_min > rules.max_trade_stale_minutes:
            return False, (
                f"last trade {trade_age_min:.0f}m ago "
                f"(max {rules.max_trade_stale_minutes:.0f}m)"
            )

    return True, "live"
