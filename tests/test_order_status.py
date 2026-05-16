import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.order_entry import OrderStatus, OrderEntry


def test_from_kalshi_executed_maps_to_filled():
    assert OrderStatus.from_kalshi("executed") == OrderStatus.FILLED


def test_from_kalshi_canceled():
    assert OrderStatus.from_kalshi("canceled") == OrderStatus.CANCELLED


def test_parse_order_status_fill_count_fp():
    detail = OrderEntry._parse_order_status(
        {
            "order_id": "x",
            "ticker": "T",
            "status": "executed",
            "fill_count_fp": "1.00",
            "initial_count_fp": "1.00",
            "type": "market",
            "side": "yes",
        }
    )
    assert detail.status == OrderStatus.FILLED
    assert detail.filled_count == 1
    assert detail.count == 1
