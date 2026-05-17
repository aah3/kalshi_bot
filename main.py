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
)
from strategy.base_strategy import BaseStrategy
from strategy.factory import VALID_STRATEGIES, _parse_comp_pairs, _parse_model_probs, build_strategy
from monitoring.session_table import SessionMonitor
from trading.auth_check import verify_portfolio_credentials
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
_max_concurrent_positions: int = 0
_live_rules = None


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
    global _active_trades, _alert_manager, _max_concurrent_positions, _live_rules

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
            from strategy.green_up_strategy import GreenUpStrategy, PositionState

            if isinstance(_strategy, GreenUpStrategy):
                pos = _strategy.get_position(ticker)
                if pos and pos.state == PositionState.WATCHING:
                    pos.state = PositionState.SCANNING
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
    global _active_trades
    """
    Called when a confirmed fill arrives via the WebSocket ``fill`` channel.

    Authoritative fill record — advances strategy state machines, updates risk,
    and clears fill-timeout alerts.
    """
    order_id = fill.get("order_id", "")
    ticker   = fill.get("ticker", "")

    if _strategy:
        _strategy.on_fill(fill)

    if _strategy and _blotter and ticker:
        from strategy.green_up_strategy import GreenUpStrategy, PositionState

        if isinstance(_strategy, GreenUpStrategy):
            pos = _strategy.get_position(ticker)
            if pos and pos.state in (PositionState.HEDGED, PositionState.STOPPED):
                trade_id = _active_trades.pop(ticker, None)
                if trade_id:
                    _blotter.close_trade(trade_id, notes=pos.state.value)

    if _execution:
        _execution.record_fill(fill)

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


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kalshi prediction market trading bot")
    parser.add_argument(
        "--strategy",
        default=os.getenv("KALSHI_STRATEGY", "kelly"),
        choices=list(VALID_STRATEGIES),
        help="Strategy engine (default: kelly, or KALSHI_STRATEGY env)",
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
        help="Green-up: max YES ask to enter (cents)",
    )
    parser.add_argument(
        "--hedge-trigger",
        "--hedge_trigger",
        type=int,
        default=None,
        dest="hedge_trigger",
        metavar="CENTS",
        help="Green-up: YES bid to trigger hedge (cents)",
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
        help="Green-up: stop if YES bid falls this fraction below entry (default 0.40)",
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
        choices=["volume", "fee_adjusted_roi", "screener"],
        help=(
            "Rank discovered markets by volume, fee-adjusted ROI, or "
            "strategy screener score (uses --strategy fit)"
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

    rank_by = args.discover_rank_by or "volume"

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
            logger.error(
                "Discovery matched no tickers — relax filters or use --discover-full-scan",
                category=criteria.category,
            )
            sys.exit(1)
        print(f"\n  KALSHI_TICKERS=\"{','.join(tickers)}\"\n")
        sys.exit(0)

    tickers, markets = await discover_with_details(
        credentials, rate_limiter, criteria
    )
    from discovery.market_registry import set_markets

    set_markets(markets)
    if not tickers:
        logger.error(
            "Discovery matched no tickers — relax filters, add --discover-full-scan, "
            "or pass --tickers explicitly",
            category=criteria.category,
        )
        sys.exit(1)
    return tickers


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace | None = None) -> None:
    global _ingestor, _execution, _strategy, _circuit_breaker
    global _store, _blotter, _settlement_watcher
    global _portfolio_monitor, _alert_manager, _session_monitor
    global _max_concurrent_positions, _live_rules

    if args is None:
        args = parse_args()

    from discovery.live_market import LiveMarketRules

    _live_rules = LiveMarketRules(
        enabled=not args.no_live_only and config.LIVE_TRADING_ONLY,
        max_minutes_since_update=config.LIVE_MAX_MINUTES_SINCE_UPDATE,
        max_minutes_to_close=config.LIVE_MAX_MINUTES_TO_CLOSE,
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
            hedge_mode=args.hedge_mode,
            stop_loss=args.stop_loss,
            gu_entry_mode=args.gu_entry_mode,
            gu_exit_mode=args.gu_exit_mode,
            comp_pairs=comp_pairs,
            hp_min_yes_ask=args.hp_min_yes_ask,
            hp_max_yes_ask=args.hp_max_yes_ask,
            hp_entry_mode=args.hp_entry_mode,
            hp_post_fill=args.hp_post_fill,
            hp_stake_cents=args.hp_stake_cents,
            hp_exit_mode=args.hp_exit_mode,
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
                hedge_trigger_cents=_strategy._hedge_trigger_price,
                hedge_mode=_strategy._hedge_mode.value,
                stop_loss_threshold=_strategy._stop_loss_threshold,
                entry_price_mode=_strategy._entry_price_mode.value,
                exit_price_mode=_strategy._exit_price_mode.value,
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
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(parse_args()))
