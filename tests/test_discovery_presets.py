"""Unit tests for discovery presets and fee-adjusted ranking."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cfg = types.ModuleType("config")
cfg.LOG_LEVEL = "INFO"
cfg.LOG_FILE = "test.jsonl"
cfg.FEE_PER_CONTRACT_CENTS = 7.0
cfg.HP_USE_FEE_ADJUSTED_ROI = True
cfg.HP_MIN_ROI_PCT = 2.0
sys.modules["config"] = cfg

log_mod = types.ModuleType("logging_.structured_logger")


class _StubLogger:
    def __getattr__(self, _):
        return lambda *a, **kw: None


log_mod.logger = _StubLogger()
sys.modules["logging_"] = types.ModuleType("logging_")
sys.modules["logging_.structured_logger"] = log_mod

from discovery.discovery_presets import apply_preset, preset_for_strategy
from discovery.market_client import MarketClient
from discovery.ticker_selector import TickerCriteria, select_tickers


def _market(ticker: str, *, volume_24h: int, yes_ask: int) -> object:
    raw = {
        "ticker": ticker,
        "event_ticker": "EVT",
        "title": ticker,
        "_category_override": "Sports",
        "series_ticker": "SER",
        "yes_bid": yes_ask - 2,
        "yes_ask": yes_ask,
        "volume_24h": volume_24h,
        "volume": volume_24h,
        "open_interest": 100,
        "liquidity": 1000,
        "status": "open",
    }
    return MarketClient._parse_market(raw)


def test_preset_for_strategy():
    assert preset_for_strategy("high_prob") == "high_prob"
    assert preset_for_strategy("kelly") == "kelly"
    assert preset_for_strategy("unknown") is None


def test_apply_high_prob_preset():
    base = TickerCriteria(category="Politics")
    merged = apply_preset(base, "high_prob")
    assert merged.min_yes_ask == 85
    assert merged.max_yes_ask == 97
    assert merged.rank_by == "fee_adjusted_roi"
    assert merged.min_fee_adjusted_roi_pct == 1.5
    assert merged.preset_name == "high_prob"


def test_explicit_fields_not_overwritten():
    base = TickerCriteria(category="Politics", min_yes_ask=80)
    merged = apply_preset(base, "high_prob", skip_fields=frozenset({"min_yes_ask"}))
    assert merged.min_yes_ask == 80
    assert merged.max_yes_ask == 97


def test_rank_by_fee_adjusted_roi():
    markets = [
        _market("LOW-ROI-HIGH-VOL", volume_24h=9000, yes_ask=96),
        _market("BETTER-ROI", volume_24h=1000, yes_ask=88),
    ]
    criteria = TickerCriteria(
        category="Sports",
        top_n=2,
        min_yes_ask=85,
        max_yes_ask=97,
        tradeable_only=False,
        rank_by="fee_adjusted_roi",
    )
    tickers = select_tickers(markets, criteria)
    assert tickers[0] == "BETTER-ROI"
