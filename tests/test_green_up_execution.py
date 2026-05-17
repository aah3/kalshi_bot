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
from strategy.execution_price import resolve_no_buy, resolve_yes_buy, resolve_yes_sell
from strategy.green_up_strategy import GreenUpStrategy, PositionState
from strategy.execution_price import EntryPriceMode


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
