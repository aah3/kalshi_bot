"""Tests for strategy/book_normalize.py."""

from strategy.book_normalize import normalize_tick_sides


def test_normalize_fills_missing_ask():
    tick = {"ticker": "T", "best_bid": 96, "best_ask": None}
    out = normalize_tick_sides(tick)
    assert out is not None
    assert out["best_ask"] == 97
    assert out["spread"] == 1


def test_normalize_one_sided_favourite():
    tick = {"ticker": "T", "best_bid": 99, "best_ask": None}
    out = normalize_tick_sides(tick)
    assert out["best_ask"] == 99


def test_normalize_returns_none_without_bid():
    assert normalize_tick_sides({"ticker": "T", "best_ask": 50}) is None


def test_normalize_passes_through_two_sided():
    tick = {"ticker": "T", "best_bid": 40, "best_ask": 42}
    assert normalize_tick_sides(tick) is tick
