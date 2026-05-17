"""Tests for Kalshi orderbook_fp parsing."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.orderbook_parse import (
    market_order_yes_price,
    parse_orderbook_response,
    worst_market_fill_price,
)


def test_orderbook_fp_yes_no_dollars():
    data = {
        "orderbook_fp": {
            "yes_dollars": [["0.0600", "100.00"], ["0.1000", "50.00"]],
            "no_dollars": [["0.8500", "10.00"], ["0.8900", "20.00"]],
        }
    }
    book = parse_orderbook_response(data, "TEST-TICKER")
    assert book is not None
    assert book.best_bid == 10
    assert book.best_ask == 11   # 100 - 89
    assert book.spread == 1
    assert book.mid_price == 10.5


def test_worst_market_fill_price_single_level():
    data = {
        "orderbook_fp": {
            "yes_dollars": [["0.0700", "5.00"]],
            "no_dollars": [["0.9100", "10.00"]],
        }
    }
    book = parse_orderbook_response(data, "T")
    assert worst_market_fill_price(book, "yes", 1) == 9
    assert worst_market_fill_price(book, "no", 1) == 93


def test_worst_market_fill_price_walks_ladder():
    data = {
        "orderbook": {
            "yes": {
                "bids": [{"price": 50, "quantity": 10}],
                "asks": [
                    {"price": 8, "quantity": 1},
                    {"price": 12, "quantity": 5},
                ],
            }
        }
    }
    book = parse_orderbook_response(data, "T")
    assert worst_market_fill_price(book, "yes", 1) == 8
    assert worst_market_fill_price(book, "yes", 2) == 12


def test_market_sell_uses_bid_floor_not_ask_cap():
    """Sell YES must not use buy-side ask cap (would sit above bid and IOC-cancel)."""
    data = {
        "orderbook": {
            "yes": {
                "bids": [{"price": 8, "quantity": 200}],
                "asks": [{"price": 10, "quantity": 200}],
            }
        }
    }
    book = parse_orderbook_response(data, "T")
    assert worst_market_fill_price(book, "yes", 150) == 10
    assert market_order_yes_price(book, "yes", "sell", 150) == 8
    assert market_order_yes_price(book, "yes", "buy", 150) == 10


def test_websocket_nested_shape():
    data = {
        "orderbook": {
            "yes": {
                "bids": [{"price": 32, "quantity": 5}],
                "asks": [{"price": 34, "quantity": 3}],
            }
        }
    }
    book = parse_orderbook_response(data, "T")
    assert book.best_bid == 32
    assert book.best_ask == 34


if __name__ == "__main__":
    test_orderbook_fp_yes_no_dollars()
    test_websocket_nested_shape()
    print("ok")
