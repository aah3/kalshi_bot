"""Pre-trade entry gate tests."""

import types
from datetime import datetime, timedelta, timezone

import pytest

from discovery.market_client import MarketSummary
import risk.entry_gates as entry_gates


def _gate_config(**overrides):
    base = types.SimpleNamespace(
        MIN_ACCOUNT_BALANCE_CENTS=5_000,
        MIN_MINUTES_TO_EXPIRY=10.0,
        BLOCK_ENTRIES_ON_LOW_BALANCE=True,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _market(mins_to_close: float) -> MarketSummary:
    close = datetime.now(timezone.utc) + timedelta(minutes=mins_to_close)
    return MarketSummary(
        ticker="TEST-TICKER",
        event_ticker="EVT",
        title="Test",
        category="Sports",
        series_ticker="SER",
        yes_bid=40,
        yes_ask=42,
        no_bid=58,
        no_ask=60,
        last_price=41,
        volume=1000,
        volume_24h=1000,
        open_interest=500,
        liquidity=10_000,
        status="open",
        close_time=close,
        updated_at=datetime.now(timezone.utc),
        result=None,
    )


@pytest.fixture
def gates(monkeypatch):
    cfg = _gate_config()
    monkeypatch.setattr(entry_gates, "config", cfg)
    return cfg


def test_allows_hedge_phase_with_low_balance(gates):
    ok, _ = entry_gates.check_entry_allowed(
        phase="hedge",
        ticker="T",
        cash_balance_cents=100,
    )
    assert ok


def test_blocks_entry_low_balance(gates):
    ok, reason = entry_gates.check_entry_allowed(
        phase="entry",
        ticker="T",
        cash_balance_cents=4_999,
    )
    assert not ok
    assert "balance" in reason.lower()


def test_blocks_entry_near_expiry(gates):
    ok, reason = entry_gates.check_entry_allowed(
        phase="entry",
        ticker="T",
        cash_balance_cents=10_000,
        market=_market(5.0),
    )
    assert not ok
    assert "closes" in reason.lower()


def test_allows_entry_with_room_to_close(gates):
    ok, _ = entry_gates.check_entry_allowed(
        phase="entry",
        ticker="T",
        cash_balance_cents=10_000,
        market=_market(60.0),
    )
    assert ok


def test_minutes_to_close_from_market(gates):
    m = _market(30.0)
    assert m.minutes_to_close is not None
    assert entry_gates.minutes_to_close("T", m) == m.minutes_to_close
