"""
strategy/price_targets.py

Per-fill price targets derived from entry price (shared across strategies).

Strategies compute absolute trigger levels on entry fill so later cycles adapt
to the actual execution price rather than session-wide static thresholds.
"""

from __future__ import annotations

import config

# Cap YES hedge trigger below $1.00 so buy-NO has room to execute and books
# stay two-sided (avoids 99¢/1¢ one-sided markets at resolution).
DEFAULT_HEDGE_TRIGGER_CAP_CENTS = 95


def hedge_trigger_cap_cents() -> int:
    return int(getattr(config, "HEDGE_TRIGGER_CAP_CENTS", DEFAULT_HEDGE_TRIGGER_CAP_CENTS))


def hedge_trigger_price(
    entry_price_cents: int,
    hedge_offset_cents: int,
    *,
    cap_cents: int | None = None,
) -> int:
    """
    YES bid at or above this level triggers a green-up hedge.

    ``hedge_offset_cents`` is added to the entry fill price, capped at
    ``cap_cents`` (default 95¢) so the complement NO leg stays executable.
    """
    if entry_price_cents <= 0:
        return 0
    cap = cap_cents if cap_cents is not None else hedge_trigger_cap_cents()
    return min(cap, entry_price_cents + hedge_offset_cents)


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
