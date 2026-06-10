"""
tests/test_high_prob_strategy.py

Unit tests for HighProbStrategy entry filters and execution metadata.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cfg = types.ModuleType("config")
cfg.KELLY_DIVISOR = 4
cfg.MAX_POSITION_CENTS = 10_000
cfg.MIN_EDGE_TO_VIG = 0.02
cfg.PROFIT_TARGET_PCT = 0.60
cfg.POSITION_STOP_LOSS_PCT = 0.40
cfg.MIN_ACCOUNT_BALANCE_CENTS = 5_000
cfg.HP_USE_FEE_ADJUSTED_ROI = True
cfg.HP_ASSUME_ROUND_TRIP_FEES = False
cfg.HP_MIN_ROI_PCT = 2.0
cfg.HP_MIN_YES_ASK = 85
cfg.HP_MAX_YES_ASK = 97
cfg.HP_STAKE_CENTS = 5000
cfg.FEE_PER_CONTRACT_CENTS = 7.0
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
def _high_prob_test_config():
    sys.modules["config"] = cfg
    sync_config_bindings()
    yield


from discovery.market_math import (
    gross_roi_if_yes_wins_pct,
    take_profit_price_cents,
    vig_proxy_cents,
)
from strategy.execution_price import EntryPriceMode, resolve_yes_buy
from strategy.high_prob_strategy import (
    HighProbStrategy,
    PostFillMode,
    PositionState,
    TakeProfitStyle,
)


def _tick(bid: int, ask: int, ticker: str = "TEST-MKT") -> dict:
    return {
        "ticker": ticker,
        "best_bid": bid,
        "best_ask": ask,
        "spread": ask - bid,
    }


def _tick_one_sided(bid: int, ticker: str = "TEST-MKT") -> dict:
    return {"ticker": ticker, "best_bid": bid, "best_ask": None}


class TestOneSidedBook:
    def test_entry_requires_two_sided_book(self):
        strat = HighProbStrategy(min_yes_ask=85, min_roi_pct=0.0)
        strat.add_watch_ticker("TEST-MKT")
        assert strat.evaluate(_tick_one_sided(88)) is None

    def test_stop_loss_on_one_sided_book(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            post_fill_mode=PostFillMode.RESTING_STOP_LOSS,
            stop_loss_cents=10,
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
        stop_sig = strat.evaluate(_tick_one_sided(79))
        assert stop_sig is not None
        assert stop_sig.meta["phase"] == "stop_loss"

    def test_resting_tp_on_one_sided_book(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            post_fill_mode=PostFillMode.RESTING_TAKE_PROFIT,
            take_profit_offset_cents=3,
            tp_style=TakeProfitStyle.FIXED,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "ord-2",
        })
        exit_sig = strat.evaluate(_tick_one_sided(91))
        assert exit_sig is not None
        assert exit_sig.meta["phase"] == "exit"
        assert exit_sig.limit_price == 93


class TestTakeProfitMath:
    def test_vig_proxy_half_spread(self):
        assert vig_proxy_cents(4) == 2
        assert vig_proxy_cents(1) == 1
        assert vig_proxy_cents(None) == 1  # default spread 2 -> vig 1

    def test_take_profit_pct_entry_plus_vig(self):
        # entry 90, vig 1, 30% gain -> 117, capped at 99
        assert take_profit_price_cents(90, vig_proxy_cents(2), 0.30) == 99

    def test_take_profit_pct_uncapped(self):
        assert take_profit_price_cents(50, 1, 0.10) == 55

    def test_take_profit_whole_number_percent(self):
        assert take_profit_price_cents(88, 1, 30) == 99

    def test_take_profit_capped_at_99(self):
        assert take_profit_price_cents(95, 2, 0.50) == 99


class TestRoiAndEntryPrice:
    def test_roi_at_90c(self):
        assert abs(gross_roi_if_yes_wins_pct(90) - 11.111) < 0.01

    def test_passive_buy_at_bid(self):
        price, order_type, tif = resolve_yes_buy(
            EntryPriceMode.PASSIVE, 88, 90, 0,
        )
        assert price == 88
        assert order_type == "limit"
        assert tif == "gtc"

    def test_market_uses_ioc(self):
        price, order_type, tif = resolve_yes_buy(
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

    def test_passive_entry_rejects_when_limit_below_min_ask(self):
        """Entry band and ROI apply to limit price (bid), not raw ask."""
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            entry_price_mode=EntryPriceMode.PASSIVE,
        )
        strat.add_watch_ticker("TEST-MKT")
        assert strat.evaluate(_tick(82, 90)) is None

    def test_passive_entry_allowed_when_bid_in_band(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            entry_price_mode=EntryPriceMode.PASSIVE,
        )
        strat.add_watch_ticker("TEST-MKT")
        sig = strat.evaluate(_tick(88, 90))
        assert sig is not None
        assert sig.limit_price == 88


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

    def test_resting_take_profit_pct_of_entry_plus_vig(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
            post_fill_mode=PostFillMode.RESTING_TAKE_PROFIT,
            take_profit_pct=0.30,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))  # spread 2 -> vig 1
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "ord-1",
        })
        pos = strat._positions["TEST-MKT"]
        assert pos.take_profit_price == 99  # 30% of (90+1) capped at 99¢
        exit_sig = strat.evaluate(_tick(96, 98))
        assert exit_sig is not None
        assert exit_sig.limit_price >= 99

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

    def test_stop_loss_cents_on_fill(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            stop_loss_cents=10,
            post_fill_mode=PostFillMode.RESTING_STOP_LOSS,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "ord-2",
        })
        assert strat._positions["TEST-MKT"].stop_loss_trigger == 80

    def test_resting_tp_fixed_keeps_computed_price_below_ask(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
            post_fill_mode=PostFillMode.RESTING_TAKE_PROFIT,
            take_profit_offset_cents=3,
            tp_style=TakeProfitStyle.FIXED,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "ord-3",
        })
        exit_sig = strat.evaluate(_tick(91, 96))
        assert exit_sig is not None
        assert exit_sig.limit_price == 93

    def test_resting_tp_at_ask_bumps_to_current_ask(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
            post_fill_mode=PostFillMode.RESTING_TAKE_PROFIT,
            take_profit_offset_cents=3,
            tp_style=TakeProfitStyle.AT_ASK,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "ord-4",
        })
        exit_sig = strat.evaluate(_tick(91, 96))
        assert exit_sig is not None
        assert exit_sig.limit_price == 96


class TestHighProbCycles:
    def test_unlimited_cycles_reenters_after_exit(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            max_cycles_per_ticker=0,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "e1",
        })
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "action": "sell",
            "price": 93,
            "size_cents": 4_650,
            "order_id": "x1",
        })
        assert strat.get_position("TEST-MKT").state == PositionState.CLOSED
        sig = strat.evaluate(_tick(88, 90))
        assert sig is not None
        assert strat.get_position("TEST-MKT").state == PositionState.WATCHING

    def test_max_cycles_blocks_reentry(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            max_cycles_per_ticker=1,
            entry_price_mode=EntryPriceMode.LIMIT_AT_ASK,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "e1",
        })
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "action": "sell",
            "price": 93,
            "size_cents": 4_650,
            "order_id": "x1",
        })
        assert strat.get_position("TEST-MKT").cycles_completed == 1
        assert strat.evaluate(_tick(88, 90)) is None


class TestHighProbStopCancelsTp:
    def test_stop_while_resting_tp_cancels_tp_order(self):
        strat = HighProbStrategy(
            min_yes_ask=85,
            min_roi_pct=0.0,
            post_fill_mode=PostFillMode.TAKE_PROFIT_AND_STOP,
            stop_loss_cents=10,
            take_profit_offset_cents=3,
        )
        strat.add_watch_ticker("TEST-MKT")
        strat.evaluate(_tick(88, 90))
        strat.on_fill({
            "ticker": "TEST-MKT",
            "side": "yes",
            "price": 90,
            "size_cents": 5_000,
            "order_id": "e2",
        })
        strat.evaluate(_tick(91, 93))
        pos = strat.get_position("TEST-MKT")
        pos.tp_order_id = "tp-1"
        pos.tp_limit_price = 93
        pos.state = PositionState.EXIT_PENDING

        sig = strat.evaluate(_tick(79, 81))
        assert sig is not None
        assert sig.meta.get("phase") == "stop_loss"
        assert sig.meta.get("cancel_order_id") == "tp-1"
        assert pos.tp_order_id == ""


def test_exit_fill_on_no_side_while_exit_pending():
    """Kalshi reports YES-sell on the contra ('no') side; EXIT_PENDING must
    still finalise the round-trip when the fill matches the resting exit."""
    strat = HighProbStrategy(
        min_yes_ask=85,
        min_roi_pct=0.0,
        post_fill_mode=PostFillMode.RESTING_TAKE_PROFIT,
    )
    strat.add_watch_ticker("TEST-MKT")
    pos = strat.get_position("TEST-MKT")
    pos.state = PositionState.EXIT_PENDING
    pos.entry_price_cents = 90
    pos.entry_stake_cents = 5_000
    pos.tp_order_id = "tp-exit-1"
    pos.tp_order_sent = True

    strat.on_fill({
        "ticker": "TEST-MKT",
        "side": "no",
        "price": 93,
        "size_cents": 4_650,
        "order_id": "tp-exit-1",
    })
    assert pos.state == PositionState.CLOSED
    assert pos.tp_order_id == ""
    assert pos.cycles_completed == 1


def test_exit_fill_ignores_unrelated_order_id():
    strat = HighProbStrategy(min_yes_ask=85, min_roi_pct=0.0)
    strat.add_watch_ticker("TEST-MKT")
    pos = strat.get_position("TEST-MKT")
    pos.state = PositionState.EXIT_PENDING
    pos.entry_price_cents = 90
    pos.tp_order_id = "tp-exit-1"

    strat.on_fill({
        "ticker": "TEST-MKT",
        "side": "no",
        "price": 93,
        "size_cents": 4_650,
        "order_id": "some-other-order",
    })
    assert pos.state == PositionState.EXIT_PENDING


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
