"""
ingestion/book_freshness.py

Shared helpers for detecting stale WebSocket order books and preferring REST
snapshots when WS has not updated recently.
"""

from __future__ import annotations

import time
from typing import Any

from discovery.orderbook_parse import OrderBookSnapshot


def book_age_seconds(
    book: Any | None,
    *,
    now_us: int | None = None,
) -> float | None:
    """Seconds since the in-memory book last changed (WS or REST)."""
    updated = getattr(book, "updated_at_us", 0) if book is not None else 0
    if not updated:
        return None
    now = now_us if now_us is not None else int(time.time() * 1_000_000)
    return max(0.0, (now - updated) / 1_000_000)


def is_book_stale(book: Any | None, max_age_seconds: float) -> bool:
    """True when the book is missing or older than max_age_seconds."""
    if max_age_seconds <= 0:
        return False
    age = book_age_seconds(book)
    if age is None:
        return True
    return age > max_age_seconds


def rest_snapshot_age_seconds(snap: OrderBookSnapshot) -> float:
    """Age of a REST order book snapshot."""
    return max(0.0, (time.time() - snap.fetched_at.timestamp()))
