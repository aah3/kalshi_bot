"""
strategy/price_targets.py

Per-fill price targets derived from entry price (shared across strategies).

Strategies compute absolute trigger levels on entry fill so later cycles adapt
to the actual execution price rather than session-wide static thresholds.
"""

from __future__ import annotations


def hedge_trigger_price(entry_price_cents: int, hedge_offset_cents: int) -> int:
    """
    YES bid at or above this level triggers a green-up hedge.

    ``hedge_offset_cents`` is added to the entry fill price (capped at 99¢).
    """
    if entry_price_cents <= 0:
        return 0
    return min(99, entry_price_cents + hedge_offset_cents)


def stop_loss_trigger_price(entry_price_cents: int, stop_loss_cents: int) -> int:
    """
    YES bid at or below this level triggers a stop.

    Loss per contract ≈ entry_price - trigger (capped at entry - 1).
    """
    if entry_price_cents <= 0:
        return 0
    return max(1, entry_price_cents - stop_loss_cents)


def parse_hedge_offset_cents(
    value: int | str | None,
) -> int | None:
    """Parse --hedge-offset / KALSHI_GREEN_UP_HEDGE_OFFSET; ``None`` = absolute mode."""
    if value is None:
        return None
    offset = int(value)
    if offset < 1:
        raise ValueError(f"hedge-offset must be at least 1 cent, got {value!r}")
    if offset > 98:
        raise ValueError(f"hedge-offset must be at most 98 cents, got {value!r}")
    return offset
