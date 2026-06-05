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

import argparse
import asyncio
import logging
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
from risk.entry_gates import check_entry_allowed, check_stop_loss_allowed
from discovery.discovery_presets import (
    STRATEGY_DISCOVERY_PRESETS,
    apply_preset,
    preset_for_strategy,
)
from discovery.ticker_selector import (
    DEFAULT_DISCOVER_CATEGORY,
    resolve_discover_category,
    TickerCriteria,
    criteria_from_env,
    discover_tickers,
    discover_with_details,
    format_discovery_table,
    summarize_filter_rejections,
)
from strategy.base_strategy import BaseStrategy
from strategy.factory import VALID_STRATEGIES, _parse_comp_pairs, _parse_model_probs, build_strategy
from monitoring.session_table import SessionMonitor
from ingestion.rest_book_fallback import make_market_client_from_session, run_rest_book_fallback_loop
from trading.auth_check import verify_portfolio_credentials
from trading.fill_reconciler import run_fill_reconciliation_loop
from trading.portfolio_monitor import PortfolioMonitor


# ── Tickers to trade ──────────────────────────────────────────────────────────
# Override via env var: KALSHI_TICKERS="TICK1,TICK2,TICK3"

def _load_tickers() -> list[str]:
    raw = os.getenv("KALSHI_TICKERS", "PRES-2024-DEM,INXD-23DEC29-B4700")
    return [t.strip() for t in raw.split(",") if t.strip()]

# Alert evaluation interval (seconds)
ALERT_INTERVAL_SECONDS: float = 30.0

# Live terminal table refresh (seconds); 0 = disabled
MONITOR_INTERVAL_SECONDS: float = float(
    os.getenv("KALSHI_MONITOR_INTERVAL", "15")
)


# ── Module-level handles ──────────────────────────────────────────────────────
# Set during startup; used by signal handler and kill_switch.

_ingestor:           MarketIngestor    | None = None
_execution:          ExecutionManager  | None = None
_settlement_watcher: SettlementWatcher | None = None
_portfolio_monitor:  PortfolioMonitor  | None = None
_alert_manager:      AlertManager      | None = None
_session_monitor:    SessionMonitor    | None = None
_blotter:            Blotter           | None = None
_circuit_breaker:    CircuitBreaker    | None = None
_strategy:           BaseStrategy      | None = None
_store:              MetricsStore      | None = None
_shutdown_event = asyncio.Event()

# Active trade tracking: ticker -> parent_trade_id
# Maps each market to its currently open logical trade so every fill
# is linked to the correct blotter parent row.
_active_trades: dict[str, str] = {}
_pending_orders: dict[str, dict[str, Any]] = {}  # order_id -> submit context for blotter
# Fill ids already applied, so the WebSocket and REST-reconciliation paths never
# double-count the same fill.
_processed_fill_ids: set[str] = set()
_max_concurrent_positions: int = 0
_live_rules = None
_portfolio_snapshot = None  # latest exchange-backed snapshot (risk sync)


def _rollback_pending_entry(ticker: str) -> None:
    """
    If an entry signal was generated but never submitted, allow retry.

    Green-up sets WATCHING inside evaluate() before gates/circuit breaker run.
    """
    if not _strategy:
        return
    from strategy.green_up_strategy import GreenUpStrategy, PositionState

    if isinstance(_strategy, GreenUpStrategy):
        pos = _strategy.get_position(ticker)
        if pos and pos.state == PositionState.WATCHING:
            pos.state = PositionState.SCANNING

    from strategy.high_prob_strategy import HighProbStrategy, PositionState as HPState
    from strategy.mean_reversion_strategy import (
        MeanReversionStrategy,
        PositionState as MRState,
    )

    if isinstance(_strategy, HighProbStrategy):
        pos = _strategy.get_position(ticker)
        if pos and pos.state == HPState.WATCHING:
            pos.state = HPState.SCANNING

    if isinstance(_strategy, MeanReversionStrategy):
        pos = _strategy.get_position(ticker)
        if pos and pos.state == MRState.WATCHING:
            pos.state = MRState.SCANNING


def _rollback_pending_exit(ticker: str, phase: str) -> None:
    """If a round-trip TP/stop order was not accepted, allow retry on the next tick."""
    if not _strategy:
        return
    from strategy.high_prob_strategy import HighProbStrategy
    from strategy.mean_reversion_strategy import MeanReversionStrategy

    if isinstance(_strategy, (HighProbStrategy, MeanReversionStrategy)):
        _strategy.rollback_exit(ticker, phase)


def _rollback_pending_stop(ticker: str) -> None:
    """If a stop-loss order was not accepted, allow retry on the next tick."""
    if not _strategy:
        return
    from strategy.green_up_strategy import GreenUpStrategy

    if isinstance(_strategy, GreenUpStrategy):
        _strategy.rollback_stop(ticker)


def _restore_suppressed_stop(ticker: str, meta: dict) -> None:
    """
    Restore position state when a stop-loss is suppressed by the time gate.

    Both green_up and high_prob pre-emptively clear resting order IDs (hedge /
    take-profit) when building the stop signal so that cancel_order_id can be
    included.  If we suppress the stop without submitting, those IDs would be
    lost and the corresponding resting orders would become orphans on the
    exchange.  This helper restores them from the signal meta so the position
    returns to exactly the state it was in before the stop fired.
    """
    if not _strategy:
        return

    cancel_id = meta.get("cancel_order_id")

    from strategy.green_up_strategy import GreenUpStrategy, PositionState as GUState

    if isinstance(_strategy, GreenUpStrategy):
        pos = _strategy.get_position(ticker)
        if pos and pos.state == GUState.STOPPING and not pos.stop_order_id:
            if cancel_id:
                # Was in HEDGING (resting hedge on book) — restore hedge order.
                pos.hedge_order_id = cancel_id
                pos.state = GUState.HEDGING
            else:
                pos.state = GUState.ENTERED
            pos.stop_limit_price = 0
            pos.stop_cancel_failures = 0
        return

    from strategy.high_prob_strategy import HighProbStrategy, PositionState as HPState
    from strategy.mean_reversion_strategy import (
        MeanReversionStrategy,
        PositionState as MRState,
    )

    if isinstance(_strategy, (HighProbStrategy, MeanReversionStrategy)):
        pos = _strategy.get_position(ticker)
        exit_pending = HPState.EXIT_PENDING if isinstance(_strategy, HighProbStrategy) else MRState.EXIT_PENDING
        if pos and pos.state == exit_pending:
            if cancel_id:
                pos.tp_order_id = cancel_id
                pos.tp_order_sent = True
                pos.state = exit_pending
            else:
                pos.state = (
                    HPState.ENTERED if isinstance(_strategy, HighProbStrategy) else MRState.ENTERED
                )
            pos.stop_order_sent = False
            pos.stop_order_id = ""


def _rollback_pending_hedge(ticker: str) -> None:
    """If a hedge order was not accepted, allow retry on the next tick."""
    if not _strategy:
        return
    from strategy.green_up_strategy import GreenUpStrategy

    if isinstance(_strategy, GreenUpStrategy):
        _strategy.rollback_hedge(ticker)


async def _register_markets_for_tickers(
    tickers: list[str],
    credentials: CredentialManager,
    rate_limiter: RateLimiter,
) -> None:
    """Load market metadata so sector/category gates work with --tickers."""
    if not tickers:
        return
    from discovery.market_client import MarketClient
    from discovery.market_registry import register_markets

    # MarketClient opens its aiohttp session in __aenter__; using it outside
    # the async context leaves self._session=None and every get_market() fails
    # with "must be used as async context manager".
    markets = []
    async with MarketClient(credentials, rate_limiter) as client:
        for ticker in dict.fromkeys(tickers):
            try:
                summary = await client.get_market(ticker)
            except Exception as exc:
                logger.warning(
                    "Could not fetch market metadata",
                    ticker=ticker,
                    error=str(exc),
                )
                continue
            if summary:
                markets.append(summary)
    if markets:
        register_markets(markets)
        logger.info(
            "Market registry updated",
            tickers=[m.ticker for m in markets],
            categories=sorted({m.category for m in markets if m.category}),
        )


# ── Tick callback ─────────────────────────────────────────────────────────────

async def on_tick(tick: dict[str, Any]) -> None:
    """
    Central callback — called by the ingestor for every normalised market tick.

    Flow:
        tick → strategy.evaluate()
             → entry gates (balance, expiry) + live gates + circuit_breaker.approve()
             → execution.submit_order() (retries on 429)
             → pending order tracked → blotter.record_fill() on WS fill only
             → alert_manager.register_order()
    """
    global _strategy, _circuit_breaker, _execution, _store, _blotter
    global _active_trades, _alert_manager, _max_concurrent_positions, _live_rules
    global _portfolio_snapshot

    if _circuit_breaker and _circuit_breaker.is_tripped:
        return

    signal_obj = _strategy.evaluate(tick)
    if signal_obj is None:
        return

    meta = signal_obj.meta or {}
    phase = meta.get("phase", "entry")

    if phase in ("entry", "leg_1") and _live_rules and _live_rules.enabled:
        from discovery.live_market import is_tick_live
        from discovery.market_registry import get_market

        ticker = signal_obj.ticker
        book = _ingestor.get_book(ticker) if _ingestor else None
        ok, reason = is_tick_live(
            tick, book, _live_rules, market=get_market(ticker)
        )
        if not ok:
            logger.info(
                "Skipping entry — market not live",
                ticker=ticker,
                reason=reason,
            )
            _rollback_pending_entry(ticker)
            return

    if phase in ("entry", "leg_1"):
        from discovery.market_registry import get_market

        cash = (
            _portfolio_snapshot.cash_balance_cents
            if _portfolio_snapshot is not None
            else None
        )
        ok, reason = check_entry_allowed(
            phase=phase,
            ticker=signal_obj.ticker,
            cash_balance_cents=cash,
            market=get_market(signal_obj.ticker),
        )
        if not ok:
            logger.info(
                "Skipping entry — pre-trade gate",
                ticker=signal_obj.ticker,
                reason=reason,
            )
            _rollback_pending_entry(signal_obj.ticker)
            return

    if (
        _max_concurrent_positions > 0
        and phase in ("entry", "leg_1")
    ):
        from strategy.position_limits import count_open_positions

        open_count = count_open_positions(
            _strategy, exclude_ticker=signal_obj.ticker
        )
        if open_count >= _max_concurrent_positions:
            logger.info(
                "Skipping entry — max concurrent positions reached",
                ticker=signal_obj.ticker,
                open_count=open_count,
                max_concurrent=_max_concurrent_positions,
            )
            _rollback_pending_entry(signal_obj.ticker)
            return

    # Stop-loss time gate: hold the position when there is still time to rebound.
    if phase == "stop_loss":
        from discovery.market_registry import get_market as _get_mkt
        _sl_ok, _sl_reason = check_stop_loss_allowed(
            _get_mkt(signal_obj.ticker),
            config.STOP_LOSS_CLOSE_WINDOW_MINUTES,
        )
        if not _sl_ok:
            logger.info(
                "Suppressing stop-loss — time remaining",
                ticker=signal_obj.ticker,
                reason=_sl_reason,
                threshold_minutes=config.STOP_LOSS_CLOSE_WINDOW_MINUTES,
            )
            _restore_suppressed_stop(signal_obj.ticker, meta)
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
        if phase in ("entry", "leg_1"):
            _rollback_pending_entry(signal_obj.ticker)
        elif phase == "hedge":
            _rollback_pending_hedge(signal_obj.ticker)
        elif phase in ("exit", "stop_loss"):
            from strategy.high_prob_strategy import HighProbStrategy
            from strategy.mean_reversion_strategy import MeanReversionStrategy

            if isinstance(_strategy, (HighProbStrategy, MeanReversionStrategy)):
                _rollback_pending_exit(signal_obj.ticker, phase)
            elif phase == "stop_loss":
                _rollback_pending_stop(signal_obj.ticker)
        return

    # Determine trade_type from signal metadata
    phase      = meta.get("phase", "entry")      # "entry"|"hedge"|"stop_loss"
    arb_leg    = meta.get("leg_number")          # 1|2|None
    trade_type = f"leg_{arb_leg}" if arb_leg else phase
    ticker     = signal_obj.ticker

    cancel_order_id = meta.get("cancel_order_id")
    if cancel_order_id and _execution:
        cancelled = await _execution.cancel_order(cancel_order_id)
        if not cancelled:
            # Never submit the replacement while the prior resting order may
            # still be live — that is exactly how duplicate stop/hedge orders
            # pile up (the abandoned-order leak seen in production). Roll back
            # so the strategy retries the cancel+replace on a later tick.
            logger.warning(
                "Skipping replacement order: prior order cancel failed",
                ticker=ticker,
                cancel_order_id=cancel_order_id,
                phase=phase,
                strategy=signal_obj.strategy,
            )
            if phase in ("entry", "leg_1"):
                _rollback_pending_entry(ticker)
            elif phase == "hedge":
                _rollback_pending_hedge(ticker)
            elif phase in ("exit", "stop_loss"):
                from strategy.high_prob_strategy import HighProbStrategy
                from strategy.mean_reversion_strategy import MeanReversionStrategy

                if isinstance(_strategy, (HighProbStrategy, MeanReversionStrategy)):
                    _rollback_pending_exit(ticker, phase)
                elif phase == "stop_loss":
                    _rollback_pending_stop(ticker)
            return

    # Submit order to exchange (blotter records on confirmed WS fill only)
    order = await _execution.submit_order(signal_obj)
    if not order:
        if phase in ("entry", "leg_1"):
            _rollback_pending_entry(ticker)
        elif phase == "hedge":
            _rollback_pending_hedge(ticker)
        elif phase in ("exit", "stop_loss"):
            from strategy.high_prob_strategy import HighProbStrategy
            from strategy.mean_reversion_strategy import MeanReversionStrategy

            if isinstance(_strategy, (HighProbStrategy, MeanReversionStrategy)):
                _rollback_pending_exit(ticker, phase)
            elif phase == "stop_loss":
                _rollback_pending_stop(ticker)
        return

    order_id = order.get("order_id", "")
    _pending_orders[order_id] = {
        "ticker":     ticker,
        "trade_type": trade_type,
        "meta":       meta,
        "strategy":   signal_obj.strategy,
        "category":   meta.get("category", "Unknown"),
        "side":       signal_obj.side.value,
    }

    if phase == "stop_loss":
        from strategy.green_up_strategy import GreenUpStrategy
        from strategy.high_prob_strategy import HighProbStrategy
        from strategy.mean_reversion_strategy import MeanReversionStrategy

        if isinstance(_strategy, GreenUpStrategy):
            _strategy.register_stop_order(
                ticker, order_id, signal_obj.limit_price
            )
        elif isinstance(_strategy, (HighProbStrategy, MeanReversionStrategy)):
            _strategy.register_stop_order(ticker, order_id)

    if phase == "exit":
        from strategy.high_prob_strategy import HighProbStrategy
        from strategy.mean_reversion_strategy import MeanReversionStrategy

        if isinstance(_strategy, (HighProbStrategy, MeanReversionStrategy)):
            _strategy.register_tp_order(
                ticker, order_id, signal_obj.limit_price
            )

    if phase == "hedge":
        from strategy.green_up_strategy import GreenUpStrategy

        if isinstance(_strategy, GreenUpStrategy):
            _strategy.register_hedge_order(
                ticker, order_id, signal_obj.limit_price
            )

    if _alert_manager:
        _alert_manager.register_order(order_id, ticker)


# ── Fill confirmed (WebSocket or REST reconciliation) ─────────────────────────

def _fill_dedup_key(fill: dict[str, Any]) -> str:
    """Stable identity for a fill so it is applied at most once."""
    trade_id = fill.get("trade_id")
    if trade_id:
        return f"t:{trade_id}"
    return "c:{}:{}:{}:{}".format(
        fill.get("order_id", ""),
        fill.get("contracts", 0),
        fill.get("price", 0),
        fill.get("action") or fill.get("side", ""),
    )


def _known_order_ids() -> set[str]:
    """Order ids the bot is still tracking (awaiting a fill) for reconciliation."""
    ids = set(_pending_orders.keys())
    if _execution:
        ids |= set(_execution.open_orders.keys())
    return ids


def _close_entry_leg_on_exit(
    trade_id:   str,
    ticker:     str,
    exit_price: int,
    close_type: str,
    order_id:   str,
) -> bool:
    """
    Realise P&L on an early exit/stop by closing the open entry leg.

    Green-up stops and high-prob take-profits *flatten* the YES position rather
    than opening a new one. Booking the closing fill as a fresh leg (the old
    behaviour) left the entry leg perpetually "open" with no realised P&L and a
    phantom extra contract on the parent trade. Closing the entry leg at the
    fill price records the true realised P&L instead.

    The venue reports a YES sell on the contra ("no") side, but ``close_leg``
    derives P&L from the entry leg's own side/price, so the result is correct
    regardless of how the closing fill's side is labelled.

    Returns True when an entry leg was closed.
    """
    if _blotter is None:
        return False

    open_entries = _blotter.query_legs(
        parent_trade_id=trade_id, trade_type="entry", status="open"
    )
    if not open_entries:
        # Arbitrage entries are booked as leg_1/leg_2 — fall back to any open leg.
        open_entries = _blotter.query_legs(
            parent_trade_id=trade_id, status="open"
        )
    if not open_entries:
        logger.warning(
            "Blotter: exit fill with no open entry leg — skipping close",
            trade_id=trade_id,
            ticker=ticker,
            order_id=order_id,
            close_type=close_type,
        )
        return False

    entry_leg = open_entries[0]
    pnl = _blotter.close_leg(
        entry_leg.leg_id,
        exit_price=exit_price,
        close_type=close_type,
    )
    logger.info(
        "Blotter: entry leg closed on exit fill",
        leg_id=entry_leg.leg_id,
        trade_id=trade_id,
        ticker=ticker,
        exit_price=exit_price,
        realised_pnl_cents=pnl,
        realised_pnl_usd=round(pnl / 100, 2),
        close_type=close_type,
    )
    return True


def on_fill_received(fill: dict[str, Any]) -> None:
    global _active_trades
    """
    Called when a confirmed fill arrives — either from the WebSocket ``fill``
    channel or from the REST fill-reconciliation safety net.

    Authoritative fill record — advances strategy state machines, updates risk,
    blotter legs, and clears fill-timeout alerts. Idempotent: a fill already
    applied (by id) is ignored, so the WS and REST paths can run concurrently.
    """
    dedup_key = _fill_dedup_key(fill)
    if dedup_key in _processed_fill_ids:
        return
    _processed_fill_ids.add(dedup_key)

    order_id = fill.get("order_id", "")
    ticker   = fill.get("ticker", "")
    pending  = _pending_orders.get(order_id)
    exit_entry_closed = False

    if _strategy:
        _strategy.on_fill(fill)

    # Blotter: record only on confirmed exchange fills
    if _blotter and pending and ticker:
        trade_type = pending.get("trade_type", "entry")
        meta       = pending.get("meta") or {}
        price      = int(fill.get("price") or 0)
        contracts  = int(fill.get("contracts") or 0)
        if trade_type in ("entry", "leg_1") and ticker not in _active_trades:
            trade_id = _blotter.open_trade(
                ticker=ticker,
                category=pending.get("category", "Unknown"),
                strategy=pending.get("strategy", ""),
                trade_type="multi_leg" if str(trade_type).startswith("leg") else "single",
            )
            _active_trades[ticker] = trade_id
        else:
            trade_id = _active_trades.get(ticker)

        if trade_id:
            if trade_type in ("exit", "stop_loss"):
                # Closing fill — realise P&L against the open entry leg rather
                # than booking a phantom new opposing position.
                exit_entry_closed = _close_entry_leg_on_exit(
                    trade_id, ticker, price, trade_type, order_id
                )
            else:
                leg_id = _blotter.record_fill(
                    parent_trade_id=trade_id,
                    order_id=order_id,
                    side=fill.get("side", pending.get("side", "yes")),
                    trade_type=trade_type,
                    contracts=contracts,
                    entry_price=price,
                    strategy=pending.get("strategy", ""),
                    strategy_meta=meta,
                )
                logger.info(
                    "Blotter: leg recorded (fill confirmed)",
                    leg_id=leg_id,
                    trade_id=trade_id,
                    order_id=order_id,
                    contracts=contracts,
                )

        if _store:
            size_cents = int(fill.get("size_cents") or 0)
            if size_cents <= 0 and contracts > 0 and price > 0:
                size_cents = contracts * price
            _store.record_fill({
                "ticker":     ticker,
                "side":       fill.get("side", ""),
                "size_cents": size_cents,
                "price":      price,
                "strategy":   pending.get("strategy", ""),
                "order_id":   order_id,
            })

    if _strategy and _blotter and ticker:
        from strategy.green_up_strategy import GreenUpStrategy, PositionState
        from strategy.high_prob_strategy import HighProbStrategy, PositionState as HPState
        from strategy.mean_reversion_strategy import (
            MeanReversionStrategy,
            PositionState as MRState,
        )

        if isinstance(_strategy, GreenUpStrategy):
            pos = _strategy.get_position(ticker)
            if pos and pos.state == PositionState.HEDGED:
                trade_id = _active_trades.pop(ticker, None)
                if trade_id:
                    _blotter.mark_trade_hedged(
                        trade_id,
                        locked_profit_cents=pos.locked_profit_cents,
                        notes=pos.state.value,
                    )
            elif pos and pos.state == PositionState.STOPPED:
                trade_id = _active_trades.pop(ticker, None)
                if trade_id:
                    _blotter.close_trade(trade_id, notes=pos.state.value)

        if isinstance(_strategy, HighProbStrategy):
            pos = _strategy.get_position(ticker)
            if pos and pos.state == HPState.CLOSED:
                trade_id = _active_trades.pop(ticker, None)
                if trade_id:
                    _blotter.close_trade(trade_id, notes=pos.state.value)
            elif exit_entry_closed and ticker in _active_trades:
                trade_id = _active_trades.pop(ticker, None)
                if trade_id:
                    _blotter.close_trade(trade_id, notes="exit")

        if isinstance(_strategy, MeanReversionStrategy):
            pos = _strategy.get_position(ticker)
            if pos and pos.state == MRState.CLOSED:
                trade_id = _active_trades.pop(ticker, None)
                if trade_id:
                    _blotter.close_trade(trade_id, notes=pos.state.value)
            elif exit_entry_closed and ticker in _active_trades:
                trade_id = _active_trades.pop(ticker, None)
                if trade_id:
                    _blotter.close_trade(trade_id, notes="exit")

    if _execution:
        _execution.record_fill(fill)
        if order_id not in _execution.open_orders:
            _pending_orders.pop(order_id, None)

    if _circuit_breaker:
        _circuit_breaker.record_fill(fill)

    if _alert_manager:
        _alert_manager.confirm_fill(order_id)

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

async def _portfolio_risk_sync_loop(interval_seconds: float) -> None:
    """Refresh exchange portfolio and sync circuit breaker + entry-gate cache."""
    global _portfolio_snapshot

    while not _shutdown_event.is_set():
        try:
            if _portfolio_monitor:
                snap = await _portfolio_monitor.refresh()
                _portfolio_snapshot = snap
                if _circuit_breaker:
                    _circuit_breaker.sync_from_portfolio(snap)
        except Exception as exc:
            logger.warning(f"Portfolio risk sync failed: {exc}")
        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=interval_seconds,
            )
            break
        except asyncio.TimeoutError:
            pass


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


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kalshi prediction market trading bot")
    parser.add_argument(
        "--strategy",
        default=os.getenv("KALSHI_STRATEGY", config.DEFAULT_STRATEGY),
        choices=list(VALID_STRATEGIES),
        help=(
            f"Strategy engine (default: {config.DEFAULT_STRATEGY}, "
            "or KALSHI_STRATEGY env)"
        ),
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated tickers (overrides KALSHI_TICKERS)",
    )
    parser.add_argument(
        "--model-prob",
        nargs="*",
        metavar="TICKER:PROB",
        help="Kelly: model P(YES) per ticker, e.g. PRES-2024-DEM:0.62",
    )
    parser.add_argument(
        "--entry-max",
        "--entry_max",
        "--entry-max-price",
        "--entry_max_price",
        type=int,
        default=None,
        dest="entry_max",
        metavar="CENTS",
        help=(
            "Green-up: max YES ask to enter (cents). Omit with --gu-no-entry-max "
            "for no cap. Env KALSHI_GREEN_UP_ENTRY_MAX (0/none=off)"
        ),
    )
    parser.add_argument(
        "--gu-no-entry-max",
        action="store_true",
        help="Green-up: disable --entry-max cap (enter at current book prices)",
    )
    parser.add_argument(
        "--hedge-trigger",
        "--hedge_trigger",
        type=int,
        default=None,
        dest="hedge_trigger",
        metavar="CENTS",
        help="Green-up: YES bid to trigger hedge (absolute mode; cents)",
    )
    parser.add_argument(
        "--hedge-offset",
        "--hedge_offset",
        type=int,
        default=None,
        dest="hedge_offset",
        metavar="CENTS",
        help=(
            "Green-up: hedge when YES bid >= entry + N cents (relative mode; "
            "overrides --hedge-trigger). Env KALSHI_GREEN_UP_HEDGE_OFFSET"
        ),
    )
    parser.add_argument(
        "--hedge-mode",
        default=None,
        choices=["full_green", "stake_back", "partial"],
    )
    parser.add_argument(
        "--stop-loss",
        "--stop_loss",
        type=float,
        default=None,
        dest="stop_loss",
        help=(
            "Green-up: stop when YES bid falls this many cents below entry "
            "(max loss per contract ≈ N cents; default 10)"
        ),
    )
    parser.add_argument(
        "--max-concurrent-positions",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Max simultaneous open/pending positions per strategy "
            "(0=unlimited; env KALSHI_MAX_CONCURRENT_POSITIONS)"
        ),
    )
    parser.add_argument(
        "--gu-entry-mode",
        default=None,
        choices=[
            "passive", "cross_spread", "market",
            "limit_at_ask", "limit_at_bid", "limit_at_mid", "limit_offset",
        ],
        help=(
            "Green-up: entry order pricing — market (IOC) or limit at bid/ask "
            "(env KALSHI_GREEN_UP_ENTRY_MODE)"
        ),
    )
    parser.add_argument(
        "--gu-exit-mode",
        default=None,
        choices=[
            "passive", "cross_spread", "market",
            "limit_at_ask", "limit_at_bid", "limit_at_mid", "limit_offset",
        ],
        help=(
            "Green-up: hedge/stop order pricing on buy-NO legs "
            "(env KALSHI_GREEN_UP_EXIT_MODE)"
        ),
    )
    parser.add_argument(
        "--gu-limit-offset",
        type=int,
        default=None,
        metavar="CENTS",
        help=(
            "Green-up: cents added to bid for limit_offset entry mode "
            "(negative = bid minus |n|). Env KALSHI_GREEN_UP_LIMIT_OFFSET"
        ),
    )
    parser.add_argument(
        "--gu-max-spread",
        type=int,
        default=None,
        metavar="CENTS",
        help=(
            "Green-up: skip entry when YES spread exceeds N cents "
            "(env KALSHI_GREEN_UP_MAX_SPREAD, default 8)"
        ),
    )
    parser.add_argument(
        "--gu-hedge-style",
        default=None,
        choices=["trigger", "resting"],
        help=(
            "Green-up: ``trigger`` waits for YES bid >= hedge level; "
            "``resting`` posts GTC buy-NO at trigger after entry fill "
            "(env KALSHI_GREEN_UP_HEDGE_STYLE, default trigger)"
        ),
    )
    parser.add_argument(
        "--gu-max-cycles",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Green-up: max completed round-trips per ticker (0=unlimited). "
            "Env KALSHI_GREEN_UP_MAX_CYCLES_PER_TICKER"
        ),
    )
    parser.add_argument(
        "--comp-pairs",
        nargs="*",
        metavar="T1:T2",
        help="Arb: complementary pairs, e.g. PRES-DEM:PRES-REP",
    )
    parser.add_argument(
        "--hp-min-yes-ask",
        type=int,
        default=None,
        help="High-prob: minimum YES ask in cents (default 85)",
    )
    parser.add_argument(
        "--hp-max-yes-ask",
        type=int,
        default=None,
        help="High-prob: maximum YES ask in cents (default 97)",
    )
    parser.add_argument(
        "--hp-entry-mode",
        default=None,
        choices=[
            "passive", "cross_spread", "market",
            "limit_at_ask", "limit_at_bid", "limit_at_mid", "limit_offset",
        ],
        help="High-prob: how to price entries (default passive)",
    )
    parser.add_argument(
        "--hp-exit-mode",
        default=None,
        choices=[
            "passive", "cross_spread", "market",
            "limit_at_ask", "limit_at_bid", "limit_at_mid", "limit_offset",
        ],
        help="High-prob: how to price exits (default passive)",
    )
    parser.add_argument(
        "--hp-post-fill",
        default=None,
        choices=["hold", "resting_take_profit", "resting_stop", "tp_and_stop"],
        help="High-prob: behaviour after entry fill",
    )
    parser.add_argument(
        "--hp-stake-cents",
        type=int,
        default=None,
        help="High-prob: stake per entry in cents (default 5000)",
    )
    parser.add_argument(
        "--hp-take-profit-pct",
        type=float,
        default=None,
        help=(
            "High-prob: take-profit as fraction of (entry+vig), e.g. 0.30 for 30%% "
            "(values >1 treated as percent); env KALSHI_HP_TAKE_PROFIT_PCT"
        ),
    )
    parser.add_argument(
        "--hp-take-profit-offset",
        type=int,
        default=None,
        metavar="CENTS",
        help=(
            "High-prob: resting TP at entry + N cents when pct not set "
            "(env KALSHI_HP_TAKE_PROFIT_OFFSET, default 3)"
        ),
    )
    parser.add_argument(
        "--hp-stop-loss",
        type=float,
        default=None,
        help=(
            "High-prob: stop when YES bid falls this fraction below entry "
            "(default 0.12); ignored if --hp-stop-loss-cents is set. "
            "Env KALSHI_HP_STOP_LOSS"
        ),
    )
    parser.add_argument(
        "--hp-stop-loss-cents",
        type=int,
        default=None,
        metavar="CENTS",
        help=(
            "High-prob: stop when YES bid falls N cents below entry "
            "(overrides --hp-stop-loss). Env KALSHI_HP_STOP_LOSS_CENTS"
        ),
    )
    parser.add_argument(
        "--hp-max-spread",
        type=int,
        default=None,
        metavar="CENTS",
        help=(
            "High-prob: skip entry when YES spread exceeds N cents "
            "(env KALSHI_HP_MAX_SPREAD, default 8)"
        ),
    )
    parser.add_argument(
        "--hp-tp-style",
        default=None,
        choices=["fixed", "at_ask"],
        help=(
            "High-prob: resting TP price — fixed (entry-based target) or "
            "at_ask (legacy max with ask). Env KALSHI_HP_TP_STYLE"
        ),
    )
    parser.add_argument(
        "--hp-max-cycles",
        type=int,
        default=None,
        metavar="N",
        help=(
            "High-prob: max completed entry→exit cycles per ticker (0=unlimited). "
            "Env KALSHI_HP_MAX_CYCLES_PER_TICKER"
        ),
    )
    parser.add_argument(
        "--mr-lookback",
        type=int,
        default=None,
        metavar="TICKS",
        help="Mean-rev: rolling mid-price window (env KALSHI_MR_LOOKBACK_TICKS, default 20)",
    )
    parser.add_argument(
        "--mr-min-samples",
        type=int,
        default=None,
        metavar="N",
        help="Mean-rev: minimum ticks before entries (env KALSHI_MR_MIN_SAMPLES, default 10)",
    )
    parser.add_argument(
        "--mr-entry-deviation",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev: enter when mid deviates this far from mean (default 5)",
    )
    parser.add_argument(
        "--mr-take-profit-offset",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev: minimum take-profit offset from entry (default 5)",
    )
    parser.add_argument(
        "--mr-stop-loss-cents",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev: stop if price moves against entry by N cents (default 10)",
    )
    parser.add_argument(
        "--mr-min-volatility",
        type=float,
        default=None,
        metavar="CENTS",
        help="Mean-rev: require rolling std dev >= N cents (default 4)",
    )
    parser.add_argument(
        "--mr-entry-max",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev long: max YES ask for dip entries (default 45)",
    )
    parser.add_argument(
        "--mr-entry-min",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev long: min YES ask (default 10)",
    )
    parser.add_argument(
        "--mr-short-min-yes-ask",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev short: min YES ask to fade spikes (default 55)",
    )
    parser.add_argument(
        "--mr-short-max-yes-ask",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev short: max YES ask to fade (default 90)",
    )
    parser.add_argument(
        "--mr-stake-cents",
        type=int,
        default=None,
        help="Mean-rev: fixed stake per entry in cents",
    )
    parser.add_argument(
        "--mr-max-spread",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev: skip entry when spread exceeds N cents",
    )
    parser.add_argument(
        "--mr-trade-direction",
        default=None,
        choices=["long", "short", "both"],
        help="Mean-rev: long dips, short spikes, or both (default both)",
    )
    parser.add_argument(
        "--mr-exit-target",
        default=None,
        choices=["mean", "offset", "max"],
        help="Mean-rev: TP at rolling mean, fixed offset, or max of both",
    )
    parser.add_argument(
        "--mr-entry-mode",
        default=None,
        choices=[
            "passive", "cross_spread", "market",
            "limit_at_ask", "limit_at_bid", "limit_at_mid", "limit_offset",
        ],
        help="Mean-rev: entry order pricing (default passive)",
    )
    parser.add_argument(
        "--mr-exit-mode",
        default=None,
        choices=[
            "passive", "cross_spread", "market",
            "limit_at_ask", "limit_at_bid", "limit_at_mid", "limit_offset",
        ],
        help="Mean-rev: exit order pricing (default passive)",
    )
    parser.add_argument(
        "--mr-post-fill",
        default=None,
        choices=["hold", "resting_take_profit", "resting_stop", "tp_and_stop"],
        help="Mean-rev: post-fill exit behaviour (default tp_and_stop)",
    )
    parser.add_argument(
        "--mr-max-cycles",
        type=int,
        default=None,
        metavar="N",
        help="Mean-rev: max round-trips per ticker (0=unlimited)",
    )
    parser.add_argument(
        "--mr-limit-offset",
        type=int,
        default=None,
        metavar="CENTS",
        help="Mean-rev: limit_offset mode adjustment (env KALSHI_MR_LIMIT_OFFSET)",
    )
    parser.add_argument(
        "--monitor-interval",
        type=float,
        default=None,
        help=(
            "Live terminal table refresh in seconds "
            f"(default {MONITOR_INTERVAL_SECONDS}, 0=off; env KALSHI_MONITOR_INTERVAL)"
        ),
    )
    parser.add_argument(
        "--monitor-no-clear",
        action="store_true",
        help="Append monitor tables instead of clearing the screen each refresh",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Dashboard mode: hide INFO/DEBUG JSON on stderr (still written to log file). "
            "Same as KALSHI_LOG_CONSOLE=false"
        ),
    )

    # Auto-discovery: top N tickers in a category matching filters
    parser.add_argument(
        "--discover",
        action="store_true",
        help=(
            "Discover tickers automatically (category + filters) instead of "
            "KALSHI_TICKERS / --tickers"
        ),
    )
    parser.add_argument(
        "--discover-category",
        type=str,
        default=None,
        help=(
            "Category for discovery, e.g. Sports (default Trending if omitted; "
            "or KALSHI_DISCOVER_CATEGORY)"
        ),
    )
    parser.add_argument(
        "--discover-tag",
        type=str,
        default=None,
        help=(
            "Subcategory tag within category (e.g. Basketball, Tennis). "
            "List tags: python tools/screen.py tags --category Sports"
        ),
    )
    parser.add_argument(
        "--discover-sport",
        type=str,
        default=None,
        help="Sport name (alias for --discover-tag on Sports; env KALSHI_DISCOVER_SPORT)",
    )
    parser.add_argument(
        "--discover-competition",
        type=str,
        default=None,
        help=(
            "League/competition id (e.g. NBA, EPL). "
            "List: python tools/screen.py sports-filters --sport Basketball"
        ),
    )
    parser.add_argument(
        "--discover-scope",
        type=str,
        default=None,
        help="Market scope (e.g. Games, Futures) from sports-filters",
    )
    parser.add_argument(
        "--discover-series",
        type=str,
        default=None,
        help=(
            "Single series ticker — all markets in that series. "
            "List series: python tools/screen.py series --category Sports --tag Basketball"
        ),
    )
    parser.add_argument("--discover-top", type=int, default=None,
                        help="Max tickers to trade (default 10; env KALSHI_DISCOVER_TOP)")
    parser.add_argument(
        "--discover-min-volume",
        type=int,
        default=None,
        help="Minimum 24h contract volume (env KALSHI_DISCOVER_MIN_VOLUME)",
    )
    parser.add_argument(
        "--discover-max-yes-ask",
        type=int,
        default=None,
        metavar="CENTS",
        help="Only markets with YES ask <= CENTS, e.g. 25 for underdog entries",
    )
    parser.add_argument(
        "--discover-min-yes-ask",
        type=int,
        default=None,
        metavar="CENTS",
        help="Only markets with YES ask >= CENTS",
    )
    parser.add_argument(
        "--discover-max-spread",
        type=int,
        default=None,
        metavar="CENTS",
        help="Maximum bid-ask spread in cents",
    )
    parser.add_argument(
        "--discover-activity-hours",
        type=float,
        default=None,
        metavar="HOURS",
        help=(
            "Only markets updated within HOURS (live / recently active; "
            "env KALSHI_DISCOVER_ACTIVITY_HOURS)"
        ),
    )
    parser.add_argument(
        "--discover-full-scan",
        action="store_true",
        help="Scan all open events in category before ranking (slower, fewer misses)",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Run discovery, print selected tickers, and exit (no trading)",
    )
    parser.add_argument(
        "--discover-no-tradeable-filter",
        action="store_true",
        help="Include markets that fail is_tradeable() (wide spread, etc.)",
    )
    parser.add_argument(
        "--discover-preset",
        default=None,
        choices=["none"] + list(STRATEGY_DISCOVERY_PRESETS.keys()),
        help=(
            "Discovery filter preset (high_prob, green_up, kelly, arb). "
            "Default: match --strategy when using --discover. Use 'none' to disable."
        ),
    )
    parser.add_argument(
        "--discover-rank-by",
        default=None,
        choices=["volume", "fee_adjusted_roi", "screener", "activity", "spread"],
        help=(
            "Rank discovered markets by volume, fee-adjusted ROI, screener score, "
            "recent update (activity), or tightest spread"
        ),
    )
    parser.add_argument(
        "--discover-min-fee-roi",
        type=float,
        default=None,
        metavar="PCT",
        help="Minimum fee-adjusted ROI if YES wins (high-prob discovery)",
    )
    parser.add_argument(
        "--discover-max-minutes-to-close",
        type=float,
        default=None,
        metavar="MINUTES",
        help=(
            "Live filter: only markets closing within MINUTES "
            "(env KALSHI_LIVE_MAX_MINUTES_TO_CLOSE)"
        ),
    )
    parser.add_argument(
        "--no-live-only",
        action="store_true",
        help="Disable live-market filters (recent activity + close window + WS freshness)",
    )

    return parser.parse_args(argv)


def _explicit_discover_fields(args: argparse.Namespace) -> frozenset[str]:
    """CLI flags the user set explicitly — do not overwrite with presets."""
    explicit: set[str] = set()
    if args.discover_top is not None:
        explicit.add("top_n")
    if args.discover_min_volume is not None:
        explicit.add("min_volume_24h")
    if args.discover_max_yes_ask is not None:
        explicit.add("max_yes_ask")
    if args.discover_min_yes_ask is not None:
        explicit.add("min_yes_ask")
    if args.discover_max_spread is not None:
        explicit.add("max_spread")
    if args.discover_activity_hours is not None:
        explicit.add("activity_hours")
    if args.discover_full_scan:
        explicit.add("full_scan")
    if args.discover_rank_by is not None:
        explicit.add("rank_by")
    if args.discover_min_fee_roi is not None:
        explicit.add("min_fee_adjusted_roi_pct")
    if args.discover_max_minutes_to_close is not None:
        explicit.add("max_minutes_to_close")
    if args.discover_tag is not None:
        explicit.add("tag")
    if args.discover_sport is not None:
        explicit.add("sport")
    if args.discover_competition is not None:
        explicit.add("competition")
    if args.discover_scope is not None:
        explicit.add("scope")
    if args.discover_series is not None:
        explicit.add("series_ticker")
    return frozenset(explicit)


def _resolve_discover_preset(args: argparse.Namespace) -> str | None:
    if args.discover_preset == "none":
        return None
    if args.discover_preset:
        return args.discover_preset
    if args.discover or criteria_from_env():
        return preset_for_strategy(args.strategy)
    return None


def _discover_criteria_from_args(args: argparse.Namespace) -> TickerCriteria | None:
    """Merge CLI discover flags with KALSHI_DISCOVER_* env defaults."""
    if not args.discover and not criteria_from_env():
        return None

    env = criteria_from_env()
    raw_category = args.discover_category or (env.category if env else None)
    category = resolve_discover_category(raw_category)
    if not (raw_category or "").strip():
        logger.info(
            "Discovery category not set; using default",
            category=DEFAULT_DISCOVER_CATEGORY,
        )

    def _pick(cli_val, env_val, default):
        if cli_val is not None:
            return cli_val
        if env_val is not None:
            return env_val
        return default

    activity = args.discover_activity_hours
    if activity is None and env:
        activity = env.activity_hours

    full_scan = args.discover_full_scan or (env.full_scan if env else False)
    if activity is not None and not args.discover_full_scan:
        full_scan = True
    drilldown = any([
        args.discover_tag,
        args.discover_sport,
        args.discover_competition,
        args.discover_scope,
        args.discover_series,
    ]) or (
        env is not None
        and any([
            env.tag,
            env.sport,
            env.competition,
            env.scope,
            env.series_ticker,
        ])
    )
    if drilldown:
        full_scan = True

    rank_by = args.discover_rank_by or "volume"

    def _pick_str(cli_val, env_val):
        if cli_val is not None:
            return cli_val.strip() or None
        return env_val

    criteria = TickerCriteria(
        category=category,
        top_n=_pick(args.discover_top, env.top_n if env else None, 10),
        min_volume_24h=_pick(
            args.discover_min_volume, env.min_volume_24h if env else None, 0
        ),
        max_yes_ask=args.discover_max_yes_ask if args.discover_max_yes_ask is not None
        else (env.max_yes_ask if env else None),
        min_yes_ask=args.discover_min_yes_ask if args.discover_min_yes_ask is not None
        else (env.min_yes_ask if env else None),
        max_spread=args.discover_max_spread if args.discover_max_spread is not None
        else (env.max_spread if env else None),
        min_fee_adjusted_roi_pct=args.discover_min_fee_roi,
        rank_by=rank_by,
        screener_strategy=args.strategy,
        activity_hours=activity,
        full_scan=full_scan,
        tradeable_only=not args.discover_no_tradeable_filter
        and (env.tradeable_only if env else True),
        live_only=not args.no_live_only and config.LIVE_TRADING_ONLY,
        max_minutes_to_close=args.discover_max_minutes_to_close,
        tag=_pick_str(args.discover_tag, env.tag if env else None),
        sport=_pick_str(args.discover_sport, env.sport if env else None),
        competition=_pick_str(
            args.discover_competition, env.competition if env else None
        ),
        scope=_pick_str(args.discover_scope, env.scope if env else None),
        series_ticker=_pick_str(
            args.discover_series, env.series_ticker if env else None
        ),
    )

    preset_name = _resolve_discover_preset(args)
    if preset_name:
        criteria = apply_preset(
            criteria,
            preset_name,
            skip_fields=_explicit_discover_fields(args),
        )
        logger.info(
            "Discovery preset applied",
            preset=preset_name,
            rank_by=criteria.rank_by,
            min_yes_ask=criteria.min_yes_ask,
            max_yes_ask=criteria.max_yes_ask,
        )

    return criteria


def _log_discovery_empty(
    criteria: TickerCriteria,
    markets: list,
    *,
    discover_only: bool,
) -> None:
    """Explain why discovery selected zero tickers."""
    rejections = summarize_filter_rejections(markets, criteria)
    passed = rejections.pop("passed", 0)
    msg = (
        "Discovery matched no tickers — relax filters, add --discover-full-scan, "
        "or pass --tickers explicitly"
        if not discover_only
        else "Discovery matched no tickers — relax filters or use --discover-full-scan"
    )
    log_kwargs: dict[str, object] = {
        "category": criteria.category,
        "api_pool": len(markets),
        "passed_filters": passed,
        "preset": criteria.preset_name,
        "min_yes_ask": criteria.min_yes_ask,
        "max_yes_ask": criteria.max_yes_ask,
    }
    if rejections:
        log_kwargs["filter_rejections"] = rejections
    if markets and rejections:
        top_reason = max(rejections, key=rejections.get)
        log_kwargs["top_rejection"] = top_reason
    if not markets:
        log_kwargs["hint"] = (
            "API returned zero markets — lower --discover-min-volume, "
            "increase --discover-activity-hours, or add --discover-full-scan"
        )
    elif rejections:
        log_kwargs["hint"] = (
            "Markets were fetched but post-filters removed all of them — "
            "run with --discover-only to inspect filter_rejections, "
            "use tools/screen.py browse, or pass --tickers explicitly"
        )
    logger.error(msg, **log_kwargs)


async def _resolve_tickers(args: argparse.Namespace) -> list[str]:
    """
    Explicit --tickers wins; else --discover / KALSHI_DISCOVER; else env list.
    """
    if args.tickers:
        return [t.strip() for t in args.tickers.split(",") if t.strip()]

    if not (args.discover or criteria_from_env()):
        return _load_tickers()

    criteria = _discover_criteria_from_args(args)
    if criteria is None:
        logger.error(
            "Discovery could not build criteria — use --discover "
            f"(category defaults to {DEFAULT_DISCOVER_CATEGORY})"
        )
        sys.exit(1)

    credentials = CredentialManager()
    rate_limiter = RateLimiter()

    if args.discover_only:
        tickers, markets = await discover_with_details(
            credentials, rate_limiter, criteria
        )
        print(format_discovery_table(markets, tickers, criteria))
        if not tickers:
            _log_discovery_empty(criteria, markets, discover_only=True)
            sys.exit(1)
        print(f"\n  KALSHI_TICKERS=\"{','.join(tickers)}\"\n")
        sys.exit(0)

    tickers, markets = await discover_with_details(
        credentials, rate_limiter, criteria
    )
    from discovery.market_registry import set_markets

    set_markets(markets)
    if not tickers:
        _log_discovery_empty(criteria, markets, discover_only=False)
        sys.exit(1)
    return tickers


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace | None = None) -> None:
    global _ingestor, _execution, _strategy, _circuit_breaker
    global _store, _blotter, _settlement_watcher
    global _portfolio_monitor, _alert_manager, _session_monitor
    global _max_concurrent_positions, _live_rules, _portfolio_snapshot

    if args is None:
        args = parse_args()

    if args.quiet or not config.LOG_CONSOLE:
        logger.set_console_level(logging.WARNING)

    from discovery.live_market import LiveMarketRules

    _live_rules = LiveMarketRules(
        enabled=not args.no_live_only and config.LIVE_TRADING_ONLY,
        max_minutes_since_update=config.LIVE_MAX_MINUTES_SINCE_UPDATE,
        max_minutes_to_close=config.LIVE_MAX_MINUTES_TO_CLOSE,
        min_minutes_to_close=config.MIN_MINUTES_TO_EXPIRY,
        max_book_stale_minutes=config.LIVE_MAX_BOOK_STALE_MINUTES,
        max_trade_stale_minutes=config.LIVE_MAX_TRADE_STALE_MINUTES,
    )

    _max_concurrent_positions = (
        args.max_concurrent_positions
        if args.max_concurrent_positions is not None
        else config.MAX_CONCURRENT_POSITIONS
    )

    tickers = await _resolve_tickers(args)
    if not tickers:
        logger.error(
            "No tickers configured — set KALSHI_TICKERS, pass --tickers, "
            f"or use --discover (defaults to {DEFAULT_DISCOVER_CATEGORY})"
        )
        sys.exit(1)

    model_probs = _parse_model_probs(
        ",".join(args.model_prob) if args.model_prob else os.getenv("KALSHI_MODEL_PROB")
    )
    comp_pairs = (
        _parse_comp_pairs(",".join(args.comp_pairs))
        if args.comp_pairs
        else _parse_comp_pairs(os.getenv("KALSHI_ARB_PAIRS"))
    )

    # ── Startup validation ────────────────────────────────────────────────────
    if config.ENV == "production" and not config.API_KEY_ID:
        logger.error("KALSHI_API_KEY_ID not set — cannot start in production mode")
        sys.exit(1)

    try:
        _strategy = build_strategy(
            args.strategy,
            tickers,
            model_probs=model_probs,
            entry_max=args.entry_max,
            hedge_trigger=args.hedge_trigger,
            hedge_offset=args.hedge_offset,
            hedge_mode=args.hedge_mode,
            stop_loss=args.stop_loss,
            gu_entry_mode=args.gu_entry_mode,
            gu_exit_mode=args.gu_exit_mode,
            gu_limit_offset=args.gu_limit_offset,
            gu_max_cycles=args.gu_max_cycles,
            gu_max_spread=args.gu_max_spread,
            gu_no_entry_max=args.gu_no_entry_max,
            gu_hedge_style=args.gu_hedge_style,
            comp_pairs=comp_pairs,
            hp_min_yes_ask=args.hp_min_yes_ask,
            hp_max_yes_ask=args.hp_max_yes_ask,
            hp_entry_mode=args.hp_entry_mode,
            hp_post_fill=args.hp_post_fill,
            hp_stake_cents=args.hp_stake_cents,
            hp_take_profit_pct=args.hp_take_profit_pct,
            hp_take_profit_offset=args.hp_take_profit_offset,
            hp_stop_loss=args.hp_stop_loss,
            hp_stop_loss_cents=args.hp_stop_loss_cents,
            hp_max_spread=args.hp_max_spread,
            hp_tp_style=args.hp_tp_style,
            hp_max_cycles=args.hp_max_cycles,
            hp_exit_mode=args.hp_exit_mode,
            mr_lookback=args.mr_lookback,
            mr_min_samples=args.mr_min_samples,
            mr_entry_deviation=args.mr_entry_deviation,
            mr_take_profit_offset=args.mr_take_profit_offset,
            mr_stop_loss_cents=args.mr_stop_loss_cents,
            mr_min_volatility=args.mr_min_volatility,
            mr_entry_max=args.mr_entry_max,
            mr_entry_min=args.mr_entry_min,
            mr_short_min_yes_ask=args.mr_short_min_yes_ask,
            mr_short_max_yes_ask=args.mr_short_max_yes_ask,
            mr_stake_cents=args.mr_stake_cents,
            mr_max_spread=args.mr_max_spread,
            mr_trade_direction=args.mr_trade_direction,
            mr_exit_target=args.mr_exit_target,
            mr_entry_mode=args.mr_entry_mode,
            mr_exit_mode=args.mr_exit_mode,
            mr_post_fill=args.mr_post_fill,
            mr_max_cycles=args.mr_max_cycles,
            mr_limit_offset=args.mr_limit_offset,
        )
    except ValueError as exc:
        logger.error(str(exc))
        sys.exit(1)

    # ── Instantiate all modules ───────────────────────────────────────────────
    credentials      = CredentialManager()
    rate_limiter     = RateLimiter()
    _store           = MetricsStore()
    _blotter         = Blotter()
    calculator       = MetricsCalculator(_store)
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
        tickers=tickers,
        on_tick=on_tick,
        on_fill=on_fill_received,
        credentials=credentials,
    )

    # ── OS signal handlers ────────────────────────────────────────────────────
    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── Start services ────────────────────────────────────────────────────────
    await _execution.start()

    import aiohttp
    shared_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS)
    )
    _portfolio_monitor._session = shared_session
    _settlement_watcher._session = shared_session

    auth_ok, auth_msg = await verify_portfolio_credentials(
        credentials, rate_limiter, shared_session
    )
    if not auth_ok:
        logger.error(auth_msg)
        print(f"\n[AUTH] {auth_msg}\n")
        await shared_session.close()
        await _execution.stop()
        sys.exit(1)
    logger.info(auth_msg)

    global _portfolio_snapshot
    try:
        _portfolio_snapshot = await _portfolio_monitor.refresh()
        registry_tickers = list(
            dict.fromkeys(
                tickers + [p.ticker for p in _portfolio_snapshot.positions]
            )
        )
        await _register_markets_for_tickers(
            registry_tickers, credentials, rate_limiter
        )
        _circuit_breaker.sync_from_portfolio(_portfolio_snapshot)
    except Exception as exc:
        logger.warning(f"Initial portfolio risk sync failed: {exc}")
        await _register_markets_for_tickers(tickers, credentials, rate_limiter)

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
    risk_sync_task = asyncio.create_task(
        _portfolio_risk_sync_loop(config.PORTFOLIO_RISK_SYNC_SECONDS),
        name="portfolio_risk_sync",
    )
    book_fallback_task = None
    rest_book_client = make_market_client_from_session(
        _portfolio_monitor, credentials, rate_limiter
    )
    if rest_book_client and config.WS_BOOK_REST_FALLBACK_SECONDS > 0:
        book_fallback_task = asyncio.create_task(
            run_rest_book_fallback_loop(
                ingestor=_ingestor,
                market_client=rest_book_client,
                shutdown_event=_shutdown_event,
            ),
            name="rest_book_fallback",
        )
    fill_reconcile_task = None
    if config.FILL_RECONCILE_SECONDS > 0:
        fill_reconcile_task = asyncio.create_task(
            run_fill_reconciliation_loop(
                fetch_fills=lambda: _portfolio_monitor.fetch_fills(limit=100),
                on_fill=on_fill_received,
                known_order_ids=_known_order_ids,
                shutdown_event=_shutdown_event,
                interval_seconds=config.FILL_RECONCILE_SECONDS,
            ),
            name="fill_reconciler",
        )
    ingestor_task = asyncio.create_task(
        _ingestor.run(), name="market_ingestor"
    )

    monitor_task = None
    monitor_interval = (
        args.monitor_interval
        if args.monitor_interval is not None
        else MONITOR_INTERVAL_SECONDS
    )
    if monitor_interval > 0:
        _session_monitor = SessionMonitor(
            tickers=tickers,
            strategy=_strategy,
            ingestor=_ingestor,
            execution=_execution,
            portfolio_monitor=_portfolio_monitor,
            alert_manager=_alert_manager,
            blotter=_blotter,
            calculator=calculator,
            interval_seconds=monitor_interval,
            clear_screen=not args.monitor_no_clear,
        )
        monitor_task = asyncio.create_task(
            _session_monitor.run(), name="session_monitor"
        )

    startup_kw: dict = dict(
        env=config.ENV,
        strategy=_strategy.name,
        tickers=tickers,
        kelly_divisor=config.KELLY_DIVISOR,
        max_drawdown_pct=config.MAX_DRAWDOWN_PCT,
        fee_per_contract_cents=config.FEE_PER_CONTRACT_CENTS,
        alert_interval_seconds=ALERT_INTERVAL_SECONDS,
        monitor_interval_seconds=monitor_interval,
        ws_book_rest_fallback_seconds=config.WS_BOOK_REST_FALLBACK_SECONDS,
        ws_book_rest_fallback_poll_seconds=config.WS_BOOK_REST_FALLBACK_POLL_SECONDS,
        max_concurrent_positions=_max_concurrent_positions,
        live_trading_only=_live_rules.enabled if _live_rules else False,
        live_max_minutes_since_update=_live_rules.max_minutes_since_update if _live_rules else None,
        live_max_minutes_to_close=_live_rules.max_minutes_to_close if _live_rules else None,
    )
    if args.strategy == "green_up":
        from strategy.green_up_strategy import GreenUpStrategy

        if isinstance(_strategy, GreenUpStrategy):
            startup_kw.update(
                entry_max_cents=_strategy._entry_max_price,
                max_spread_cents=_strategy._max_spread_cents,
                hedge_trigger_cents=_strategy._hedge_trigger_price,
                hedge_offset_cents=_strategy._hedge_offset_cents,
                hedge_style=_strategy._hedge_style.value,
                hedge_mode=_strategy._hedge_mode.value,
                stop_loss_cents=_strategy._stop_loss_cents,
                entry_price_mode=_strategy._entry_price_mode.value,
                exit_price_mode=_strategy._exit_price_mode.value,
                max_cycles_per_ticker=_strategy._max_cycles_per_ticker,
                limit_offset_cents=_strategy._limit_offset,
            )
    logger.info("Kalshi trading bot started", **startup_kw)

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

    # Stop session monitor
    if _session_monitor:
        _session_monitor.stop()
    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    # Stop alert manager, portfolio risk sync, REST book fallback, and reconciler
    alert_task.cancel()
    risk_sync_task.cancel()
    if book_fallback_task:
        book_fallback_task.cancel()
    if fill_reconcile_task:
        fill_reconcile_task.cancel()
    try:
        await alert_task
    except asyncio.CancelledError:
        pass
    try:
        await risk_sync_task
    except asyncio.CancelledError:
        pass
    if book_fallback_task:
        try:
            await book_fallback_task
        except asyncio.CancelledError:
            pass
    if fill_reconcile_task:
        try:
            await fill_reconcile_task
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
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(parse_args()))
