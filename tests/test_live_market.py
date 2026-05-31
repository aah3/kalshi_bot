"""Live market filter tests."""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.live_market import LiveMarketRules, is_market_live, is_tick_live
from discovery.market_client import MarketClient


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _book(
    *,
    updated_min_ago: float = 0.0,
    last_trade_min_ago: float | None = None,
    watching_min: float = 0.0,
) -> SimpleNamespace:
    """Fake WebSocket order book with the timestamps ``is_tick_live`` reads."""
    now = _now_us()
    minute_us = 60_000_000
    return SimpleNamespace(
        updated_at_us=now - int(updated_min_ago * minute_us),
        last_trade_at_us=(
            0 if last_trade_min_ago is None
            else now - int(last_trade_min_ago * minute_us)
        ),
        created_at_us=now - int(watching_min * minute_us),
    )


def _market(**kw) -> object:
    now = datetime.now(timezone.utc)
    raw = {
        "ticker": kw.get("ticker", "TEST"),
        "event_ticker": "E",
        "title": "Test",
        "_category_override": "Sports",
        "yes_bid": 8,
        "yes_ask": 10,
        "volume_24h": 1000,
        "volume": 1000,
        "open_interest": 100,
        "liquidity": 1000,
        "status": "open",
        "updated_time": (now - timedelta(minutes=kw.get("updated_min_ago", 30))).isoformat(),
        "close_time": (now + timedelta(minutes=kw.get("closes_in_min", 120))).isoformat(),
    }
    return MarketClient._parse_market(raw)


def test_rejects_stale_update():
    m = _market(updated_min_ago=300)
    rules = LiveMarketRules(max_minutes_since_update=120.0)
    ok, reason = is_market_live(m, rules)
    assert not ok
    assert "update" in reason.lower()


def test_rejects_far_close():
    m = _market(closes_in_min=500)
    rules = LiveMarketRules(max_minutes_to_close=360.0)
    ok, reason = is_market_live(m, rules)
    assert not ok
    assert "live window" in reason.lower()


def test_accepts_live_window():
    m = _market(updated_min_ago=30, closes_in_min=120)
    rules = LiveMarketRules(
        max_minutes_since_update=120.0,
        max_minutes_to_close=360.0,
    )
    ok, _ = is_market_live(m, rules)
    assert ok


def test_update_recency_can_be_skipped():
    """Discovery enforces updated_time recency; runtime can opt out."""
    m = _market(updated_min_ago=2000)
    rules = LiveMarketRules(max_minutes_since_update=60.0)

    ok, _ = is_market_live(m, rules)
    assert not ok  # discovery default still rejects

    ok, _ = is_market_live(m, rules, check_update_recency=False)
    assert ok  # runtime opt-out ignores stale metadata


def test_tick_live_ignores_stale_metadata_when_book_fresh():
    """The runtime gate must not block an actively-trading market on stale REST
    metadata — the production bug where a 2000m-old ``updated_time`` blocked a
    live NBA market."""
    m = _market(updated_min_ago=2000, closes_in_min=120)
    rules = LiveMarketRules(
        max_minutes_since_update=60.0,
        max_minutes_to_close=360.0,
        max_book_stale_minutes=30.0,
        max_trade_stale_minutes=120.0,
    )
    book = _book(updated_min_ago=0.1, last_trade_min_ago=1.0)
    ok, reason = is_tick_live({}, book, rules, market=m)
    assert ok, reason


def test_tick_live_grace_for_fresh_connection():
    """No trade printed yet, but we only just started watching → allow."""
    rules = LiveMarketRules(max_trade_stale_minutes=120.0)
    book = _book(updated_min_ago=0.1, last_trade_min_ago=None, watching_min=5.0)
    ok, reason = is_tick_live({}, book, rules, market=None)
    assert ok, reason


def test_tick_live_blocks_quiet_market_after_grace():
    """No trade for the whole grace window of watching → too quiet, block."""
    rules = LiveMarketRules(max_trade_stale_minutes=120.0)
    book = _book(updated_min_ago=0.1, last_trade_min_ago=None, watching_min=200.0)
    ok, reason = is_tick_live({}, book, rules, market=None)
    assert not ok
    assert "no trade" in reason.lower()


def test_tick_live_blocks_stale_trade():
    """A trade printed but long ago → market went quiet, block."""
    rules = LiveMarketRules(max_trade_stale_minutes=120.0)
    book = _book(updated_min_ago=0.1, last_trade_min_ago=200.0, watching_min=300.0)
    ok, reason = is_tick_live({}, book, rules, market=None)
    assert not ok
    assert "last trade" in reason.lower()


def test_tick_live_blocks_stale_book():
    """Quotes themselves are stale → dead feed, block before trade checks."""
    rules = LiveMarketRules(max_book_stale_minutes=30.0, max_trade_stale_minutes=120.0)
    book = _book(updated_min_ago=45.0, last_trade_min_ago=1.0)
    ok, reason = is_tick_live({}, book, rules, market=None)
    assert not ok
    assert "stale" in reason.lower()
