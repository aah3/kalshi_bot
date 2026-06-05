"""
tests/test_mean_reversion_strategy.py

Unit tests for MeanReversionStrategy entry/exit logic.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cfg = types.ModuleType("config")
cfg.MAX_POSITION_CENTS = 10_000
cfg.MR_STAKE_CENTS = 5_000
log_mod = types.ModuleType("logging_.structured_logger")


class _StubLogger:
    def __getattr__(self, _):
        return lambda *a, **kw: None


log_mod.logger = _StubLogger()
sys.modules["logging_"] = types.ModuleType("logging_")
sys.modules["logging_.structured_logger"] = log_mod
sys.modules["config"] = cfg

from tests.conftest import sync_config_bindings


@pytest.fixture(autouse=True)
def _mean_rev_test_config():
    sys.modules["config"] = cfg
    sync_config_bindings()
    yield


from strategy.execution_price import EntryPriceMode
from strategy.mean_reversion_strategy import (
    ExitTarget,
    MeanReversionStrategy,
    PositionState,
    PostFillMode,
    TradeDirection,
)


def _tick(bid: int, ask: int, ticker: str = "TEST-MKT") -> dict:
    return {
        "ticker": ticker,
        "best_bid": bid,
        "best_ask": ask,
        "spread": ask - bid,
    }


def _warmup(strat: MeanReversionStrategy, ticker: str, prices: list[int]) -> None:
    """Seed rolling mid-price history without emitting entry signals."""
    for mid in prices:
        bid = max(1, mid - 1)
        ask = min(99, mid + 1)
        strat._record_mid(ticker, bid, ask)


class TestMeanReversionEntry:
    def setup_method(self):
        self.strat = MeanReversionStrategy(
            lookback_ticks=10,
            min_samples=8,
            entry_deviation_cents=5,
            min_volatility_cents=3.0,
            entry_max_price=45,
            entry_min_price=10,
            max_spread_cents=8,
            stake_cents=5_000,
            trade_direction=TradeDirection.LONG,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
            post_fill_mode=PostFillMode.HOLD_TO_SETTLEMENT,
        )
        self.strat.add_watch_ticker("TEST-MKT")

    def test_long_entry_on_dip_below_mean(self):
        # Oscillating 30–40 range, then dip to 22
        _warmup(self.strat, "TEST-MKT", [35, 38, 36, 34, 37, 35, 33, 36, 34, 22])
        sig = self.strat.evaluate(_tick(20, 22))
        assert sig is not None
        assert sig.side.value == "yes"
        assert sig.meta["phase"] == "entry"
        assert sig.meta["direction"] == "long"

    def test_rejects_when_volatility_too_low(self):
        flat = [30] * 10
        _warmup(self.strat, "TEST-MKT", flat)
        assert self.strat.evaluate(_tick(29, 31)) is None

    def test_rejects_when_not_deviated_enough(self):
        _warmup(self.strat, "TEST-MKT", [30, 35, 32, 38, 33, 36, 31, 37, 34, 33])
        assert self.strat.evaluate(_tick(32, 34)) is None


class TestMeanReversionShort:
    def setup_method(self):
        self.strat = MeanReversionStrategy(
            lookback_ticks=10,
            min_samples=8,
            entry_deviation_cents=5,
            min_volatility_cents=3.0,
            short_min_yes_ask=55,
            short_max_yes_ask=90,
            max_spread_cents=8,
            trade_direction=TradeDirection.SHORT,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
            post_fill_mode=PostFillMode.HOLD_TO_SETTLEMENT,
        )
        self.strat.add_watch_ticker("TEST-MKT")

    def test_short_entry_on_spike_above_mean(self):
        _warmup(self.strat, "TEST-MKT", [60, 62, 58, 61, 59, 63, 60, 58, 61, 72])
        sig = self.strat.evaluate(_tick(70, 72))
        assert sig is not None
        assert sig.side.value == "no"
        assert sig.meta["direction"] == "short"


class TestMeanReversionExit:
    def test_resting_take_profit_after_entry_fill(self):
        strat = MeanReversionStrategy(
            lookback_ticks=10,
            min_samples=8,
            entry_deviation_cents=5,
            min_volatility_cents=3.0,
            take_profit_offset_cents=5,
            exit_target=ExitTarget.MAX,
            trade_direction=TradeDirection.LONG,
            post_fill_mode=PostFillMode.RESTING_TAKE_PROFIT,
            exit_price_mode=EntryPriceMode.PASSIVE,
        )
        strat.add_watch_ticker("TEST-MKT")
        _warmup(strat, "TEST-MKT", [35, 38, 36, 34, 37, 35, 33, 36, 34, 22])
        entry_sig = strat.evaluate(_tick(20, 22))
        assert entry_sig is not None

        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 22,
            "size_cents": 5000,
            "order_id": "entry-1",
        })
        pos = strat.get_position("TEST-MKT")
        assert pos is not None
        assert pos.state == PositionState.ENTERED
        assert pos.take_profit_price > 22

        exit_sig = strat.evaluate(_tick(24, 26))
        assert exit_sig is not None
        assert exit_sig.meta["phase"] == "exit"
        assert exit_sig.meta["action"] == "sell"
        assert exit_sig.limit_price == pos.take_profit_price

    def test_stop_loss_triggers_on_breach(self):
        strat = MeanReversionStrategy(
            lookback_ticks=10,
            min_samples=8,
            entry_deviation_cents=5,
            min_volatility_cents=3.0,
            stop_loss_cents=8,
            trade_direction=TradeDirection.LONG,
            post_fill_mode=PostFillMode.RESTING_STOP_LOSS,
            exit_price_mode=EntryPriceMode.PASSIVE,
        )
        strat.add_watch_ticker("TEST-MKT")
        _warmup(strat, "TEST-MKT", [35, 38, 36, 34, 37, 35, 33, 36, 34, 22])
        strat.evaluate(_tick(20, 22))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 22,
            "size_cents": 5000,
            "order_id": "entry-1",
        })

        stop_sig = strat.evaluate(_tick(12, 14))
        assert stop_sig is not None
        assert stop_sig.meta["phase"] == "stop_loss"


class TestMeanReversionCycle:
    def test_exit_fill_starts_new_cycle(self):
        strat = MeanReversionStrategy(
            lookback_ticks=10,
            min_samples=8,
            entry_deviation_cents=5,
            min_volatility_cents=3.0,
            max_cycles_per_ticker=2,
            trade_direction=TradeDirection.LONG,
            post_fill_mode=PostFillMode.HOLD_TO_SETTLEMENT,
        )
        strat.add_watch_ticker("TEST-MKT")
        _warmup(strat, "TEST-MKT", [35, 38, 36, 34, 37, 35, 33, 36, 34, 22])
        strat.evaluate(_tick(20, 22))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 22,
            "size_cents": 5000,
            "order_id": "entry-1",
        })
        pos = strat.get_position("TEST-MKT")
        assert pos is not None
        pos.state = PositionState.EXIT_PENDING
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 30,
            "size_cents": 5000,
            "order_id": "exit-1",
            "action": "sell",
        })
        pos = strat.get_position("TEST-MKT")
        assert pos is not None
        assert pos.state == PositionState.CLOSED
        assert pos.cycles_completed == 1
