"""
discovery/market_math.py

Shared ROI helpers for high-probability strategy, discovery filters, and screener.
"""

from __future__ import annotations

import math

import config


def gross_roi_if_yes_wins_pct(yes_ask_cents: int) -> float:
    """ROI if YES resolves, ignoring fees: (100 - ask) / ask × 100."""
    if yes_ask_cents <= 0 or yes_ask_cents >= 100:
        return 0.0
    return (100 - yes_ask_cents) / yes_ask_cents * 100.0


def fee_adjusted_roi_if_yes_wins_pct(
    yes_ask_cents: int,
    *,
    fee_per_contract_cents: float | None = None,
    round_trip_fees: bool = False,
) -> float:
    """
    Net ROI if YES wins after Kalshi entry (and optional exit) fees.

    Hold-to-settlement (default):
        net_profit = (100 - ask) - entry_fee
        cost       = ask + entry_fee

    Round-trip (entry + exit limit before settlement):
        net_profit = (100 - ask) - entry_fee - exit_fee
        cost       = ask + entry_fee
    """
    if yes_ask_cents <= 0 or yes_ask_cents >= 100:
        return 0.0

    fee = (
        fee_per_contract_cents
        if fee_per_contract_cents is not None
        else config.FEE_PER_CONTRACT_CENTS
    )
    exit_fee = fee if round_trip_fees else 0.0
    net_profit = (100 - yes_ask_cents) - fee - exit_fee
    cost = yes_ask_cents + fee
    if cost <= 0:
        return 0.0
    return net_profit / cost * 100.0


def effective_min_roi_pct(
    min_roi_pct: float,
    *,
    use_fee_adjusted: bool | None = None,
) -> float:
    """Return the ROI threshold the strategy should apply at runtime."""
    if use_fee_adjusted is None:
        use_fee_adjusted = config.HP_USE_FEE_ADJUSTED_ROI
    return min_roi_pct


def passes_roi_gate(
    yes_ask_cents: int,
    min_roi_pct: float,
    *,
    use_fee_adjusted: bool | None = None,
    round_trip_fees: bool | None = None,
) -> tuple[bool, float, float]:
    """
    Check gross and fee-adjusted ROI against min_roi_pct.

    Returns:
        (passed, gross_roi_pct, applied_roi_pct)
    """
    gross = gross_roi_if_yes_wins_pct(yes_ask_cents)
    use_fees = (
        getattr(config, "HP_USE_FEE_ADJUSTED_ROI", True)
        if use_fee_adjusted is None
        else use_fee_adjusted
    )
    round_trip = (
        getattr(config, "HP_ASSUME_ROUND_TRIP_FEES", False)
        if round_trip_fees is None
        else round_trip_fees
    )
    applied = (
        fee_adjusted_roi_if_yes_wins_pct(
            yes_ask_cents, round_trip_fees=round_trip
        )
        if use_fees
        else gross
    )
    if min_roi_pct <= 0:
        return True, gross, applied
    return applied >= min_roi_pct, gross, applied


def round_trip_fees_for_post_fill(post_fill_mode: str) -> bool:
    """Whether to count an exit fee when gating entries."""
    return post_fill_mode not in ("hold", "")


def vig_proxy_cents(spread_cents: int | None) -> int:
    """
    Half-spread vig proxy in cents (matches Kelly / high-prob edge-to-vig).

    Uses max(spread/2, 0.5) so even a 1¢ spread contributes 0.5¢ vig.
    """
    spread = spread_cents if spread_cents is not None else 2
    return int(math.ceil(max(spread / 2.0, 0.5)))


def take_profit_price_cents(
    entry_cents: int,
    vig_cents: int,
    profit_pct: float,
    *,
    price_cap: int = 99,
) -> int:
    """
    Resting take-profit YES price: entry + profit_pct × (entry + vig).

    ``profit_pct`` is a fraction (0.30 = 30% gain on entry+vig basis).
    Values > 1 are treated as a whole-number percent (30 → 0.30).
    """
    if profit_pct > 1.0:
        profit_pct = profit_pct / 100.0
    if profit_pct < 0:
        profit_pct = 0.0
    basis = entry_cents + vig_cents
    gain = int(round(basis * profit_pct))
    return min(price_cap, max(1, entry_cents + max(gain, 0)))
