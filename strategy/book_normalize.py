"""
strategy/book_normalize.py

Helpers for one-sided order books (bid present, ask missing — common near
resolution when a heavy favourite has no YES sellers).
"""

from __future__ import annotations

from typing import Any


def normalize_tick_sides(tick: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return a copy of ``tick`` with a synthetic complement when one side is missing.

    Requires at least ``best_bid``. Used for hedge/stop evaluation on ENTERED
    positions; entry paths should still require a real two-sided book.
    """
    ticker = tick.get("ticker", "")
    best_bid = tick.get("best_bid")
    best_ask = tick.get("best_ask")
    if not ticker or best_bid is None:
        return None
    if best_ask is not None:
        return tick

    out = dict(tick)
    if best_bid >= 99:
        out["best_ask"] = 99
    else:
        out["best_ask"] = min(99, best_bid + 1)

    bid = int(out["best_bid"])
    ask = int(out["best_ask"])
    out["spread"] = ask - bid
    if out.get("mid_price") is None:
        out["mid_price"] = (bid + ask) / 2.0
    return out
