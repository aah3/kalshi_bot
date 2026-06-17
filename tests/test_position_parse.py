import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.position_parse import format_contract_qty, parse_market_position


def test_position_fp_yes_three_contracts():
    raw = {
        "ticker": "KXPGATOP20-PGC26-SLOW",
        "position_fp": "3.00",
        "market_exposure_dollars": "0.290000",
        "total_traded_dollars": "0.290000",
    }
    p = parse_market_position(raw)
    assert p is not None
    assert p["side"] == "yes"
    assert p["contracts"] == 3
    assert p["cost_basis_cents"] == 29
    assert p["avg_entry_price"] == round(29 / 3)


def test_position_fp_negative_is_no():
    raw = {
        "ticker": "T",
        "position_fp": "-2.00",
        "market_exposure_dollars": "1.200000",
    }
    p = parse_market_position(raw)
    assert p["side"] == "no"
    assert p["contracts"] == 2
    assert p["avg_entry_price"] == 60


def test_legacy_position_and_side():
    raw = {
        "ticker": "T",
        "position": 1,
        "side": "yes",
        "market_exposure": 9,
    }
    p = parse_market_position(raw)
    assert p["contracts"] == 1
    assert p["avg_entry_price"] == 9


def test_zero_position_skipped():
    assert parse_market_position({"ticker": "T", "position_fp": "0.00"}) is None


def test_position_fp_fractional_no_not_rounded_up():
    raw = {
        "ticker": "KXLIUSAELIMINATIONW-26JUN12-BEA",
        "position_fp": "-0.69",
        "market_exposure_dollars": "0.462300",
    }
    p = parse_market_position(raw)
    assert p is not None
    assert p["side"] == "no"
    assert p["contracts"] == 0.69
    assert p["contracts_fp"] == 0.69
    assert p["avg_entry_price"] == 67


def test_position_fp_fractional_yes():
    raw = {
        "ticker": "T",
        "position_fp": "0.31",
        "market_exposure_dollars": "0.105400",
    }
    p = parse_market_position(raw)
    assert p is not None
    assert p["side"] == "yes"
    assert p["contracts"] == 0.31
    assert p["avg_entry_price"] == round(11 / 0.31)


def test_format_contract_qty_whole_and_fractional():
    assert format_contract_qty(3.0) == "3"
    assert format_contract_qty(0.69) == "0.69"
    assert format_contract_qty(27.31) == "27.31"
