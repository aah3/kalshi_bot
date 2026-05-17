"""WebSocket orderbook parsing for MarketIngestor (Kalshi FP format)."""

from ingestion.market_ingestor import OrderBook


def test_ws_snapshot_fp_format():
    book = OrderBook(ticker="TEST")
    ok = book.load_ws_snapshot({
        "market_ticker": "TEST",
        "yes_dollars_fp": [["0.05", "100.00"], ["0.04", "50.00"]],
        "no_dollars_fp": [["0.94", "80.00"]],
    })
    assert ok
    assert book.best_bid == 5
    assert book.best_ask == 6  # 100 - 94
    assert book.spread == 1


def test_ws_delta_yes_and_no():
    book = OrderBook(ticker="TEST")
    book.load_ws_snapshot({
        "yes_dollars_fp": [["0.05", "10.00"]],
        "no_dollars_fp": [["0.94", "10.00"]],
    })

    assert book.apply_ws_delta({
        "price_dollars": "0.06",
        "delta_fp": "5.00",
        "side": "yes",
    })
    assert book.yes_bids.get(6) == 5

    assert book.apply_ws_delta({
        "price_dollars": "0.93",
        "delta_fp": "3.00",
        "side": "no",
    })
    assert book.yes_asks.get(7) == 3  # 100 - 93


def test_ws_legacy_batch_delta():
    book = OrderBook(ticker="TEST")
    book.yes_bids[10] = 5
    assert book.apply_ws_delta({
        "deltas": [{"side": "yes_bid", "price": 10, "delta": -5}],
    })
    assert 10 not in book.yes_bids
