import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.order_entry import kalshi_order_count_fields


def test_kalshi_order_count_fields_whole_contracts():
    assert kalshi_order_count_fields(3) == (3, "3.00")
    assert kalshi_order_count_fields(10.0) == (10, "10.00")


def test_kalshi_order_count_fields_fractional():
    assert kalshi_order_count_fields(0.69) == (0, "0.69")
    assert kalshi_order_count_fields(0.31) == (0, "0.31")
    assert kalshi_order_count_fields(27.31) == (27, "27.31")
