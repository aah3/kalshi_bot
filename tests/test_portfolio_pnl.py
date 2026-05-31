"""
tests/test_portfolio_pnl.py

Realised P&L accounting from Kalshi fills (PortfolioMonitor helper).
Replaces the old `is_taker` placeholder with average-cost matching.
"""

from trading.portfolio_monitor import realized_pnl_cents_from_fills


def _fill(ticker, side, action, count, yes_price, created_time):
    return {
        "ticker": ticker,
        "side": side,
        "action": action,
        "count": count,
        "yes_price": yes_price,
        "created_time": created_time,
    }


def test_no_fills_is_zero():
    assert realized_pnl_cents_from_fills([]) == 0


def test_open_only_no_realized_pnl():
    # A buy with no closing sell realises nothing.
    fills = [_fill("MKT", "yes", "buy", 10, 60, "2026-05-29T10:00:00Z")]
    assert realized_pnl_cents_from_fills(fills) == 0


def test_round_trip_profit_yes():
    # Buy 10 YES @60, sell 10 YES @73 → +13c × 10 = +130c.
    fills = [
        _fill("MKT", "yes", "buy", 10, 60, "2026-05-29T10:00:00Z"),
        _fill("MKT", "yes", "sell", 10, 73, "2026-05-29T10:05:00Z"),
    ]
    assert realized_pnl_cents_from_fills(fills) == 130


def test_round_trip_loss_yes():
    # Buy 5 YES @80, sell 5 YES @72 → -8c × 5 = -40c.
    fills = [
        _fill("MKT", "yes", "buy", 5, 80, "2026-05-29T10:00:00Z"),
        _fill("MKT", "yes", "sell", 5, 72, "2026-05-29T10:05:00Z"),
    ]
    assert realized_pnl_cents_from_fills(fills) == -40


def test_no_side_round_trip():
    # NO leg: normalize_fill_message prices NO = 100 - yes_price.
    # Buy 4 NO @ (100-60)=40, sell 4 NO @ (100-45)=55 → +15c × 4 = +60c.
    fills = [
        _fill("MKT", "no", "buy", 4, 60, "2026-05-29T10:00:00Z"),
        _fill("MKT", "no", "sell", 4, 45, "2026-05-29T10:05:00Z"),
    ]
    assert realized_pnl_cents_from_fills(fills) == 60


def test_weighted_average_cost():
    # Buy 10 @60 and 10 @70 → avg 65; sell 20 @75 → +10c × 20 = +200c.
    fills = [
        _fill("MKT", "yes", "buy", 10, 60, "2026-05-29T10:00:00Z"),
        _fill("MKT", "yes", "buy", 10, 70, "2026-05-29T10:01:00Z"),
        _fill("MKT", "yes", "sell", 20, 75, "2026-05-29T10:05:00Z"),
    ]
    assert realized_pnl_cents_from_fills(fills) == 200


def test_partial_close_uses_avg_cost():
    # Buy 10 @60, sell only 4 @70 → +10c × 4 = +40c (remaining 6 stay open).
    fills = [
        _fill("MKT", "yes", "buy", 10, 60, "2026-05-29T10:00:00Z"),
        _fill("MKT", "yes", "sell", 4, 70, "2026-05-29T10:05:00Z"),
    ]
    assert realized_pnl_cents_from_fills(fills) == 40


def test_sell_beyond_known_position_is_ignored():
    # Closing buy predates the fills window: only the part we can match to a
    # tracked long is realised; the excess is conservatively ignored.
    fills = [
        _fill("MKT", "yes", "buy", 3, 60, "2026-05-29T10:00:00Z"),
        _fill("MKT", "yes", "sell", 10, 80, "2026-05-29T10:05:00Z"),
    ]
    # Only 3 contracts matched: +20c × 3 = +60c. The other 7 are ignored.
    assert realized_pnl_cents_from_fills(fills) == 60


def test_unordered_fills_sorted_by_time():
    # Sell appears before its opening buy in the input list; sorting by
    # created_time must restore buy → sell order.
    fills = [
        _fill("MKT", "yes", "sell", 10, 73, "2026-05-29T10:05:00Z"),
        _fill("MKT", "yes", "buy", 10, 60, "2026-05-29T10:00:00Z"),
    ]
    assert realized_pnl_cents_from_fills(fills) == 130


def test_independent_tickers_and_sides():
    fills = [
        _fill("AAA", "yes", "buy", 10, 50, "2026-05-29T10:00:00Z"),
        _fill("AAA", "yes", "sell", 10, 55, "2026-05-29T10:01:00Z"),   # +50c
        _fill("BBB", "no", "buy", 5, 70, "2026-05-29T10:02:00Z"),       # NO @30
        _fill("BBB", "no", "sell", 5, 60, "2026-05-29T10:03:00Z"),      # NO @40 → +10c×5
    ]
    assert realized_pnl_cents_from_fills(fills) == 50 + 50
