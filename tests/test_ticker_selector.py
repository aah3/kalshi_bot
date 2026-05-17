"""Unit tests for discovery/ticker_selector.py (no API calls)."""

from discovery.market_client import MarketClient
from discovery.ticker_selector import (
    DEFAULT_DISCOVER_CATEGORY,
    TickerCriteria,
    filter_markets,
    resolve_discover_category,
    select_tickers,
)


def _market(
    ticker: str,
    *,
    volume_24h: int = 100,
    yes_bid: int = 20,
    yes_ask: int = 22,
    status: str = "open",
) -> object:
    raw = {
        "ticker": ticker,
        "event_ticker": "EVT",
        "title": ticker,
        "_category_override": "Sports",
        "series_ticker": "SER",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "volume_24h": volume_24h,
        "volume": volume_24h,
        "open_interest": 100,
        "liquidity": 1000,
        "status": status,
    }
    return MarketClient._parse_market(raw)


def test_select_top_by_volume_with_max_yes_ask():
    markets = [
        _market("HIGH-VOL-EXPENSIVE", volume_24h=5000, yes_ask=40),
        _market("MID", volume_24h=2000, yes_ask=24),
        _market("TOP-CHEAP", volume_24h=3000, yes_ask=20),
        _market("TOO-CHEAP-SPREAD", volume_24h=9000, yes_bid=1, yes_ask=2),
    ]
    criteria = TickerCriteria(
        category="Sports",
        top_n=2,
        max_yes_ask=25,
        tradeable_only=False,
    )
    tickers = select_tickers(markets, criteria)
    assert tickers == ["TOO-CHEAP-SPREAD", "TOP-CHEAP"]


def test_tradeable_only_excludes_wide_spread():
    wide = _market("WIDE", yes_bid=10, yes_ask=35, volume_24h=9999)
    ok = _market("OK", yes_bid=20, yes_ask=22, volume_24h=100)
    criteria = TickerCriteria(category="Sports", top_n=5, tradeable_only=True)
    filtered = filter_markets([wide, ok], criteria)
    assert [m.ticker for m in filtered] == ["OK"]


def test_resolve_discover_category_defaults_to_trending():
    assert resolve_discover_category(None) == DEFAULT_DISCOVER_CATEGORY
    assert resolve_discover_category("") == DEFAULT_DISCOVER_CATEGORY
    assert resolve_discover_category("  ") == DEFAULT_DISCOVER_CATEGORY
    assert resolve_discover_category("Sports") == "Sports"


def test_min_volume_filter():
    markets = [
        _market("A", volume_24h=10),
        _market("B", volume_24h=500),
    ]
    criteria = TickerCriteria(category="Sports", top_n=10, min_volume_24h=100)
    assert select_tickers(markets, criteria) == ["B"]
