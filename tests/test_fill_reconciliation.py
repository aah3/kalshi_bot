"""WS liveness + REST fill-reconciliation safety net.

Covers the three hardening changes that prevent the "fills silently not captured"
failure mode:
  A. MarketIngestor.force_reconnect tears down a stalled socket.
  B. run_fill_reconciliation_loop recovers fills the WS missed (and skips others).
  C. run_rest_book_fallback_loop escalates persistent staleness to a WS reconnect.
Plus normalize_fill_message handling REST (/portfolio/fills) payloads.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.orderbook_parse import OrderBookSnapshot
from ingestion.market_ingestor import MarketIngestor, OrderBook, normalize_fill_message
from ingestion.rest_book_fallback import run_rest_book_fallback_loop
from trading.fill_reconciler import _reconcile_once, run_fill_reconciliation_loop


# ── normalize_fill_message: REST payload shape ───────────────────────────────

def test_normalize_rest_sell_fill():
    raw = {
        "trade_id": "TR-1",
        "order_id": "OID-1",
        "ticker":   "KXNBAGAME-26MAY28OKCSAS-SAS",   # REST uses `ticker`
        "side":     "yes",
        "action":   "sell",
        "count":    1,
        "yes_price": 73,
        "no_price":  27,
        "is_taker": False,
    }
    fill = normalize_fill_message(raw)
    assert fill["ticker"] == "KXNBAGAME-26MAY28OKCSAS-SAS"
    assert fill["side"] == "yes"
    assert fill["price"] == 73
    assert fill["contracts"] == 1
    assert fill["action"] == "sell"
    assert fill["order_id"] == "OID-1"
    assert fill["trade_id"] == "TR-1"


def test_normalize_ws_fill_still_works():
    raw = {
        "market_ticker": "KXNBAGAME-26MAY28OKCSAS-SAS",
        "side": "yes",
        "yes_price": 70,
        "count": 1,
        "order_id": "OID-2",
    }
    fill = normalize_fill_message(raw)
    assert fill["ticker"] == "KXNBAGAME-26MAY28OKCSAS-SAS"
    assert fill["price"] == 70


# ── B: fill reconciliation routing ───────────────────────────────────────────

def test_reconciler_recovers_tracked_order_only():
    routed: list[dict] = []

    async def fetch_fills():
        return [
            {"trade_id": "T1", "order_id": "MINE", "ticker": "X",
             "side": "yes", "action": "sell", "count": 1, "yes_price": 73},
            {"trade_id": "T2", "order_id": "NOT-MINE", "ticker": "Y",
             "side": "yes", "action": "buy", "count": 1, "yes_price": 50},
        ]

    async def _run():
        n = await _reconcile_once(
            fetch_fills=fetch_fills,
            on_fill=routed.append,
            known_order_ids=lambda: {"MINE"},
            fills_limit=100,
        )
        return n

    recovered = asyncio.run(_run())
    assert recovered == 1
    assert len(routed) == 1
    assert routed[0]["order_id"] == "MINE"
    assert routed[0]["action"] == "sell"


def test_reconciler_noop_without_known_orders():
    async def fetch_fills():
        raise AssertionError("should not fetch when nothing is tracked")

    async def _run():
        return await _reconcile_once(
            fetch_fills=fetch_fills,
            on_fill=lambda f: None,
            known_order_ids=lambda: set(),
            fills_limit=100,
        )

    assert asyncio.run(_run()) == 0


def test_reconciler_loop_disabled_when_interval_zero():
    async def _run():
        await run_fill_reconciliation_loop(
            fetch_fills=lambda: asyncio.sleep(0, result=[]),
            on_fill=lambda f: None,
            known_order_ids=lambda: {"MINE"},
            shutdown_event=asyncio.Event(),
            interval_seconds=0,
        )

    asyncio.run(_run())  # returns immediately, no hang


# ── A: forced reconnect closes the live socket ───────────────────────────────

def test_force_reconnect_closes_socket():
    class _FakeWS:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def _run():
        ing = MarketIngestor(tickers=["T"], on_tick=lambda t: None)
        ws = _FakeWS()
        ing._ws = ws
        await ing.force_reconnect("test")
        return ws.closed

    assert asyncio.run(_run()) is True


def test_force_reconnect_safe_when_no_socket():
    async def _run():
        ing = MarketIngestor(tickers=["T"], on_tick=lambda t: None)
        await ing.force_reconnect("test")  # _ws is None — must not raise

    asyncio.run(_run())


# ── C: persistent staleness escalates to a WS reconnect ──────────────────────

def _stale_book(ticker: str) -> OrderBook:
    book = OrderBook(ticker=ticker)
    book.yes_bids[25] = 1
    book.yes_asks[26] = 1
    # 120s old → stale against any reasonable threshold
    book.updated_at_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000) - 120_000_000
    return book


def test_rest_fallback_escalates_to_reconnect():
    shutdown = asyncio.Event()

    class _FakeIngestor:
        def __init__(self):
            self._book = _stale_book("T")
            self.reconnects: list[str] = []

        @property
        def tickers(self):
            return ["T"]

        def get_book(self, ticker):
            return self._book

        def apply_rest_snapshot(self, snap):
            return True

        def push_tick(self, ticker, event_type="rest_refresh"):
            pass

        async def force_reconnect(self, reason):
            self.reconnects.append(reason)
            shutdown.set()   # stop the loop deterministically after escalation

    class _FakeClient:
        async def get_order_book(self, ticker, depth=5):
            return OrderBookSnapshot(
                ticker=ticker,
                fetched_at=datetime.now(timezone.utc),
                yes_bids=[(25, 1)],
                yes_asks=[(26, 1)],
                best_bid=25,
                best_ask=26,
                spread=1,
                mid_price=25.5,
            )

    ing = _FakeIngestor()

    async def _run():
        await run_rest_book_fallback_loop(
            ingestor=ing,
            market_client=_FakeClient(),
            shutdown_event=shutdown,
            poll_seconds=0.01,
            stale_seconds=60,
            escalate_after_polls=2,
        )

    asyncio.run(asyncio.wait_for(_run(), timeout=5))
    assert ing.reconnects == ["rest_fallback_stale_escalation"]


# ── Idempotency: WS + REST paths never double-apply a fill ───────────────────

def test_on_fill_received_is_idempotent():
    import main

    main._processed_fill_ids.clear()
    fill = {
        "trade_id": "DUP-1", "order_id": "O", "ticker": "X",
        "side": "yes", "price": 73, "contracts": 1, "action": "sell",
    }
    main.on_fill_received(fill)   # WS delivery
    main.on_fill_received(fill)   # REST reconciliation re-delivery (must be ignored)
    assert len(main._processed_fill_ids) == 1

