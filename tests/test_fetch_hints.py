"""Tests for browse/screen empty-state hint messages."""

from discovery.market_client import CategoryFetchStats
from discovery.screener import ScreenRunStats


def test_fetch_hints_combined_filters_no_overlap():
    stats = CategoryFetchStats(
        category="Sports",
        events_matched=3239,
        pages_scanned=40,
        markets_seen=26970,
        matched=0,
        pass_volume_only=1200,
        pass_activity_only=953,
        max_vol_activity_only=8,
        full_scan=True,
    )
    lines = stats.format_empty_hints(
        min_volume_24h=50,
        activity_hours=2.0,
        env="demo",
    )
    text = "\n".join(lines)
    assert "953" in text
    assert "1,200" in text or "1200" in text
    assert "peak vol24h: 8" in text
    assert "drop --min-volume 50" in text
    assert "demo API" in text


def test_fetch_hints_no_events():
    stats = CategoryFetchStats(category="Unknown", events_matched=0)
    lines = stats.format_empty_hints()
    assert any("--categories" in line for line in lines)


def test_screen_hints_no_tradeable():
    stats = ScreenRunStats(
        markets_fetched=100,
        tradeable=0,
        scored=0,
        category="Sports",
    )
    lines = stats.format_empty_hints()
    assert any("tradeable" in line.lower() for line in lines)


def test_screen_hints_fetched_but_not_scored():
    stats = ScreenRunStats(
        markets_fetched=50,
        tradeable=12,
        scored=0,
        category="Politics",
    )
    lines = stats.format_empty_hints()
    text = "\n".join(lines)
    assert "12 tradeable" in text
    assert "0.30" in text
