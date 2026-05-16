"""Unit tests for discovery/market_math.py."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cfg = types.ModuleType("config")
cfg.FEE_PER_CONTRACT_CENTS = 7.0
cfg.HP_USE_FEE_ADJUSTED_ROI = True
cfg.HP_ASSUME_ROUND_TRIP_FEES = False
cfg.HP_MIN_ROI_PCT = 2.0
sys.modules["config"] = cfg

from discovery.market_math import (
    fee_adjusted_roi_if_yes_wins_pct,
    gross_roi_if_yes_wins_pct,
    passes_roi_gate,
)


def test_gross_roi_at_90c():
    assert abs(gross_roi_if_yes_wins_pct(90) - 11.111) < 0.01


def test_fee_adjusted_roi_at_90c():
    # net = (10 - 7) / (90 + 7) = 3.09%
    net = fee_adjusted_roi_if_yes_wins_pct(90, fee_per_contract_cents=7.0)
    assert 3.0 < net < 3.2


def test_fee_adjusted_stricter_than_gross():
    gross = gross_roi_if_yes_wins_pct(92)
    net = fee_adjusted_roi_if_yes_wins_pct(92, fee_per_contract_cents=7.0)
    assert net < gross


def test_passes_roi_gate_uses_fee_adjusted():
    ok, gross, applied = passes_roi_gate(90, min_roi_pct=2.0)
    assert gross > 10
    assert applied < gross
    assert ok is True


def test_passes_roi_gate_rejects_expensive_ask():
    ok, _, applied = passes_roi_gate(99, min_roi_pct=2.0)
    assert ok is False
    assert applied < 2.0
