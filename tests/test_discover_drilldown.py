"""Tests for discovery tag/sport/series drill-down (no API calls)."""

from discovery.ticker_selector import (
    TickerCriteria,
    discover_filter_label,
    rank_markets,
)


def _market(
    ticker: str,
    *,
    volume_24h: int = 100,
    spread: int = 5,
    minutes_since_update: float | None = 10.0,
):
    from discovery.market_client import MarketClient

    raw = {
        "ticker": ticker,
        "event_ticker": "EVT",
        "title": ticker,
        "_category_override": "Sports",
        "series_ticker": "KXNBA",
        "yes_bid": 40,
        "yes_ask": 45,
        "volume_24h": volume_24h,
        "volume": volume_24h,
        "open_interest": 100,
        "liquidity": 1000,
        "status": "open",
        "updated_time": "2026-05-25T12:00:00Z",
    }
    m = MarketClient._parse_market(raw)
    m.spread = spread
    m.minutes_since_update = minutes_since_update
    return m


def test_discover_filter_label():
    c = TickerCriteria(
        category="Sports",
        tag="Basketball",
        scope="Games",
    )
    assert discover_filter_label(c) == " (tag=Basketball, scope=Games)"


def test_rank_by_activity_prefers_recent():
    old = _market("OLD", minutes_since_update=120.0, volume_24h=9999)
    new = _market("NEW", minutes_since_update=5.0, volume_24h=100)
    ranked = rank_markets([old, new], rank_by="activity")
    assert ranked[0].ticker == "NEW"


def test_rank_by_spread_prefers_tight():
    wide = _market("WIDE", spread=20, volume_24h=9999)
    tight = _market("TIGHT", spread=2, volume_24h=100)
    ranked = rank_markets([wide, tight], rank_by="spread")
    assert ranked[0].ticker == "TIGHT"
