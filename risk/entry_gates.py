"""
risk/entry_gates.py

Hard pre-trade gates applied before new entry orders (all strategies).

Uses exchange-backed cash balance and market close time from discovery metadata.
"""

from __future__ import annotations

import config
from discovery.market_client import MarketSummary


def minutes_to_close(
    ticker: str,
    market: MarketSummary | None = None,
) -> float | None:
    """Minutes until market close, or None if unknown."""
    if market is not None and market.minutes_to_close is not None:
        return float(market.minutes_to_close)
    return None


def check_entry_allowed(
    *,
    phase: str,
    ticker: str,
    cash_balance_cents: int | None,
    market: MarketSummary | None = None,
) -> tuple[bool, str]:
    """
    Return (True, "") if a new entry may proceed, else (False, reason).

    Only blocks ``entry`` and ``leg_1`` phases; hedges/exits/stops always pass.
    """
    if phase not in ("entry", "leg_1"):
        return True, ""

    if config.BLOCK_ENTRIES_ON_LOW_BALANCE and cash_balance_cents is not None:
        if cash_balance_cents < config.MIN_ACCOUNT_BALANCE_CENTS:
            return False, (
                f"cash balance ${cash_balance_cents / 100:.2f} below minimum "
                f"${config.MIN_ACCOUNT_BALANCE_CENTS / 100:.2f}"
            )

    mins = minutes_to_close(ticker, market)
    if mins is not None and mins < config.MIN_MINUTES_TO_EXPIRY:
        return False, (
            f"market closes in {mins:.0f}m "
            f"(minimum {config.MIN_MINUTES_TO_EXPIRY:.0f}m before entry)"
        )

    return True, ""
