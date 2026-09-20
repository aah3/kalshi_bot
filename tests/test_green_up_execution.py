"""Green-up execution modes, pending-order guard, and screener discovery ranking."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cfg = types.ModuleType("config")
cfg.KELLY_DIVISOR = 4
cfg.MAX_POSITION_CENTS = 10_000
cfg.MIN_EDGE_TO_VIG = 0.02
cfg.FEE_PER_CONTRACT_CENTS = 0.0
cfg.HP_MIN_ROI_PCT = 2.0
cfg.HP_MIN_YES_ASK = 85
cfg.HP_MAX_YES_ASK = 97
cfg.HP_STAKE_CENTS = 5000
cfg.HP_USE_FEE_ADJUSTED_ROI = True
cfg.HP_ASSUME_ROUND_TRIP_FEES = False
cfg.HP_MAX_SPREAD_CENTS = 8
cfg.STOP_LOSS_CLOSE_WINDOW_MINUTES = 0.0
cfg.STOP_LOSS_ESCALATE_SECONDS = 120.0
sys.modules["config"] = cfg

log_mod = types.ModuleType("logging_.structured_logger")


class _StubLogger:
    def __getattr__(self, _):
        return lambda *a, **kw: None


log_mod.logger = _StubLogger()
sys.modules["logging_"] = types.ModuleType("logging_")
sys.modules["logging_.structured_logger"] = log_mod

from discovery.discovery_presets import apply_preset
from discovery.market_client import MarketClient
from discovery.ticker_selector import TickerCriteria, select_tickers
from strategy.execution_price import (
    EntryPriceMode,
    resolve_no_buy,
    resolve_yes_buy,
    resolve_yes_sell,
    resolve_yes_sell_exit,
)
from strategy.green_up_strategy import (
    GreenUpStrategy,
    PositionState,
    parse_stop_loss_cents,
    stop_loss_trigger_price,
)


def _tick(bid: int, ask: int, ticker: str = "TEST-TICKER") -> dict:
    return {
        "ticker": ticker,
        "best_bid": bid,
        "best_ask": ask,
        "spread": ask - bid,
    }


def test_resolve_yes_buy_market_and_passive():
    price, order_type, tif = resolve_yes_buy(EntryPriceMode.MARKET, 8, 10, 0)
    assert price == 10 and order_type == "market" and tif == "ioc"

    price, order_type, tif = resolve_yes_buy(EntryPriceMode.PASSIVE, 8, 10, 0)
    assert price == 8 and order_type == "limit" and tif == "gtc"

    price, order_type, tif = resolve_yes_buy(EntryPriceMode.CROSS_SPREAD, 8, 10, 0)
    assert price == 10 and order_type == "limit" and tif == "ioc"


def test_resolve_yes_sell_passive_and_cross():
    price, order_type, tif = resolve_yes_sell(EntryPriceMode.PASSIVE, 68, 70, 0)
    assert price == 70 and order_type == "limit" and tif == "gtc"

    price, order_type, tif = resolve_yes_sell(EntryPriceMode.CROSS_SPREAD, 68, 70, 0)
    assert price == 68 and order_type == "limit" and tif == "ioc"


def test_resolve_yes_sell_exit_resting_at_bid():
    price, order_type, tif = resolve_yes_sell_exit(EntryPriceMode.CROSS_SPREAD, 9, 10, 0)
    assert price == 9 and order_type == "limit" and tif == "gtc"

    price, order_type, tif = resolve_yes_sell_exit(EntryPriceMode.PASSIVE, 9, 10, 0)
    assert price == 10 and order_type == "limit" and tif == "gtc"


def test_resolve_no_buy_uses_complement():
    price, order_type, tif = resolve_no_buy(EntryPriceMode.LIMIT_AT_ASK, 68, 70, 0)
    assert price == 32 and order_type == "limit" and tif == "ioc"

    price, order_type, tif = resolve_no_buy(EntryPriceMode.LIMIT_AT_BID, 68, 70, 0)
    assert price == 30 and order_type == "limit" and tif == "gtc"


def test_watching_does_not_repeat_entry_signals():
    strat = GreenUpStrategy(
        entry_max_price=25,
        hedge_trigger_price=68,
        entry_price_mode=EntryPriceMode.LIMIT_AT_BID,
    )
    strat.add_watch_ticker("T1")
    first = strat.evaluate(_tick(8, 10, "T1"))
    assert first is not None
    assert first.meta["order_type"] == "limit"
    assert first.limit_price == 8
    second = strat.evaluate(_tick(8, 10, "T1"))
    assert second is None


def test_market_entry_meta():
    strat = GreenUpStrategy(
        entry_max_price=25,
        hedge_trigger_price=68,
        entry_price_mode=EntryPriceMode.MARKET,
    )
    strat.add_watch_ticker("T2")
    sig = strat.evaluate(_tick(8, 10, "T2"))
    assert sig is not None
    assert sig.meta["order_type"] == "market"
    assert sig.meta["time_in_force"] == "ioc"


def test_green_up_preset_ranks_by_screener():
    merged = apply_preset(TickerCriteria(category="Sports"), "green_up")
    assert merged.rank_by == "screener"
    assert merged.activity_hours == 2.0
    assert merged.max_minutes_to_close == 360.0


def test_select_tickers_by_screener_score():
    markets = [
        MarketClient._parse_market({
            "ticker": "LOW-SCORE",
            "event_ticker": "E",
            "title": "Low",
            "_category_override": "Sports",
            "yes_bid": 18,
            "yes_ask": 20,
            "volume_24h": 10_000,
            "volume": 10_000,
            "open_interest": 500,
            "liquidity": 1000,
            "status": "open",
        }),
        MarketClient._parse_market({
            "ticker": "HIGH-SCORE",
            "event_ticker": "E",
            "title": "High",
            "_category_override": "Sports",
            "yes_bid": 13,
            "yes_ask": 15,
            "volume_24h": 6_000,
            "volume": 6_000,
            "open_interest": 500,
            "liquidity": 1000,
            "status": "open",
        }),
    ]
    criteria = TickerCriteria(
        category="Sports",
        top_n=1,
        tradeable_only=False,
        rank_by="screener",
        screener_strategy="green_up",
    )
    tickers = select_tickers(markets, criteria)
    assert tickers == ["HIGH-SCORE"]


def test_limit_offset_entry_below_bid():
    price, order_type, tif = resolve_yes_buy(EntryPriceMode.LIMIT_OFFSET, 10, 12, -2)
    assert price == 8 and order_type == "limit" and tif == "gtc"


def test_max_cycles_allows_two_entries_then_stops():
    strat = GreenUpStrategy(
        entry_max_price=25,
        hedge_trigger_price=68,
        entry_price_mode=EntryPriceMode.MARKET,
        max_cycles_per_ticker=2,
    )
    strat.add_watch_ticker("T3")
    pos = strat.get_position("T3")
    assert pos is not None

    pos.state = PositionState.HEDGED
    pos.cycles_completed = 1
    assert strat.evaluate(_tick(8, 10, "T3")) is not None

    pos = strat.get_position("T3")
    pos.state = PositionState.HEDGED
    pos.cycles_completed = 2
    assert strat.evaluate(_tick(8, 10, "T3")) is None
    assert pos.state == PositionState.CLOSED


def test_unlimited_cycles_resets_after_hedged():
    strat = GreenUpStrategy(
        entry_max_price=25,
        hedge_trigger_price=68,
        entry_price_mode=EntryPriceMode.MARKET,
        max_cycles_per_ticker=0,
    )
    strat.add_watch_ticker("T4")
    pos = strat.get_position("T4")
    pos.state = PositionState.HEDGED
    pos.cycles_completed = 5
    sig = strat.evaluate(_tick(8, 10, "T4"))
    assert sig is not None
    assert strat.get_position("T4").state == PositionState.WATCHING


def test_stop_loss_trigger_price_from_cents():
    assert stop_loss_trigger_price(26, 12) == 14
    assert stop_loss_trigger_price(25, 10) == 15
    assert stop_loss_trigger_price(5, 10) == 1


def test_parse_stop_loss_cents_accepts_integer_and_legacy_fraction():
    assert parse_stop_loss_cents(12) == 12
    assert parse_stop_loss_cents("12") == 12
    assert parse_stop_loss_cents(0.40) == 10


def test_stop_loss_fires_when_bid_drops_enough():
    strat = GreenUpStrategy(
        entry_max_price=99,
        hedge_trigger_price=100,
        stop_loss_cents=12,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T5")
    pos = strat.get_position("T5")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 26
    pos.entry_stake_cents = 26
    pos.entry_contracts = 1
    pos.stop_loss_trigger_price = stop_loss_trigger_price(26, 12)

    assert strat.evaluate(_tick(15, 16, "T5")) is None

    sig = strat.evaluate(_tick(14, 15, "T5"))
    assert sig is not None
    assert sig.side.value == "yes"
    assert sig.meta.get("phase") == "stop_loss"
    assert sig.meta.get("action") == "sell"
    assert sig.meta.get("time_in_force") == "gtc"
    assert sig.limit_price == 14
    assert sig.size_cents == 14
    assert strat.get_position("T5").state == PositionState.STOPPING


def test_passive_exit_stop_crosses_spread_to_bid():
    """Regression: a passive exit must still cross to the bid for the stop leg.

    With PASSIVE pricing a sell rests at the ask and never fills in a falling
    book, so the loss is never capped. The stop must escalate to cross-spread
    (sell at the bid) so it actually exits.
    """
    strat = GreenUpStrategy(
        entry_max_price=99,
        hedge_trigger_price=100,
        stop_loss_cents=12,
        exit_price_mode=EntryPriceMode.PASSIVE,
    )
    strat.add_watch_ticker("T18")
    pos = strat.get_position("T18")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 26
    pos.entry_stake_cents = 26
    pos.entry_contracts = 1
    pos.stop_loss_trigger_price = stop_loss_trigger_price(26, 12)  # 14

    sig = strat.evaluate(_tick(14, 15, "T18"))
    assert sig is not None
    assert sig.meta.get("phase") == "stop_loss"
    assert sig.meta.get("action") == "sell"
    assert sig.meta.get("time_in_force") == "gtc"
    # Crosses to the bid (14), NOT resting at the ask (15).
    assert sig.limit_price == 14


def test_passive_resting_stop_reprices_to_bid():
    """A repriced resting stop in passive mode must also chase the bid, not the ask."""
    strat = GreenUpStrategy(
        stop_loss_cents=12,
        exit_price_mode=EntryPriceMode.PASSIVE,
    )
    strat.add_watch_ticker("T19")
    pos = strat.get_position("T19")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 24
    pos.entry_stake_cents = 24
    pos.entry_contracts = 1
    pos.stop_order_id = "ord-9"
    pos.stop_limit_price = 12

    sig = strat.evaluate(_tick(8, 9, "T19"))
    assert sig is not None
    assert sig.limit_price == 8
    assert sig.meta.get("cancel_order_id") == "ord-9"


def test_explicit_maker_exit_mode_is_preserved_for_stop():
    """An operator who deliberately picks a non-passive exit mode keeps it."""
    strat = GreenUpStrategy(
        entry_max_price=99,
        hedge_trigger_price=100,
        stop_loss_cents=12,
        exit_price_mode=EntryPriceMode.LIMIT_AT_MID,
    )
    strat.add_watch_ticker("T20")
    pos = strat.get_position("T20")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 26
    pos.entry_stake_cents = 26
    pos.entry_contracts = 1
    pos.stop_loss_trigger_price = stop_loss_trigger_price(26, 12)  # 14

    sig = strat.evaluate(_tick(10, 16, "T20"))
    assert sig is not None
    assert sig.meta.get("phase") == "stop_loss"
    # limit_at_mid -> (10 + 16) // 2 == 13, untouched by the passive escalation.
    assert sig.limit_price == 13


def test_entry_fill_sets_stop_trigger_from_cents():
    strat = GreenUpStrategy(
        entry_max_price=25,
        hedge_trigger_price=68,
        stop_loss_cents=12,
    )
    strat.add_watch_ticker("T6")
    pos = strat.get_position("T6")
    pos.state = PositionState.WATCHING
    strat.on_fill({
        "ticker": "T6",
        "side": "yes",
        "price": 26,
        "size_cents": 26,
        "order_id": "oid-1",
    })
    assert pos.state == PositionState.ENTERED
    assert pos.hedge_trigger_price == 68
    assert pos.stop_loss_trigger_price == 14


def test_stop_loss_fill_on_yes_sell():
    strat = GreenUpStrategy(stop_loss_cents=12)
    strat.add_watch_ticker("T7")
    pos = strat.get_position("T7")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 24
    pos.entry_stake_cents = 24
    pos.entry_contracts = 1

    strat.on_fill({
        "ticker": "T7",
        "side": "yes",
        "action": "sell",
        "price": 9,
        "size_cents": 9,
        "contracts": 1,
        "order_id": "stop-1",
    })
    assert pos.state == PositionState.STOPPED


def test_stop_loss_fill_on_no_side_contra_label():
    """Kalshi reports a YES-sell as a fill on the contra ('no') side and often
    omits ``action``. Stop recognition must not depend on the reported side,
    otherwise the position stays stuck in STOPPING (the production reprice-loop
    bug)."""
    strat = GreenUpStrategy(stop_loss_cents=12)
    strat.add_watch_ticker("T7b")
    pos = strat.get_position("T7b")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 61
    pos.entry_stake_cents = 61
    pos.entry_contracts = 1
    pos.stop_order_id = "stop-42"

    strat.on_fill({
        "ticker": "T7b",
        "side": "no",          # contra label for a YES sell
        "price": 43,
        "size_cents": 43,
        "contracts": 1,
        "order_id": "stop-42",  # matches the resting stop order
    })
    assert pos.state == PositionState.STOPPED
    assert pos.stop_order_id == ""


def test_stop_fill_ignores_unrelated_order_id():
    """A fill for a different order id must not finalise the stop while one is
    resting (avoids a stray fill closing the wrong position)."""
    strat = GreenUpStrategy(stop_loss_cents=12)
    strat.add_watch_ticker("T7c")
    pos = strat.get_position("T7c")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 61
    pos.entry_stake_cents = 61
    pos.entry_contracts = 1
    pos.stop_order_id = "stop-99"

    strat.on_fill({
        "ticker": "T7c",
        "side": "no",
        "price": 43,
        "size_cents": 43,
        "contracts": 1,
        "order_id": "some-other-order",
    })
    assert pos.state == PositionState.STOPPING


def test_stop_reprices_when_bid_moves():
    strat = GreenUpStrategy(
        stop_loss_cents=12,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T8")
    pos = strat.get_position("T8")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 24
    pos.entry_stake_cents = 24
    pos.entry_contracts = 1
    pos.stop_order_id = "ord-1"
    pos.stop_limit_price = 10

    sig = strat.evaluate(_tick(8, 9, "T8"))
    assert sig is not None
    assert sig.limit_price == 8
    assert sig.meta.get("cancel_order_id") == "ord-1"


def test_full_green_hedges_at_trigger_on_micro_stake():
    """Prod-sized stake must hedge when bid >= trigger (no min locked-profit gate)."""
    from strategy.green_up_strategy import HedgeMode

    cfg.MAX_POSITION_CENTS = 100
    strat = GreenUpStrategy(
        entry_max_price=99,
        hedge_trigger_price=70,
        hedge_mode=HedgeMode.FULL_GREEN,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("NYK")
    pos = strat.get_position("NYK")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 49
    pos.entry_stake_cents = 49
    pos.hedge_trigger_price = 70

    sig = strat.evaluate(_tick(92, 93, "NYK"))
    assert sig is not None
    assert sig.side.value == "no"
    assert sig.meta.get("phase") == "hedge"
    assert strat.get_position("NYK").state == PositionState.HEDGING


def test_entry_fill_sets_relative_hedge_trigger_from_offset():
    strat = GreenUpStrategy(
        entry_max_price=25,
        hedge_offset_cents=26,
        stop_loss_cents=12,
    )
    strat.add_watch_ticker("T9")
    pos = strat.get_position("T9")
    pos.state = PositionState.WATCHING
    strat.on_fill({
        "ticker": "T9",
        "side": "yes",
        "price": 18,
        "size_cents": 18,
        "order_id": "oid-9",
    })
    assert pos.state == PositionState.ENTERED
    assert pos.hedge_trigger_price == 44
    assert pos.stop_loss_trigger_price == 6


def test_relative_hedge_fires_at_entry_plus_offset_not_absolute_trigger():
    from strategy.green_up_strategy import HedgeMode

    strat = GreenUpStrategy(
        entry_max_price=99,
        hedge_trigger_price=51,
        hedge_offset_cents=26,
        hedge_mode=HedgeMode.FULL_GREEN,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T10")
    pos = strat.get_position("T10")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 18
    pos.entry_stake_cents = 18
    pos.hedge_trigger_price = strat.hedge_trigger_for_entry(18)

    assert pos.hedge_trigger_price == 44
    assert strat.evaluate(_tick(43, 44, "T10")) is None

    sig = strat.evaluate(_tick(44, 45, "T10"))
    assert sig is not None
    assert sig.side.value == "no"
    assert sig.meta.get("phase") == "hedge"


def test_second_cycle_uses_new_entry_for_relative_hedge():
    strat = GreenUpStrategy(
        entry_max_price=25,
        hedge_offset_cents=26,
        entry_price_mode=EntryPriceMode.MARKET,
        max_cycles_per_ticker=0,
    )
    strat.add_watch_ticker("T11")
    pos = strat.get_position("T11")
    pos.state = PositionState.HEDGED
    pos.cycles_completed = 1
    pos.entry_price_cents = 25
    pos.hedge_trigger_price = 51

    sig = strat.evaluate(_tick(8, 10, "T11"))
    assert sig is not None
    assert sig.limit_price == 10

    new_pos = strat.get_position("T11")
    new_pos.state = PositionState.WATCHING
    strat.on_fill({
        "ticker": "T11",
        "side": "yes",
        "price": 10,
        "size_cents": 10,
        "order_id": "oid-11",
    })
    assert new_pos.hedge_trigger_price == 36


def test_entry_size_rounds_to_whole_contracts(monkeypatch):
    """Entry stake must be an exact multiple of the entry price so the logged
    preview (hedge size / locked profit) matches what actually fills. A
    sub-contract Kelly stake floors to one contract."""
    monkeypatch.setattr(cfg, "MAX_POSITION_CENTS", 100)
    monkeypatch.setattr(cfg, "KELLY_DIVISOR", 4)

    strat = GreenUpStrategy(
        entry_max_price=None,
        hedge_offset_cents=26,
        max_spread_cents=8,
        entry_price_mode=EntryPriceMode.MARKET,   # limit_price = best_ask
    )
    strat.add_watch_ticker("T25")
    sig = strat.evaluate(_tick(20, 22, "T25"))
    assert sig is not None
    assert sig.limit_price == 22
    # Kelly wants < 1 contract here -> floored to 1 contract == 22c.
    assert sig.size_cents == 22
    assert sig.size_cents % sig.limit_price == 0
    # Preview economics now reflect the real 22c stake (full-green at trigger 48).
    assert sig.meta["preview_locked_profit_cents"] == 26
    assert strat.get_position("T25").entry_stake_cents == 22


def test_entry_skipped_when_spread_too_wide():
    strat = GreenUpStrategy(
        entry_max_price=99,
        hedge_trigger_price=68,
        max_spread_cents=2,
        entry_price_mode=EntryPriceMode.MARKET,
    )
    strat.add_watch_ticker("T12")
    assert strat.evaluate(_tick(8, 12, "T12")) is None


def test_no_entry_max_allows_higher_ask():
    strat = GreenUpStrategy(
        entry_max_price=None,
        hedge_offset_cents=26,
        max_spread_cents=8,
        entry_price_mode=EntryPriceMode.MARKET,
    )
    strat.add_watch_ticker("T13")
    sig = strat.evaluate(_tick(30, 35, "T13"))
    assert sig is not None
    assert sig.limit_price == 35
    assert sig.meta["hedge_trigger_price"] == 61


def test_entry_skipped_when_hedge_would_hit_price_cap():
    strat = GreenUpStrategy(
        entry_max_price=None,
        hedge_offset_cents=26,
        max_spread_cents=8,
        entry_price_mode=EntryPriceMode.MARKET,
    )
    strat.add_watch_ticker("T14")
    assert strat.evaluate(_tick(74, 76, "T14")) is None


def test_resolve_entry_max_price():
    from strategy.green_up_strategy import resolve_entry_max_price

    assert resolve_entry_max_price(None) == 25
    assert resolve_entry_max_price(30) == 30
    assert resolve_entry_max_price(None, no_entry_max=True) is None
    assert resolve_entry_max_price(None, env_value="0") is None
    assert resolve_entry_max_price(None, env_value="none") is None
    assert resolve_entry_max_price(None, env_value="40") == 40


def test_resting_hedge_posts_gtc_no_at_trigger_price():
    from strategy.green_up_strategy import HedgeMode, HedgeStyle

    strat = GreenUpStrategy(
        entry_max_price=99,
        hedge_offset_cents=26,
        hedge_mode=HedgeMode.FULL_GREEN,
        hedge_style=HedgeStyle.RESTING,
    )
    strat.add_watch_ticker("T15")
    pos = strat.get_position("T15")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 25
    pos.entry_stake_cents = 25
    pos.hedge_trigger_price = 51

    sig = strat.evaluate(_tick(20, 22, "T15"))
    assert sig is not None
    assert sig.side.value == "no"
    assert sig.limit_price == 49
    assert sig.meta.get("time_in_force") == "gtc"
    assert sig.meta.get("hedge_style") == "resting"
    assert strat.get_position("T15").state == PositionState.HEDGING


def test_resting_hedge_not_re_emitted_while_on_book():
    from strategy.green_up_strategy import HedgeStyle

    strat = GreenUpStrategy(
        hedge_offset_cents=26,
        hedge_style=HedgeStyle.RESTING,
    )
    strat.add_watch_ticker("T16")
    pos = strat.get_position("T16")
    pos.state = PositionState.HEDGING
    pos.entry_price_cents = 25
    pos.entry_stake_cents = 25
    pos.hedge_trigger_price = 51
    pos.hedge_order_id = "hedge-1"
    pos.hedge_limit_price = 49

    assert strat.evaluate(_tick(20, 22, "T16")) is None


def test_stop_while_resting_hedge_cancels_hedge_order():
    from strategy.green_up_strategy import HedgeStyle

    strat = GreenUpStrategy(
        stop_loss_cents=10,
        hedge_style=HedgeStyle.RESTING,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T17")
    pos = strat.get_position("T17")
    pos.state = PositionState.HEDGING
    pos.entry_price_cents = 25
    pos.entry_stake_cents = 25
    pos.entry_contracts = 1
    pos.hedge_trigger_price = 51
    pos.hedge_order_id = "hedge-2"
    pos.hedge_limit_price = 49
    pos.stop_loss_trigger_price = 15

    sig = strat.evaluate(_tick(14, 15, "T17"))
    assert sig is not None
    assert sig.meta.get("phase") == "stop_loss"
    assert sig.meta.get("cancel_order_id") == "hedge-2"
    assert pos.hedge_order_id == ""


def test_rollback_stop_reverts_to_entered_on_first_fire_failure():
    """First stop fire never registered an order -> re-arm via ENTERED."""
    strat = GreenUpStrategy(stop_loss_cents=10)
    strat.add_watch_ticker("T21")
    pos = strat.get_position("T21")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 25
    pos.entry_stake_cents = 25
    pos.stop_order_id = ""
    pos.stop_limit_price = 0

    strat.rollback_stop("T21")
    assert pos.state == PositionState.ENTERED


def test_rollback_stop_keeps_resting_order_on_reprice_failure():
    """A live resting stop must be retained so retries target the same order,
    not stack a duplicate sell (the production order-leak fix)."""
    strat = GreenUpStrategy(stop_loss_cents=10)
    strat.add_watch_ticker("T22")
    pos = strat.get_position("T22")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 25
    pos.entry_stake_cents = 25
    pos.stop_order_id = "live-stop-1"
    pos.stop_limit_price = 9

    strat.rollback_stop("T22")
    assert pos.state == PositionState.STOPPING
    assert pos.stop_order_id == "live-stop-1"
    assert pos.stop_limit_price == 9


def test_rollback_stop_drops_dead_order_after_repeated_failures():
    """Repeated cancel failures mean the resting stop is gone (already filled or
    cancelled on the venue). Retrying forever spins thousands of dead cancels,
    so after MAX_STOP_CANCEL_FAILURES we drop the id and re-arm via ENTERED."""
    import strategy.green_up_strategy as gu

    strat = gu.GreenUpStrategy(stop_loss_cents=10)
    strat.add_watch_ticker("T24")
    pos = strat.get_position("T24")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 25
    pos.entry_stake_cents = 25
    pos.stop_order_id = "dead-stop-1"
    pos.stop_limit_price = 9

    for _ in range(gu.MAX_STOP_CANCEL_FAILURES - 1):
        strat.rollback_stop("T24")
        assert pos.state == PositionState.STOPPING
        assert pos.stop_order_id == "dead-stop-1"

    strat.rollback_stop("T24")  # threshold reached
    assert pos.state == PositionState.ENTERED
    assert pos.stop_order_id == ""
    assert pos.stop_cancel_failures == 0


def test_register_stop_order_clears_failure_counter():
    """A successfully accepted reprice resets the consecutive-failure counter so
    transient cancel failures never accumulate toward the dead-order cutoff."""
    strat = GreenUpStrategy(stop_loss_cents=10)
    strat.add_watch_ticker("T25")
    pos = strat.get_position("T25")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 25
    pos.stop_order_id = "stop-a"
    pos.stop_cancel_failures = 2

    strat.register_stop_order("T25", "stop-b", 9)
    assert pos.stop_order_id == "stop-b"
    assert pos.stop_cancel_failures == 0


def test_first_stop_logs_warning_even_when_cancelling_hedge(monkeypatch):
    """Regression: the first stop fire must log the WARNING even when it also
    cancels a resting hedge; only true reprices use the quieter info line."""
    import strategy.green_up_strategy as gu

    class _Recorder:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def _fn(*args, **kwargs):
                self.calls.append((name, args[0] if args else "", kwargs))
            return _fn

    rec = _Recorder()
    monkeypatch.setattr(gu, "logger", rec)

    strat = gu.GreenUpStrategy(
        stop_loss_cents=10,
        hedge_style=gu.HedgeStyle.RESTING,
        exit_price_mode=EntryPriceMode.PASSIVE,
    )
    strat.add_watch_ticker("T23")
    pos = strat.get_position("T23")
    pos.state = PositionState.HEDGING
    pos.entry_price_cents = 25
    pos.entry_stake_cents = 25
    pos.entry_contracts = 1
    pos.hedge_trigger_price = 51
    pos.hedge_order_id = "hedge-9"
    pos.hedge_limit_price = 49
    pos.stop_loss_trigger_price = 15

    sig = strat.evaluate(_tick(14, 15, "T23"))
    assert sig is not None
    assert sig.meta.get("cancel_order_id") == "hedge-9"
    assert any(
        name == "warning" and "stop-loss triggered" in msg
        for name, msg, _ in rec.calls
    )
    assert not any(
        name == "info" and "repricing" in msg for name, msg, _ in rec.calls
    )


def test_reprice_logs_info_not_warning(monkeypatch):
    """A genuine reprice of an on-book stop logs info, not the trigger warning."""
    import strategy.green_up_strategy as gu

    class _Recorder:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def _fn(*args, **kwargs):
                self.calls.append((name, args[0] if args else "", kwargs))
            return _fn

    rec = _Recorder()
    monkeypatch.setattr(gu, "logger", rec)

    strat = gu.GreenUpStrategy(
        stop_loss_cents=12,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T24")
    pos = strat.get_position("T24")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 24
    pos.entry_stake_cents = 24
    pos.entry_contracts = 1
    pos.stop_order_id = "ord-7"
    pos.stop_limit_price = 10

    sig = strat.evaluate(_tick(8, 9, "T24"))
    assert sig is not None
    assert any(
        name == "info" and "repricing" in msg for name, msg, _ in rec.calls
    )
    assert not any(name == "warning" for name, _, _ in rec.calls)


def test_evaluate_hedges_when_ask_missing_on_entered_position():
    """One-sided favourite books (bid only) must still fire hedge/stop logic."""
    strat = GreenUpStrategy(
        hedge_offset_cents=35,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T-OS")
    pos = strat.get_position("T-OS")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 63
    pos.entry_stake_cents = 63
    pos.entry_contracts = 1
    pos.hedge_trigger_price = 95

    sig = strat.evaluate(
        {"ticker": "T-OS", "best_bid": 96, "best_ask": None, "spread": None}
    )
    assert sig is not None
    assert sig.side.value == "no"
    assert sig.meta.get("phase") == "hedge"


def test_stop_loss_suppressed_by_time_gate_stays_entered():
    """Time gate blocks before STOPPING — no trigger spam / state flip."""
    from datetime import datetime, timedelta, timezone

    from discovery.market_client import MarketSummary
    from discovery.market_registry import register_markets

    cfg.STOP_LOSS_CLOSE_WINDOW_MINUTES = 5.0

    now = datetime.now(timezone.utc)
    register_markets([
        MarketSummary(
            ticker="T-GATE",
            event_ticker="EVT",
            title="Gate test",
            category="Sports",
            series_ticker="KXTEST",
            yes_bid=10,
            yes_ask=12,
            no_bid=88,
            no_ask=90,
            last_price=11,
            volume=100,
            volume_24h=100,
            open_interest=10,
            liquidity=1000,
            status="open",
            close_time=now + timedelta(hours=3),
            updated_at=now,
            result=None,
        )
    ])

    strat = GreenUpStrategy(
        stop_loss_cents=10,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T-GATE")
    pos = strat.get_position("T-GATE")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 20
    pos.entry_stake_cents = 20
    pos.entry_contracts = 1
    pos.stop_loss_trigger_price = stop_loss_trigger_price(20, 10)

    assert strat.evaluate(_tick(5, 6, "T-GATE")) is None
    assert pos.state == PositionState.ENTERED
    assert pos.stop_armed_at > 0


def test_stopping_without_order_id_retries_stop():
    strat = GreenUpStrategy(
        stop_loss_cents=10,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T-RETRY")
    pos = strat.get_position("T-RETRY")
    pos.state = PositionState.STOPPING
    pos.entry_price_cents = 20
    pos.entry_stake_cents = 20
    pos.entry_contracts = 1
    pos.stop_loss_trigger_price = stop_loss_trigger_price(20, 10)
    pos.stop_order_id = ""

    sig = strat.evaluate(_tick(5, 6, "T-RETRY"))
    assert sig is not None
    assert sig.meta.get("phase") == "stop_loss"
    assert pos.state == PositionState.STOPPING


def test_stop_escalates_to_market_on_floor_bid():
    strat = GreenUpStrategy(
        stop_loss_cents=10,
        exit_price_mode=EntryPriceMode.CROSS_SPREAD,
    )
    strat.add_watch_ticker("T-FLOOR")
    pos = strat.get_position("T-FLOOR")
    pos.state = PositionState.ENTERED
    pos.entry_price_cents = 20
    pos.entry_stake_cents = 20
    pos.entry_contracts = 1
    pos.stop_loss_trigger_price = stop_loss_trigger_price(20, 10)

    sig = strat.evaluate(_tick(1, 99, "T-FLOOR"))
    assert sig is not None
    assert sig.meta.get("order_type") == "market"
    assert sig.meta.get("time_in_force") == "ioc"
