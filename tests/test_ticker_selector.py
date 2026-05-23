"""Unit tests for discovery/ticker_selector.py (no API calls)."""

from discovery.market_client import MarketClient
from discovery.ticker_selector import (
    DEFAULT_DISCOVER_CATEGORY,
    TickerCriteria,
    filter_markets,
    format_discovery_table,
    market_filter_rejection,
    near_miss_markets,
    resolve_discover_category,
    select_tickers,
    summarize_filter_rejections,
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


def test_summarize_filter_rejections():
    markets = [
        _market("LOW-ASK", volume_24h=500, yes_ask=80),
        _market("HIGH-ASK", volume_24h=500, yes_ask=97),
        _market("OK", volume_24h=500, yes_ask=88),
    ]
    criteria = TickerCriteria(
        category="Sports",
        top_n=5,
        min_yes_ask=85,
        max_yes_ask=97,
        min_fee_adjusted_roi_pct=2.0,
        tradeable_only=False,
        live_only=False,
    )
    summary = summarize_filter_rejections(markets, criteria)
    assert summary["passed"] == 1
    assert summary["min_yes_ask"] == 1
    assert summary["fee_adjusted_roi"] == 1
    assert market_filter_rejection(markets[2], criteria) is None


def test_near_miss_markets_when_none_selected():
    markets = [
        _market("LOW-ASK", volume_24h=500, yes_ask=80),
        _market("HIGH-ASK", volume_24h=900, yes_ask=97),
        _market("OK", volume_24h=500, yes_ask=88),
    ]
    criteria = TickerCriteria(
        category="Sports",
        top_n=2,
        min_yes_ask=85,
        max_yes_ask=97,
        min_fee_adjusted_roi_pct=2.0,
        tradeable_only=False,
        live_only=False,
    )
    misses = near_miss_markets(markets, criteria, limit=5)
    assert len(misses) == 2
    assert misses[0][0].ticker == "HIGH-ASK"
    assert misses[0][1] == "fee_adjusted_roi"


def test_format_discovery_table_shows_near_misses():
    markets = [
        _market("LOW-ASK", volume_24h=500, yes_ask=80),
        _market("HIGH-ASK", volume_24h=900, yes_ask=97),
    ]
    criteria = TickerCriteria(
        category="Sports",
        top_n=2,
        min_yes_ask=85,
        max_yes_ask=97,
        min_fee_adjusted_roi_pct=2.0,
        tradeable_only=False,
        live_only=False,
    )
    text = format_discovery_table(markets, [], criteria)
    assert "Near misses" in text
    assert "HIGH-ASK" in text
    assert "fee ROI too low" in text
