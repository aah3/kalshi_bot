"""
main.py — entry point

Wires all modules together and manages the top-level event loop.

Startup sequence:
    1. Load config (ENV flag, credentials from env vars)
    2. Initialise blotter, metrics store, structured logger
    3. Instantiate strategy engine and risk layer (CircuitBreaker)
    4. Start ExecutionManager (opens HTTP session, starts token refresh loop)
    5. Start SettlementWatcher background task (polls every 5 min)
    6. Start AlertManager background task (evaluates alerts every 30 s)
    7. Start MarketIngestor (WebSocket → order book → strategy callbacks)
    8. Register SIGINT / SIGTERM handler for graceful shutdown

Shutdown sequence (on SIGINT / SIGTERM):
    1. Stop MarketIngestor (close WebSocket)
    2. Stop SettlementWatcher and AlertManager
    3. Log all open positions from blotter
    4. Cancel all resting limit orders via ExecutionManager
    5. Run one final settlement check
    6. Close HTTP session and flush logs
    7. Print session summary from blotter
"""

import asyncio
import os
import signal
import sys
from typing import Any

import config
from credentials.credential_manager import CredentialManager
from execution.execution_manager import ExecutionManager
from execution.rate_limiter import RateLimiter
from ingestion.market_ingestor import MarketIngestor
from logging_.structured_logger import logger
from metrics.blotter import Blotter
from metrics.calculator import MetricsCalculator
from metrics.metrics_store import MetricsStore
from metrics.settlement import SettlementWatcher
from risk.alert_manager import AlertManager
from risk.circuit_breaker import CircuitBreaker
from strategy.kelly_strategy import KellyStrategy
from trading.portfolio_monitor import PortfolioMonitor


# ── Tickers to trade ──────────────────────────────────────────────────────────
# Override via env var: KALSHI_TICKERS="TICK1,TICK2,TICK3"

TICKERS: list[str] = os.getenv(
    "KALSHI_TICKERS", "PRES-2024-DEM,INXD-23DEC29-B4700"
).split(",")

# Alert evaluation interval (seconds)
ALERT_INTERVAL_SECONDS: float = 30.0


# ── Module-level handles ──────────────────────────────────────────────────────
# Set during startup; used by signal handler and kill_switch.

_ingestor:           MarketIngestor    | None = None
_execution:          ExecutionManager  | None = None
_settlement_watcher: SettlementWatcher | None = None
_portfolio_monitor:  PortfolioMonitor  | None = None
_alert_manager:      AlertManager      | None = None
_blotter:            Blotter           | None = None
_circuit_breaker:    CircuitBreaker    | None = None
_strategy:           KellyStrategy     | None = None
_store:              MetricsStore      | None = None
_shutdown_event = asyncio.Event()

# Active trade tracking: ticker -> parent_trade_id
# Maps each market to its currently open logical trade so every fill
# is linked to the correct blotter parent row.
_active_trades: dict[str, str] = {}


# ── Tick callback ─────────────────────────────────────────────────────────────

async def on_tick(tick: dict[str, Any]) -> None:
    """
    Central callback — called by the ingestor for every normalised market tick.

    Flow:
        tick → strategy.evaluate()
             → circuit_breaker.approve()
             → execution.submit_order()
             → blotter.open_trade() / record_fill()
             → alert_manager.register_order()
    """
    global _strategy, _circuit_breaker, _execution, _store, _blotter
    global _active_trades, _alert_manager

    signal_obj = _strategy.evaluate(tick)
    if signal_obj is None:
        return

    # Record signal intent (for fill-rate tracking)
    _store.record_signal({
        "ticker":      signal_obj.ticker,
        "side":        signal_obj.side.value,
        "edge":        signal_obj.edge,
        "edge_to_vig": signal_obj.edge_to_vig,
        "size_cents":  signal_obj.size_cents,
        "strategy":    signal_obj.strategy,
    })

    logger.signal_generated(
        ticker=signal_obj.ticker,
        side=signal_obj.side.value,
        size_cents=signal_obj.size_cents,
        edge=signal_obj.edge,
        edge_to_vig=signal_obj.edge_to_vig,
        strategy=signal_obj.strategy,
    )

    if not _circuit_breaker.approve(signal_obj):
        return

    # Determine trade_type from signal metadata
    meta       = signal_obj.meta or {}
    phase      = meta.get("phase", "entry")      # "entry"|"hedge"|"stop_loss"
    arb_leg    = meta.get("leg_number")          # 1|2|None
    trade_type = f"leg_{arb_leg}" if arb_leg else phase

    # Open a new parent trade row for entries; reuse existing for hedges/stops
    ticker = signal_obj.ticker
    if trade_type in ("entry", "leg_1") and ticker not in _active_trades:
        trade_id = _blotter.open_trade(
            ticker=ticker,
            category=meta.get("category", "Unknown"),
            strategy=signal_obj.strategy,
            trade_type="multi_leg" if arb_leg else "single",
        )
        _active_trades[ticker] = trade_id
    else:
        trade_id = _active_trades.get(ticker)

    # Submit order to exchange
    order = await _execution.submit_order(signal_obj)
    if not order:
        return

    order_id    = order.get("order_id", "")
    order_status = order.get("status", "pending")

    # Track order for fill-timeout alerts
    if _alert_manager:
        _alert_manager.register_order(order_id, ticker)

    # Record fill in blotter — optimistic for market/aggressive orders.
    # The on_fill_received() WebSocket handler will confirm or correct this.
    price_cents = signal_obj.limit_price or 50
    contracts   = max(signal_obj.size_cents // price_cents, 1)

    if trade_id:
        leg_id = _blotter.record_fill(
            parent_trade_id=trade_id,
            order_id=order_id,
            side=signal_obj.side.value,
            trade_type=trade_type,
            contracts=contracts,
            entry_price=price_cents,
            strategy=signal_obj.strategy,
            strategy_meta=meta,
        )
        logger.info(
            "Blotter: leg recorded",
            leg_id=leg_id,
            trade_id=trade_id,
            order_id=order_id,
            order_status=order_status,
        )

    # Also record in legacy MetricsStore (Sharpe / drawdown calculator)
    _store.record_fill({
        "ticker":     ticker,
        "side":       signal_obj.side.value,
        "size_cents": signal_obj.size_cents,
        "price":      price_cents,
        "strategy":   signal_obj.strategy,
        "order_id":   order_id,
    })


# ── Fill confirmed (WebSocket) ────────────────────────────────────────────────

def on_fill_received(fill: dict[str, Any]) -> None:
    """
    Called when a confirmed fill arrives via the WebSocket trade stream.

    This is the authoritative fill record — use it to:
      - Advance strategy state machines (green-up, arb leg tracking)
      - Update the circuit breaker's position state
      - Confirm fill in the alert manager (clears fill-timeout timer)
      - Mark signal as filled in MetricsStore

    Wire this into market_ingestor.py's _apply_trade() method.
    """
    order_id = fill.get("order_id", "")
    ticker   = fill.get("ticker", "")

    # Advance strategy state (green-up hedge trigger, arb leg 2 readiness)
    if _strategy:
        _strategy.on_fill(fill)

    # Update circuit breaker position inventory
    if _circuit_breaker:
        _circuit_breaker.record_fill(fill)

    # Clear fill-timeout alert timer
    if _alert_manager:
        _alert_manager.confirm_fill(order_id)

    # Mark signal as filled in MetricsStore
    if _store:
        _store.mark_signal_filled(ticker=ticker, order_id=order_id)

    logger.fill_received(
        order_id=order_id,
        ticker=ticker,
        side=fill.get("side", ""),
        price=fill.get("price", 0),
        contracts=fill.get("contracts", 0),
    )


# ── Kill switch (circuit breaker callback) ────────────────────────────────────

async def kill_switch() -> None:
    """
    Hard stop — called by CircuitBreaker when any risk limit is breached.
    Cancels all open orders then sets the shutdown event.
    """
    logger.risk_breach("kill switch activated — cancelling all orders and halting")
    if _execution:
        await _execution.cancel_all_orders()
    _shutdown_event.set()


# ── OS signal handler ─────────────────────────────────────────────────────────

def _handle_signal(sig, frame) -> None:
    logger.shutdown(reason=f"OS signal {sig} received")
    _shutdown_event.set()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    global _ingestor, _execution, _strategy, _circuit_breaker
    global _store, _blotter, _settlement_watcher
    global _portfolio_monitor, _alert_manager

    # ── Startup validation ────────────────────────────────────────────────────
    if config.ENV == "production" and not config.API_KEY_ID:
        logger.error("KALSHI_API_KEY_ID not set — cannot start in production mode")
        sys.exit(1)

    # ── Instantiate all modules ───────────────────────────────────────────────
    credentials      = CredentialManager()
    rate_limiter     = RateLimiter()
    _store           = MetricsStore()
    _blotter         = Blotter()
    calculator       = MetricsCalculator(_store)
    _strategy        = KellyStrategy()
    _circuit_breaker = CircuitBreaker(kill_switch=kill_switch)
    _execution       = ExecutionManager(credentials, rate_limiter)

    _settlement_watcher = SettlementWatcher(
        blotter=_blotter,
        credentials=credentials,
        rate_limiter=rate_limiter,
        metrics_store=_store,
    )

    _portfolio_monitor = PortfolioMonitor(
        credentials=credentials,
        rate_limiter=rate_limiter,
    )

    _alert_manager = AlertManager(blotter=_blotter)

    _ingestor = MarketIngestor(
        tickers=TICKERS,
        on_tick=on_tick,
        credentials=credentials,
    )

    # ── OS signal handlers ────────────────────────────────────────────────────
    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── Start services ────────────────────────────────────────────────────────
    await _execution.start()

    # Open shared HTTP session for portfolio monitor and alert manager
    import aiohttp
    shared_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS)
    )
    _portfolio_monitor._session = shared_session
    _settlement_watcher._session = shared_session

    # Background tasks
    settlement_task = asyncio.create_task(
        _settlement_watcher.run(), name="settlement_watcher"
    )
    alert_task = asyncio.create_task(
        _alert_manager.run(
            monitor=_portfolio_monitor,
            interval_seconds=ALERT_INTERVAL_SECONDS,
        ),
        name="alert_manager",
    )
    ingestor_task = asyncio.create_task(
        _ingestor.run(), name="market_ingestor"
    )

    logger.info(
        "Kalshi trading bot started",
        env=config.ENV,
        tickers=TICKERS,
        kelly_divisor=config.KELLY_DIVISOR,
        max_drawdown_pct=config.MAX_DRAWDOWN_PCT,
        fee_per_contract_cents=config.FEE_PER_CONTRACT_CENTS,
        alert_interval_seconds=ALERT_INTERVAL_SECONDS,
    )

    # Startup metrics from calculator
    metrics = calculator.all_metrics()
    logger.info("Startup metrics snapshot", **metrics)

    # Resume open positions from previous session
    open_pos = _blotter.open_positions_summary()
    if open_pos:
        logger.info(
            f"Resuming with {len(open_pos)} open positions from previous session",
            open_positions=[p["trade_id"] for p in open_pos],
        )

    # ── Run until shutdown event ──────────────────────────────────────────────
    await _shutdown_event.wait()

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    open_blotter = _blotter.open_positions_summary()
    logger.shutdown(
        open_blotter_trade_ids=[p["trade_id"] for p in open_blotter],
        open_exchange_orders=list(_execution.open_orders.keys()),
        active_alerts=_alert_manager.active_alert_summary(),
    )

    # Stop ingestor first (no new ticks)
    await _ingestor.stop()
    ingestor_task.cancel()
    try:
        await ingestor_task
    except asyncio.CancelledError:
        pass

    # Stop alert manager
    alert_task.cancel()
    try:
        await alert_task
    except asyncio.CancelledError:
        pass

    # Stop settlement watcher
    await _settlement_watcher.stop()
    settlement_task.cancel()
    try:
        await settlement_task
    except asyncio.CancelledError:
        pass

    # Cancel all resting exchange orders + close HTTP session
    await _execution.stop()

    # Final settlement check — catch any resolutions that occurred during shutdown
    logger.info("Running final settlement check...")
    async with aiohttp.ClientSession() as final_sess:
        _settlement_watcher._session = final_sess
        settled_now = await _settlement_watcher.check_now()
        if settled_now:
            logger.info(
                f"Final settlement check resolved {len(settled_now)} position(s)",
                settled=[r.ticker for r in settled_now],
            )

    # Close shared session
    await shared_session.close()

    # Session summary from blotter
    closed  = _blotter.query_trades(status="closed",  days=1)
    settled = _blotter.query_trades(status="settled", days=1)
    session_pnl = sum((t.net_pnl_cents or 0) for t in closed + settled)

    final_metrics = calculator.all_metrics()
    logger.info("Final session metrics", **final_metrics)
    logger.info(
        "Session complete",
        trades_closed=len(closed),
        trades_settled=len(settled),
        session_net_pnl_usd=round(session_pnl / 100, 2),
    )
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
