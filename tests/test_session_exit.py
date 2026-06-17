"""Tests for trading/session_exit.py auto-shutdown conditions."""

from datetime import datetime, timedelta, timezone

from discovery.market_client import MarketSummary
from trading.session_exit import (
    market_is_done,
    should_exit_on_settle,
    should_exit_when_flat,
)


def _market(
    ticker: str,
    *,
    minutes_to_close: float = 120.0,
    status: str = "open",
) -> MarketSummary:
    close = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_close)
    return MarketSummary(
        ticker=ticker,
        event_ticker="EVT",
        title="Test",
        category="Sports",
        series_ticker="SER",
        yes_bid=10,
        yes_ask=12,
        no_bid=88,
        no_ask=90,
        last_price=11,
        volume=1000,
        volume_24h=1000,
        open_interest=100,
        liquidity=5000,
        status=status,
        close_time=close,
        updated_at=datetime.now(timezone.utc),
        result=None,
    )


def test_exit_on_settle_waits_for_final_status():
    reason = should_exit_on_settle(
        ["T-A"],
        {"T-A": {"status": "open", "result": None}},
        [],
    )
    assert reason is None


def test_exit_on_settle_when_all_settled_and_blotter_clean():
    reason = should_exit_on_settle(
        ["T-A", "T-B"],
        {
            "T-A": {"status": "settled", "result": "yes"},
            "T-B": {"status": "finalized", "result": "no"},
        },
        [],
    )
    assert reason is not None
    assert "settled" in reason


def test_exit_on_settle_blocked_by_open_blotter():
    reason = should_exit_on_settle(
        ["T-A"],
        {"T-A": {"status": "settled", "result": "yes"}},
        [{"trade_id": "T-0001", "ticker": "T-A", "status": "hedged"}],
    )
    assert reason is None


def test_exit_when_flat_not_at_startup():
    reason = should_exit_when_flat(
        ["KXNBAGAME-26JUN10SASNYK-NYK"],
        {"KXNBAGAME-26JUN10SASNYK-NYK": {"status": "open"}},
        {"KXNBAGAME-26JUN10SASNYK-NYK": _market("KXNBAGAME-26JUN10SASNYK-NYK", minutes_to_close=120)},
        [],
        {},
        {"KXNBAGAME-26JUN10SASNYK-NYK": "scanning"},
        now=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )
    assert reason is None


def test_exit_when_flat_after_game_window_with_no_activity():
    ticker = "KXNBAGAME-26JUN10SASNYK-NYK"
    reason = should_exit_when_flat(
        [ticker],
        {ticker: {"status": "open"}},
        {ticker: _market(ticker, minutes_to_close=5000)},
        [],
        {},
        {ticker: "scanning"},
        now=datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc),
    )
    assert reason is not None
    assert "flat" in reason


def test_exit_when_flat_allows_hedged_blotter():
    ticker = "T-A"
    reason = should_exit_when_flat(
        [ticker],
        {ticker: {"status": "closed"}},
        {ticker: _market(ticker, minutes_to_close=0, status="closed")},
        [{"trade_id": "T-0001", "ticker": ticker, "status": "hedged"}],
        {},
        {ticker: "hedged"},
    )
    assert reason is not None


def test_exit_when_flat_blocked_by_open_order():
    reason = should_exit_when_flat(
        ["T-A"],
        {"T-A": {"status": "closed"}},
        {"T-A": _market("T-A", minutes_to_close=0)},
        [],
        {"oid-1": {"ticker": "T-A", "status": "resting"}},
        {"T-A": "scanning"},
    )
    assert reason is None


def test_exit_when_flat_blocked_by_in_flight_strategy():
    reason = should_exit_when_flat(
        ["T-A"],
        {"T-A": {"status": "closed"}},
        {"T-A": _market("T-A", minutes_to_close=0)},
        [],
        {},
        {"T-A": "entered"},
    )
    assert reason is None


def test_market_is_done_uses_settled_status():
    assert market_is_done("T-A", "settled", None) is True


def test_market_is_done_game_after_estimated_end():
    ticker = "KXNBAGAME-26JUN10SASNYK-NYK"
    assert market_is_done(
        ticker,
        "open",
        None,
        now=datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc),
    ) is True
