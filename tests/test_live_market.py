"""Live market filter tests."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.live_market import LiveMarketRules, is_market_live
from discovery.market_client import MarketClient


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
