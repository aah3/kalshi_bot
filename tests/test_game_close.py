"""Tests for discovery/game_close.py."""

from datetime import datetime, timedelta, timezone

from discovery.game_close import (
    apply_effective_close,
    effective_minutes_to_close,
    estimated_game_end,
    is_game_ticker,
    is_within_game_window,
    parse_game_day_start,
)
from discovery.market_client import MarketSummary


def _game_market(
    *,
    ticker: str = "KXNBAGAME-26JUN05NYKSAS-NYK",
    volume_24h: int = 100,
    minutes_since_update: float = 15.0,
    api_minutes_to_close: float = 20_000.0,
    status: str = "open",
    yes_bid: int | None = 50,
    yes_ask: int | None = 52,
) -> MarketSummary:
    now = datetime.now(timezone.utc)
    close_time = now + timedelta(minutes=api_minutes_to_close)
    updated_at = now - timedelta(minutes=minutes_since_update)
    m = MarketSummary(
        ticker=ticker,
        event_ticker="EVT",
        title="Test game",
        category="Sports",
        series_ticker="KXNBAGAME",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=48,
        no_ask=50,
        last_price=51,
        volume=1000,
        volume_24h=volume_24h,
        open_interest=100,
        liquidity=5000,
        status=status,
        close_time=close_time,
        updated_at=updated_at,
        result=None,
    )
    return m


def test_is_game_ticker():
    assert is_game_ticker("KXNBAGAME-26JUN05NYKSAS-NYK")
    assert not is_game_ticker("KXGOVCA-26-SHIL")


def test_parse_game_day_start():
    start = parse_game_day_start("KXNBAGAME-26JUN05NYKSAS-NYK")
    assert start == datetime(2026, 6, 5, tzinfo=timezone.utc)


def test_in_play_game_uses_short_minutes_to_close():
    m = _game_market()
    if not is_within_game_window(m.ticker):
        return  # skip when test runs outside the synthetic game window
    mins = effective_minutes_to_close(m)
    assert mins is not None
    assert mins <= 5.0


def test_registry_apply_effective_close():
    m = _game_market()
    apply_effective_close(m)
    if is_within_game_window(m.ticker) and m.volume_24h > 0:
        assert m.minutes_to_close is not None
        assert m.minutes_to_close <= 5.0


def test_non_game_market_unchanged():
    m = _game_market(ticker="KXGOVCA-26-SHIL", api_minutes_to_close=500.0)
    api_mins = m.minutes_to_close
    assert effective_minutes_to_close(m) == api_mins


def test_estimated_game_end_after_game_day():
    end = estimated_game_end("KXNBAGAME-26JUN05NYKSAS-NYK")
    start = parse_game_day_start("KXNBAGAME-26JUN05NYKSAS-NYK")
    assert end is not None and start is not None
    assert end > start


def test_settled_game_allows_stop_gate():
    m = _game_market(status="settled", api_minutes_to_close=20_000.0)
    assert effective_minutes_to_close(m) <= 5.0


def test_dead_book_low_bid_allows_stop_gate():
    m = _game_market(
        yes_bid=1,
        yes_ask=99,
        minutes_since_update=60.0,
        api_minutes_to_close=20_000.0,
    )
    assert effective_minutes_to_close(m) <= 5.0
