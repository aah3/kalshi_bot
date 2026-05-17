"""
monitoring/session_table.py

Live terminal monitoring table for main.py — P&L, market odds, strategy state,
execution, and green-up hedge / stop-loss progress.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import config
from discovery.orderbook_parse import OrderBookSnapshot
from ingestion.market_ingestor import MarketIngestor
from logging_.structured_logger import logger
from metrics.blotter import Blotter
from metrics.calculator import MetricsCalculator
from strategy.base_strategy import BaseStrategy
from strategy.green_up_strategy import GreenUpStrategy
from trading.portfolio_monitor import PortfolioMonitor, PortfolioSnapshot


W = 118


def _pnl_str(usd: float) -> str:
    if usd > 0:
        return f"+${usd:.2f}"
    if usd < 0:
        return f"-${abs(usd):.2f}"
    return f"${usd:.2f}"


def _action_hint(state: str, bid: int | None, hedge_at: int, stop_at: int, entry_max: int) -> str:
    if state in ("scanning", "watching"):
        if bid is not None and bid >= hedge_at:
            return "hedge trigger met"
        return f"watch entry (<={entry_max}c ask)"
    if state == "entered":
        if bid is not None and bid >= hedge_at:
            return "HEDGE NOW"
        if bid is not None and stop_at and bid <= stop_at:
            return "STOP zone"
        return f"hold -> hedge @{hedge_at}c"
    if state in ("hedging", "stopping"):
        return "awaiting fill"
    if state == "hedged":
        return "profit locked"
    if state == "stopped":
        return "stopped out"
    return state


def _book_quotes(
    ticker: str,
    ingestor: MarketIngestor | None,
    rest_books: dict[str, OrderBookSnapshot] | None,
) -> tuple[int | None, int | None, float | None, int | None]:
    """Best bid/ask/mid/spread from WS book or REST fallback."""
    bid = ask = mid = spread = None
    if ingestor:
        ob = ingestor.get_book(ticker)
        if ob and ob.best_bid is not None:
            bid, ask = ob.best_bid, ob.best_ask
            mid, spread = ob.mid_price, ob.spread
    if (bid is None or ask is None) and rest_books and ticker in rest_books:
        snap = rest_books[ticker]
        bid    = snap.best_bid
        ask    = snap.best_ask
        mid    = snap.mid_price
        spread = snap.spread
    return bid, ask, mid, spread


def build_ticker_rows(
    tickers: list[str],
    ingestor: MarketIngestor | None,
    strategy: BaseStrategy | None,
    rest_books: dict[str, OrderBookSnapshot] | None = None,
) -> list[dict[str, Any]]:
    """Merge order book, strategy state, and green-up previews per ticker."""
    rows: list[dict[str, Any]] = []
    green: GreenUpStrategy | None = (
        strategy if isinstance(strategy, GreenUpStrategy) else None
    )
    summaries: dict[str, dict] = {}
    if strategy and hasattr(strategy, "summary"):
        for s in strategy.summary():
            summaries[s["ticker"]] = s

    for ticker in tickers:
        bid, ask, mid, spread = _book_quotes(ticker, ingestor, rest_books)

        s = summaries.get(ticker, {})
        state = s.get("state", "—")
        entry_max = green._entry_max_price if green else 0
        hedge_at  = green._hedge_trigger_price if green else 68
        stop_at   = s.get("stop_trigger_price") or 0

        preview_locked: float | None = None
        preview_hedge: int | None = None
        if green and state == "entered" and bid is not None:
            pos = green.get_position(ticker)
            if pos and pos.entry_price_cents > 0:
                no_px = 100 - bid
                if no_px > 0:
                    preview_hedge, preview_locked = pos.compute_full_green(no_px)

        rows.append({
            "ticker":      ticker,
            "bid":         bid,
            "ask":         ask,
            "mid_pct":     round(mid, 1) if mid is not None else None,
            "spread":      spread,
            "state":       state,
            "entry_c":     s.get("entry_price_cents") or 0,
            "entry_odds":  s.get("entry_decimal_odds") or 0,
            "stake_usd":   (s.get("entry_stake_cents") or 0) / 100,
            "hedge_at":    hedge_at,
            "stop_at":     stop_at,
            "locked_usd":  s.get("locked_profit_usd") or (
                round(preview_locked / 100, 2) if preview_locked else None
            ),
            "preview_locked_usd": (
                round(preview_locked / 100, 2) if preview_locked else None
            ),
            "time_s":      s.get("time_in_trade_s"),
            "action":      _action_hint(
                state if state != "—" else "watching",
                bid, hedge_at, stop_at, entry_max,
            ),
        })

    return rows


def render_session_table(
    *,
    tickers: list[str],
    strategy_name: str,
    ingestor: MarketIngestor | None,
    strategy: BaseStrategy | None,
    rest_books: dict[str, OrderBookSnapshot] | None = None,
    execution_open: int,
    portfolio: PortfolioSnapshot | None,
    alerts: list[dict],
    blotter_open: int,
    metrics: dict[str, Any] | None = None,
    clear_screen: bool = True,
) -> None:
    """Print a full-screen monitoring table to stdout."""
    if clear_screen:
        print("\033[2J\033[H", end="")

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    env = config.ENV.upper()
    print("=" * W)
    print(
        f"  KALSHI BOT LIVE MONITOR  |  {now}  |  ENV={env}  |  "
        f"strategy={strategy_name}"
    )
    print("=" * W)

    # Account strip
    if portfolio:
        cash   = portfolio.cash_balance_cents / 100
        unreal = portfolio.total_unrealised_pnl_cents / 100
        real   = portfolio.session_realised_pnl_cents / 100
        inv    = portfolio.total_cost_basis_cents / 100
        print(
            f"  Cash ${cash:,.2f}  |  Invested ${inv:,.2f}  |  "
            f"Unrealised {_pnl_str(unreal)}  |  Session {_pnl_str(real)}  |  "
            f"Positions {portfolio.num_positions}  |  "
            f"Open orders {execution_open}  |  Blotter open {blotter_open}"
        )
    else:
        print(
            f"  Portfolio: (refresh pending)  |  Open orders {execution_open}  |  "
            f"Blotter open {blotter_open}"
        )

    if metrics:
        fill = metrics.get("fill_rate", {})
        dd   = metrics.get("max_drawdown", {})
        total_sig = fill.get("total_signals", 0)
        filled    = fill.get("filled", 0)
        if total_sig == 0:
            fill_line = "Fill rate n/a (no signals yet — strategy needs WS ticks to enter)"
        else:
            fill_line = (
                f"Fill rate {fill.get('fill_rate', 0)*100:.0f}% "
                f"({filled}/{total_sig})"
            )
        print(f"  {fill_line}  |  Drawdown {dd.get('max_drawdown_pct', 0)*100:.1f}%")

    # Per-ticker strategy + market table
    print("-" * W)
    print(
        f"  {'TICKER':<28} {'BID':>4} {'ASK':>4} {'SPR':>4} {'MID%':>5} "
        f"{'STATE':<9} {'ENTRY':>5} {'ODDS':>5} {'STAKE':>7} "
        f"{'HEDGE@':>6} {'STOP@':>5} {'LOCK$':>7} {'PREV$':>7} "
        f"{'UNRL$':>8} {'NEXT':<14}"
    )
    print("-" * W)

    pos_by_ticker = {
        p.ticker: p for p in (portfolio.positions if portfolio else [])
    }

    for row in build_ticker_rows(tickers, ingestor, strategy, rest_books):
        ticker = row["ticker"]
        bid_s  = f"{row['bid']:>3}c" if row["bid"] is not None else "  — "
        ask_s  = f"{row['ask']:>3}c" if row["ask"] is not None else "  — "
        spr_s  = f"{row['spread']:>3}c" if row["spread"] is not None else "  —"
        mid_s  = f"{row['mid_pct']:>4.0f}" if row["mid_pct"] is not None else "   —"
        entry_s = f"{row['entry_c']:>4}c" if row["entry_c"] else "   —"
        odds_s  = f"{row['entry_odds']:>4.1f}" if row["entry_odds"] else "  — "
        stake_s = f"${row['stake_usd']:>5.2f}" if row["stake_usd"] else "     —"
        hedge_s = f"{row['hedge_at']:>5}c"
        stop_s  = f"{row['stop_at']:>4}c" if row["stop_at"] else "  —"
        lock_s  = (
            f"${row['locked_usd']:>5.2f}"
            if row["locked_usd"] is not None
            else "    —"
        )
        prev_s  = (
            f"${row['preview_locked_usd']:>5.2f}"
            if row.get("preview_locked_usd") is not None
            else "    —"
        )

        exch = pos_by_ticker.get(ticker)
        unrl_s = (
            f"{_pnl_str(exch.unrealised_pnl / 100):>8}"
            if exch
            else "       —"
        )

        print(
            f"  {ticker:<28} {bid_s:>4} {ask_s:>4} {spr_s:>4} {mid_s:>5} "
            f"{row['state']:<9} {entry_s:>5} {odds_s:>5} {stake_s:>7} "
            f"{hedge_s:>6} {stop_s:>5} {lock_s:>7} {prev_s:>7} "
            f"{unrl_s:>8} {row['action']:<14}"
        )

    # Exchange positions not in ticker list
    extra = [p for p in pos_by_ticker.values() if p.ticker not in tickers]
    if extra:
        print("-" * W)
        print("  Other exchange positions:")
        for p in extra[:5]:
            print(
                f"    {p.ticker:<28} {p.side:<4} qty={p.contracts}  "
                f"entry={p.avg_entry_price}c  mark={p.mark_price or 0}c  "
                f"unreal={_pnl_str(p.unrealised_pnl/100)}"
            )

    # Alerts
    print("-" * W)
    if alerts:
        print("  Alerts:")
        for a in alerts[:5]:
            sev = (a.get("severity") or "info")[:4].upper()
            msg = (a.get("message") or a.get("alert_type", ""))[:72]
            tkr = a.get("ticker") or "—"
            print(f"    [{sev}] {tkr:<28} {msg}")
    else:
        print("  Alerts: none")

    print("=" * W)
    print("  Ctrl+C to stop bot")


class SessionMonitor:
    """
    Background task: refresh portfolio + redraw the session table.
    """

    def __init__(
        self,
        *,
        tickers: list[str],
        strategy: BaseStrategy,
        ingestor: MarketIngestor,
        execution,
        portfolio_monitor: PortfolioMonitor,
        alert_manager,
        blotter: Blotter,
        calculator: MetricsCalculator | None = None,
        interval_seconds: float = 15.0,
        clear_screen: bool = True,
    ) -> None:
        self._tickers = tickers
        self._strategy = strategy
        self._ingestor = ingestor
        self._execution = execution
        self._portfolio_monitor = portfolio_monitor
        self._alert_manager = alert_manager
        self._blotter = blotter
        self._calculator = calculator
        self._interval = interval_seconds
        self._clear_screen = clear_screen
        self._running = False
        self._market_client = None

    async def run(self) -> None:
        self._running = True
        logger.info(
            "Session monitor started",
            interval_seconds=self._interval,
        )
        while self._running:
            try:
                portfolio = await self._portfolio_monitor.refresh()
            except Exception as exc:
                logger.warning(f"Monitor portfolio refresh failed: {exc}")
                portfolio = None

            metrics = None
            if self._calculator:
                try:
                    metrics = self._calculator.all_metrics()
                except Exception:
                    pass

            alerts = (
                self._alert_manager.active_alert_summary()
                if self._alert_manager
                else []
            )
            open_orders = len(self._execution.open_orders) if self._execution else 0
            blotter_open = len(self._blotter.open_positions_summary())

            rest_books: dict[str, OrderBookSnapshot] = {}
            need_rest = any(
                _book_quotes(t, self._ingestor, None)[0] is None
                for t in self._tickers
            )
            if need_rest and self._portfolio_monitor._session:
                from discovery.market_client import MarketClient
                if self._market_client is None:
                    self._market_client = MarketClient(
                        self._portfolio_monitor._creds,
                        self._portfolio_monitor._limiter,
                    )
                    self._market_client._session = self._portfolio_monitor._session
                for t in self._tickers:
                    if _book_quotes(t, self._ingestor, None)[0] is not None:
                        continue
                    try:
                        book = await self._market_client.get_order_book(t, depth=5)
                        if book:
                            rest_books[t] = book
                    except Exception as exc:
                        logger.warning(f"REST book fetch failed for {t}: {exc}")

            try:
                render_session_table(
                    tickers=self._tickers,
                    strategy_name=self._strategy.name,
                    ingestor=self._ingestor,
                    strategy=self._strategy,
                    rest_books=rest_books or None,
                    execution_open=open_orders,
                    portfolio=portfolio,
                    alerts=alerts,
                    blotter_open=blotter_open,
                    metrics=metrics,
                    clear_screen=self._clear_screen,
                )
            except Exception as exc:
                logger.warning(f"Monitor render failed: {exc}")

            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False
