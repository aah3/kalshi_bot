"""
strategy/execution_price.py

Order pricing modes for Kalshi YES/NO legs.

Default (passive):  buy at bid, sell at ask  — rest on the book.
cross_spread:       buy at ask, sell at bid  — cross the spread (aggressive limit).
market:             IOC market at the touch.
"""

from __future__ import annotations

from enum import Enum


class EntryPriceMode(str, Enum):
    """How to price limit/market orders relative to the current book."""

    PASSIVE      = "passive"        # buy @ bid, sell @ ask (default)
    CROSS_SPREAD = "cross_spread"   # buy @ ask, sell @ bid
    MARKET       = "market"
    LIMIT_AT_ASK = "limit_at_ask"   # alias for cross on buys / passive on sells
    LIMIT_AT_BID = "limit_at_bid"   # alias for passive on buys / cross on sells
    LIMIT_AT_MID = "limit_at_mid"
    LIMIT_OFFSET = "limit_offset"


def resolve_yes_buy(
    mode: EntryPriceMode,
    best_bid: int,
    best_ask: int,
    limit_offset: int = 0,
) -> tuple[int, str, str]:
    """Buy YES: returns (yes_price_cents, order_type, time_in_force)."""
    if mode in (EntryPriceMode.PASSIVE, EntryPriceMode.LIMIT_AT_BID):
        return best_bid, "limit", "gtc"
    if mode in (EntryPriceMode.CROSS_SPREAD, EntryPriceMode.LIMIT_AT_ASK):
        return best_ask, "limit", "ioc"
    if mode == EntryPriceMode.MARKET:
        return best_ask, "market", "ioc"
    if mode == EntryPriceMode.LIMIT_AT_MID:
        return (best_bid + best_ask) // 2, "limit", "gtc"
    price = max(1, min(99, best_bid + limit_offset))
    tif = "gtc" if price <= best_bid else "ioc"
    return price, "limit", tif


def resolve_yes_sell(
    mode: EntryPriceMode,
    best_bid: int,
    best_ask: int,
    limit_offset: int = 0,
) -> tuple[int, str, str]:
    """Sell YES: returns (yes_price_cents, order_type, time_in_force)."""
    if mode in (EntryPriceMode.PASSIVE, EntryPriceMode.LIMIT_AT_ASK):
        return best_ask, "limit", "gtc"
    if mode in (EntryPriceMode.CROSS_SPREAD, EntryPriceMode.LIMIT_AT_BID):
        return best_bid, "limit", "ioc"
    if mode == EntryPriceMode.MARKET:
        return best_bid, "market", "ioc"
    if mode == EntryPriceMode.LIMIT_AT_MID:
        return (best_bid + best_ask) // 2, "limit", "gtc"
    price = max(1, min(99, best_ask - limit_offset))
    tif = "gtc" if price >= best_ask else "ioc"
    return price, "limit", tif


def resolve_no_buy(
    mode: EntryPriceMode,
    best_bid: int,
    best_ask: int,
    limit_offset: int = 0,
) -> tuple[int, str, str]:
    """
    Buy NO from the YES book.

    Passive NO bid = 100 - YES ask; aggressive NO ask = 100 - YES bid.
    """
    no_ask = 100 - best_bid
    no_bid = 100 - best_ask

    if mode in (EntryPriceMode.PASSIVE, EntryPriceMode.LIMIT_AT_BID):
        return no_bid, "limit", "gtc"
    if mode in (EntryPriceMode.CROSS_SPREAD, EntryPriceMode.LIMIT_AT_ASK):
        return no_ask, "limit", "ioc"
    if mode == EntryPriceMode.MARKET:
        return no_ask, "market", "ioc"
    if mode == EntryPriceMode.LIMIT_AT_MID:
        return (no_bid + no_ask) // 2, "limit", "gtc"
    price = max(1, min(99, no_bid + limit_offset))
    tif = "gtc" if price <= no_bid else "ioc"
    return price, "limit", tif


def execution_meta(
    *,
    order_type: str,
    time_in_force: str,
    price_mode: str,
    phase: str,
    action: str = "buy",
) -> dict[str, str]:
    return {
        "order_type":    order_type,
        "action":        action,
        "time_in_force": time_in_force,
        "price_mode":    price_mode,
        "phase":         phase,
    }
