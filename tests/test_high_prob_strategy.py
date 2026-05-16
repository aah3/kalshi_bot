"""
tests/test_high_prob_strategy.py

Unit tests for HighProbStrategy entry filters and execution metadata.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cfg = types.ModuleType("config")
cfg.KELLY_DIVISOR = 4
cfg.MAX_POSITION_CENTS = 10_000
cfg.MIN_EDGE_TO_VIG = 0.02
cfg.FEE_PER_CONTRACT_CENTS = 0.0
cfg.PROFIT_TARGET_PCT = 0.60
cfg.POSITION_STOP_LOSS_PCT = 0.40
cfg.MIN_ACCOUNT_BALANCE_CENTS = 5_000
cfg.HP_USE_FEE_ADJUSTED_ROI = True
cfg.HP_ASSUME_ROUND_TRIP_FEES = False
cfg.FEE_PER_CONTRACT_CENTS = 7.0
sys.modules["config"] = cfg

log_mod = types.ModuleType("logging_.structured_logger")


class _StubLogger:
    def __getattr__(self, _):
        return lambda *a, **kw: None


log_mod.logger = _StubLogger()
sys.modules["logging_"] = types.ModuleType("logging_")
sys.modules["logging_.structured_logger"] = log_mod

from discovery.market_math import gross_roi_if_yes_wins_pct
from strategy.high_prob_strategy import (
    EntryPriceMode,
    HighProbStrategy,
    PostFillMode,
    _resolve_entry_price,
)


def _tick(bid: int, ask: int, ticker: str = "TEST-MKT") -> dict:
    return {
        "ticker": ticker,
        "best_bid": bid,
        "best_ask": ask,
        "spread": ask - bid,
    }


class TestRoiAndEntryPrice:
    def test_roi_at_90c(self):
        assert abs(gross_roi_if_yes_wins_pct(90) - 11.111) < 0.01

    def test_limit_at_bid_is_passive(self):
        price, order_type, tif = _resolve_entry_price(
            EntryPriceMode.LIMIT_AT_BID, 88, 90, 0,
        )
        assert price == 88
        assert order_type == "limit"
        assert tif == "gtc"

    def test_market_uses_ioc(self):
        price, order_type, tif = _resolve_entry_price(
            EntryPriceMode.MARKET, 88, 90, 0,
        )
        assert price == 90
        assert order_type == "market"
        assert tif == "ioc"


class TestHighProbEntry:
    def setup_method(self):
        self.strat = HighProbStrategy(
            min_yes_ask=85,
            max_yes_ask=97,
            min_roi_pct=2.0,
            max_spread_cents=8,
            stake_cents=5_000,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
            post_fill_mode=PostFillMode.HOLD_TO_SETTLEMENT,
        )
        self.strat.add_watch_ticker("TEST-MKT")

    def test_entry_in_window(self):
        sig = self.strat.evaluate(_tick(88, 90))
        assert sig is not None
        assert sig.side.value == "yes"
        assert sig.limit_price == 90
        assert sig.meta["order_type"] == "limit"
        assert sig.meta["action"] == "buy"
        assert sig.meta["phase"] == "entry"

    def test_rejects_low_probability(self):
        assert self.strat.evaluate(_tick(70, 72)) is None

    def test_rejects_wide_spread(self):
        assert self.strat.evaluate(_tick(80, 95)) is None

    def test_round_trip_fee_gate_rejects_expensive_ask(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=2.0,
            post_fill_mode=PostFillMode.RESTING_TAKE_PROFIT,
        )
        strat.add_watch_ticker("TEST-MKT")
        assert strat.evaluate(_tick(88, 96)) is None

    def test_no_duplicate_entry_while_watching(self):
        self.strat.evaluate(_tick(88, 90))
        assert self.strat.evaluate(_tick(88, 90)) is None


class TestHighProbExit:
    def test_resting_take_profit_after_fill(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
            post_fill_mode=PostFillMode.RESTING_TAKE_PROFIT,
            take_profit_offset_cents=3,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "ord-1",
        })
        exit_sig = strat.evaluate(_tick(91, 93))
        assert exit_sig is not None
        assert exit_sig.meta["action"] == "sell"
        assert exit_sig.meta["order_type"] == "limit"
        assert exit_sig.meta["time_in_force"] == "gtc"
        assert exit_sig.limit_price == 93

    def test_stop_loss_trigger(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            post_fill_mode=PostFillMode.RESTING_STOP_LOSS,
            stop_loss_pct=0.10,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "ord-1",
        })
        # stop at 90 * 0.9 = 81; bid at 80 triggers
        exit_sig = strat.evaluate(_tick(80, 82))
        assert exit_sig is not None
        assert exit_sig.meta["phase"] == "stop_loss"
        assert exit_sig.meta["time_in_force"] == "ioc"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
