"""Unit tests for strategy/price_targets.py."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.price_targets import (
    hedge_trigger_price,
    parse_hedge_offset_cents,
    stop_loss_trigger_price,
)


def test_hedge_trigger_price_adds_offset_and_caps():
    assert hedge_trigger_price(25, 26) == 51
    assert hedge_trigger_price(18, 26) == 44
    assert hedge_trigger_price(80, 26) == 99
    assert hedge_trigger_price(0, 26) == 0


def test_stop_loss_trigger_price_matches_green_up_behavior():
    assert stop_loss_trigger_price(26, 12) == 14
    assert stop_loss_trigger_price(5, 10) == 1


def test_parse_hedge_offset_cents():
    assert parse_hedge_offset_cents(None) is None
    assert parse_hedge_offset_cents(26) == 26
    assert parse_hedge_offset_cents("26") == 26
