from discovery.market_client import MarketClient


def test_period_seconds_to_interval():
    assert MarketClient._period_seconds_to_interval(60) == 1
    assert MarketClient._period_seconds_to_interval(300) == 1
    assert MarketClient._period_seconds_to_interval(3600) == 60
    assert MarketClient._period_seconds_to_interval(86400) == 1440


def test_parse_candlestick():
    raw = {
        "end_period_ts": 1779573600,
        "volume_fp": "734883.33",
        "price": {
            "open_dollars": "0.4500",
            "high_dollars": "0.4500",
            "low_dollars": "0.4400",
            "close_dollars": "0.4500",
        },
    }
    candle = MarketClient._parse_candlestick(raw)
    assert candle == {
        "ts": 1779573600,
        "open": 45,
        "high": 45,
        "low": 44,
        "close": 45,
        "volume": 734883,
    }
