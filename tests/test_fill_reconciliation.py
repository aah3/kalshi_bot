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


# ── Closing fills realise P&L on the entry leg (no phantom new position) ──────

def test_stop_fill_closes_entry_leg_with_realised_pnl():
    """Regression for the production bug where a stop fill (reported on the
    contra 'no' side) was booked as a brand-new open leg, leaving the entry leg
    perpetually open with no realised P&L and a phantom extra contract.

    A stop fill must now CLOSE the entry leg at the fill price and roll the
    realised loss up into the parent trade."""
    import config
    import main
    from metrics.blotter import Blotter
    from strategy.green_up_strategy import GreenUpStrategy, PositionState

    saved = (
        main._blotter, main._strategy, main._store, main._execution,
        main._circuit_breaker, main._alert_manager,
        dict(main._active_trades), dict(main._pending_orders),
    )
    try:
        ticker = "KXTEST-CLOSE-LEG"
        blotter = Blotter(":memory:")
        strat   = GreenUpStrategy(stop_loss_cents=10)
        strat.add_watch_ticker(ticker)

        main._blotter         = blotter
        main._strategy        = strat
        main._store           = None
        main._execution       = None
        main._circuit_breaker = None
        main._alert_manager   = None
        main._active_trades   = {}
        main._pending_orders  = {}
        main._processed_fill_ids.clear()

        # 1) Entry fill: buy 1 YES @ 61c -> opens trade + entry leg.
        # A pending entry order puts the position in WATCHING (real flow).
        strat.get_position(ticker).state = PositionState.WATCHING
        main._pending_orders["entry-1"] = {
            "ticker": ticker, "trade_type": "entry", "meta": {},
            "strategy": "green_up_full_green_market", "category": "Sports",
            "side": "yes",
        }
        main.on_fill_received({
            "trade_id": "E1", "order_id": "entry-1", "ticker": ticker,
            "side": "yes", "action": "buy", "price": 61, "size_cents": 61,
            "contracts": 1,
        })
        assert strat.get_position(ticker).state == PositionState.ENTERED
        trade_id = main._active_trades[ticker]
        legs = blotter.query_legs(parent_trade_id=trade_id)
        assert len(legs) == 1 and legs[0].status == "open"

        # 2) Stop fires: arm the resting stop, then it fills on the 'no' side.
        pos = strat.get_position(ticker)
        pos.state = PositionState.STOPPING
        pos.stop_order_id = "stop-1"
        main._pending_orders["stop-1"] = {
            "ticker": ticker, "trade_type": "stop_loss", "meta": {},
            "strategy": "green_up_full_green_market", "category": "Sports",
            "side": "yes",
        }
        main.on_fill_received({
            "trade_id": "S1", "order_id": "stop-1", "ticker": ticker,
            "side": "no", "price": 43, "size_cents": 43, "contracts": 1,
        })

        # Strategy finalised the stop; trade is no longer active.
        assert strat.get_position(ticker).state == PositionState.STOPPED
        assert ticker not in main._active_trades

        # Still exactly ONE leg — the entry leg, now CLOSED with realised P&L.
        legs = blotter.query_legs(parent_trade_id=trade_id)
        assert len(legs) == 1
        entry_leg = legs[0]
        assert entry_leg.status == "closed"
        assert entry_leg.exit_price == 43
        expected_fee = int(config.FEE_PER_CONTRACT_CENTS * 1)
        assert entry_leg.realised_pnl_cents == (43 - 61) * 1 - expected_fee

        # Parent trade closed and net P&L reflects the realised loss.
        parent = blotter.query_trades()
        parent = [p for p in parent if p.trade_id == trade_id][0]
        assert parent.status == "closed"
        assert parent.net_pnl_cents == (43 - 61) * 1 - expected_fee
        assert parent.total_contracts == 1   # no phantom second contract
    finally:
        (
            main._blotter, main._strategy, main._store, main._execution,
            main._circuit_breaker, main._alert_manager,
            main._active_trades, main._pending_orders,
        ) = saved


def test_hedge_fill_marks_trade_hedged_not_closed():
    """Regression: hedge fill must mark the parent ``hedged`` and keep both
    legs open for settlement — not ``close_trade`` with net_pnl=0."""
    import main
    from metrics.blotter import Blotter
    from strategy.green_up_strategy import GreenUpStrategy, PositionState

    saved = (
        main._blotter, main._strategy, main._store, main._execution,
        main._circuit_breaker, main._alert_manager,
        dict(main._active_trades), dict(main._pending_orders),
    )
    try:
        ticker = "KXTEST-HEDGE-LEG"
        blotter = Blotter(":memory:")
        strat   = GreenUpStrategy(hedge_offset_cents=26)
        strat.add_watch_ticker(ticker)

        main._blotter         = blotter
        main._strategy        = strat
        main._store           = None
        main._execution       = None
        main._circuit_breaker = None
        main._alert_manager   = None
        main._active_trades   = {}
        main._pending_orders  = {}
        main._processed_fill_ids.clear()

        pos = strat.get_position(ticker)
        pos.state = PositionState.WATCHING
        main._pending_orders["entry-1"] = {
            "ticker": ticker, "trade_type": "entry", "meta": {},
            "strategy": "green_up_full_green_market", "category": "Sports",
            "side": "yes",
        }
        main.on_fill_received({
            "trade_id": "E1", "order_id": "entry-1", "ticker": ticker,
            "side": "yes", "action": "buy", "price": 25, "size_cents": 25,
            "contracts": 1,
        })
        trade_id = main._active_trades[ticker]

        pos = strat.get_position(ticker)
        pos.state = PositionState.HEDGING
        main._pending_orders["hedge-1"] = {
            "ticker": ticker, "trade_type": "hedge", "meta": {},
            "strategy": "green_up_full_green_market", "category": "Sports",
            "side": "no",
        }
        main.on_fill_received({
            "trade_id": "H1", "order_id": "hedge-1", "ticker": ticker,
            "side": "no", "action": "buy", "price": 49, "size_cents": 49,
            "contracts": 1,
        })

        assert strat.get_position(ticker).state == PositionState.HEDGED
        assert ticker not in main._active_trades

        parents = blotter.query_trades(trade_id=trade_id)
        assert len(parents) == 1
        assert parents[0].status == "hedged"
        assert parents[0].net_pnl_cents is None
        assert "locked_profit_cents=" in parents[0].notes

        legs = blotter.query_legs(parent_trade_id=trade_id, status="open")
        assert len(legs) == 2
        assert any(p["trade_id"] == trade_id for p in blotter.open_positions_summary())
    finally:
        (
            main._blotter, main._strategy, main._store, main._execution,
            main._circuit_breaker, main._alert_manager,
            main._active_trades, main._pending_orders,
        ) = saved


def test_high_prob_exit_fill_closes_entry_leg_with_realised_pnl():
    """End-to-end: contra-side exit fill closes entry leg and parent trade."""
    import config
    import main
    from metrics.blotter import Blotter
    from strategy.high_prob_strategy import HighProbStrategy, PositionState

    expected_fee = int(config.FEE_PER_CONTRACT_CENTS * 1)
    expected_pnl = (93 - 90) * 1 - expected_fee

    saved = (
        main._blotter, main._strategy, main._store, main._execution,
        main._circuit_breaker, main._alert_manager,
        dict(main._active_trades), dict(main._pending_orders),
    )
    try:
        ticker = "KXTEST-HP-EXIT"
        blotter = Blotter(":memory:")
        strat   = HighProbStrategy(min_yes_ask=85, min_roi_pct=0.0)
        strat.add_watch_ticker(ticker)

        main._blotter         = blotter
        main._strategy        = strat
        main._store           = None
        main._execution       = None
        main._circuit_breaker = None
        main._alert_manager   = None
        main._active_trades   = {}
        main._pending_orders  = {}
        main._processed_fill_ids.clear()

        pos = strat.get_position(ticker)
        pos.state = PositionState.WATCHING
        main._pending_orders["entry-1"] = {
            "ticker": ticker, "trade_type": "entry", "meta": {},
            "strategy": "high_prob_passive", "category": "Sports",
            "side": "yes",
        }
        main.on_fill_received({
            "trade_id": "E1", "order_id": "entry-1", "ticker": ticker,
            "side": "yes", "action": "buy", "price": 90, "size_cents": 90,
            "contracts": 1,
        })
        trade_id = main._active_trades[ticker]

        pos = strat.get_position(ticker)
        pos.state = PositionState.EXIT_PENDING
        pos.tp_order_id = "exit-1"
        main._pending_orders["exit-1"] = {
            "ticker": ticker, "trade_type": "exit", "meta": {},
            "strategy": "high_prob_passive", "category": "Sports",
            "side": "yes",
        }
        main.on_fill_received({
            "trade_id": "X1", "order_id": "exit-1", "ticker": ticker,
            "side": "no", "price": 93, "size_cents": 93, "contracts": 1,
        })

        assert strat.get_position(ticker).state == PositionState.CLOSED
        assert ticker not in main._active_trades

        parents = blotter.query_trades(trade_id=trade_id)
        assert len(parents) == 1
        assert parents[0].status == "closed"
        assert parents[0].net_pnl_cents == expected_pnl

        legs = blotter.query_legs(parent_trade_id=trade_id)
        assert len(legs) == 1
        assert legs[0].status == "closed"
        assert legs[0].realised_pnl_cents == expected_pnl
    finally:
        (
            main._blotter, main._strategy, main._store, main._execution,
            main._circuit_breaker, main._alert_manager,
            main._active_trades, main._pending_orders,
        ) = saved


def test_entry_fill_via_inflight_context_when_pending_not_yet_keyed():
    """Fill arriving before order_id is registered still books the blotter leg."""
    import main
    from metrics.blotter import Blotter
    from strategy.green_up_strategy import GreenUpStrategy, PositionState

    saved = (
        main._blotter, main._strategy, main._store, main._execution,
        main._circuit_breaker, main._alert_manager,
        dict(main._active_trades), dict(main._pending_orders),
        dict(main._inflight_submits),
    )
    try:
        ticker = "KXTEST-INFLIGHT"
        blotter = Blotter(":memory:")
        strat   = GreenUpStrategy(stop_loss_cents=10)
        strat.add_watch_ticker(ticker)

        main._blotter         = blotter
        main._strategy        = strat
        main._store           = None
        main._execution       = None
        main._circuit_breaker = None
        main._alert_manager   = None
        main._active_trades   = {}
        main._pending_orders  = {}
        main._inflight_submits = {
            ticker: main._build_pending_context(
                ticker=ticker,
                trade_type="entry",
                meta={},
                strategy=strat.name,
                side="yes",
            )
        }
        main._processed_fill_ids.clear()

        strat.get_position(ticker).state = PositionState.WATCHING
        main.on_fill_received({
            "trade_id": "INF-1", "order_id": "entry-inflight", "ticker": ticker,
            "side": "yes", "action": "buy", "price": 16, "size_cents": 16,
            "contracts": 1,
        })

        assert ticker in main._active_trades
        legs = blotter.query_legs(parent_trade_id=main._active_trades[ticker])
        assert len(legs) == 1
        assert legs[0].contracts == 1
    finally:
        (
            main._blotter, main._strategy, main._store, main._execution,
            main._circuit_breaker, main._alert_manager,
            main._active_trades, main._pending_orders, main._inflight_submits,
        ) = saved


def test_restore_active_trades_from_blotter():
    import main
    from metrics.blotter import Blotter

    saved = (
        main._blotter,
        dict(main._active_trades),
    )
    try:
        blotter = Blotter(":memory:")
        trade_id = blotter.open_trade(
            ticker="KXRESUME-1",
            category="Sports",
            strategy="green_up_test",
            trade_type="single",
        )
        blotter.record_fill(
            parent_trade_id=trade_id,
            order_id="e1",
            side="yes",
            trade_type="entry",
            contracts=1,
            entry_price=20,
            strategy="green_up_test",
        )
        main._blotter = blotter
        main._active_trades = {}
        main._restore_active_trades_from_blotter()
        assert main._active_trades["KXRESUME-1"] == trade_id
    finally:
        main._blotter, main._active_trades = saved

