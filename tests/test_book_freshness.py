"""Stale book detection, REST snapshot load, and monitor quote selection."""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.orderbook_parse import OrderBookSnapshot
from ingestion.book_freshness import (
    book_age_seconds,
    book_needs_rest_refresh,
    is_book_crossed,
    is_book_stale,
)
from ingestion.market_ingestor import OrderBook, MarketIngestor
from monitoring.session_table import _book_quotes


def test_book_age_and_stale():
    book = OrderBook(ticker="T")
    assert book_age_seconds(book) is None

    book.updated_at_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000) - 90_000_000
    age = book_age_seconds(book)
    assert age is not None
    assert 89 <= age <= 91
    assert is_book_stale(book, 60)
    assert not is_book_stale(book, 120)


def test_is_book_crossed():
    book = OrderBook(ticker="T")
    assert not is_book_crossed(book)

    book.yes_bids[46] = 10
    book.yes_asks[28] = 10
    assert is_book_crossed(book)

    book.yes_asks.clear()
    book.yes_asks[47] = 10
    assert not is_book_crossed(book)


def test_book_needs_rest_refresh_crossed_even_when_fresh():
    book = OrderBook(ticker="T")
    book.yes_bids[46] = 10
    book.yes_asks[28] = 10
    book.updated_at_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)

    assert book_needs_rest_refresh(book, 60)
    assert not is_book_stale(book, 60)


def test_load_rest_snapshot():
    book = OrderBook(ticker="T")
    book.yes_bids[25] = 100
    book.yes_asks[26] = 100
    book.updated_at_us = 1

    snap = OrderBookSnapshot(
        ticker="T",
        fetched_at=datetime.now(timezone.utc),
        yes_bids=[(3, 500), (2, 100)],
        yes_asks=[(4, 800), (5, 200)],
        best_bid=3,
        best_ask=4,
        spread=1,
        mid_price=3.5,
    )
    assert book.load_rest_snapshot(snap)
    assert book.best_bid == 3
    assert book.best_ask == 4
    assert book.updated_at_us > 1


def test_ingestor_apply_rest_and_push_tick():
    ticks: list[dict] = []

    async def on_tick(tick):
        ticks.append(tick)

    async def _run():
        ingestor = MarketIngestor(tickers=["T"], on_tick=on_tick)
        book = ingestor.get_book("T")
        assert book is not None
        book.yes_bids[25] = 1
        book.yes_asks[26] = 1
        book.updated_at_us = 1

        snap = OrderBookSnapshot(
            ticker="T",
            fetched_at=datetime.now(timezone.utc),
            yes_bids=[(14, 10)],
            yes_asks=[(15, 10)],
            best_bid=14,
            best_ask=15,
            spread=1,
            mid_price=14.5,
        )
        assert ingestor.apply_rest_snapshot(snap)
        ingestor.push_tick("T", event_type="rest_refresh")
        await asyncio.sleep(0.05)
        assert len(ticks) == 1
        assert ticks[0]["best_bid"] == 14
        assert ticks[0]["event_type"] == "rest_refresh"

    import asyncio
    asyncio.run(_run())


def test_monitor_prefers_rest_when_ws_stale():
    ingestor = MarketIngestor(tickers=["T"], on_tick=lambda t: None)
    book = ingestor.get_book("T")
    assert book is not None
    book.yes_bids[25] = 1
    book.yes_asks[26] = 1
    book.updated_at_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000) - 120_000_000

    rest = OrderBookSnapshot(
        ticker="T",
        fetched_at=datetime.now(timezone.utc),
        yes_bids=[(3, 1)],
        yes_asks=[(4, 1)],
        best_bid=3,
        best_ask=4,
        spread=1,
        mid_price=3.5,
    )

    bid, ask, mid, spread = _book_quotes(
        "T", ingestor, {"T": rest}, stale_seconds=60
    )
    assert bid == 3
    assert ask == 4
    assert mid == 3.5

    bid2, ask2, _, _ = _book_quotes("T", ingestor, {"T": rest}, stale_seconds=300)
    assert bid2 == 25
    assert ask2 == 26


def test_monitor_prefers_rest_when_ws_crossed():
    ingestor = MarketIngestor(tickers=["T"], on_tick=lambda t: None)
    book = ingestor.get_book("T")
    assert book is not None
    book.yes_bids[46] = 1
    book.yes_asks[28] = 1
    book.updated_at_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)

    rest = OrderBookSnapshot(
        ticker="T",
        fetched_at=datetime.now(timezone.utc),
        yes_bids=[(27, 1)],
        yes_asks=[(28, 1)],
        best_bid=27,
        best_ask=28,
        spread=1,
        mid_price=27.5,
    )

    bid, ask, mid, spread = _book_quotes(
        "T", ingestor, {"T": rest}, stale_seconds=300
    )
    assert bid == 27
    assert ask == 28
    assert mid == 27.5
    assert spread == 1
