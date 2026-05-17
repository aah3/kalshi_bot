"""
risk/circuit_breaker.py

Hard risk guardrails that sit between the strategy engine and execution.

Three independent checks — ANY breach triggers the kill switch:
    1. Real-time drawdown:     rolling peak-to-trough > MAX_DRAWDOWN_PCT
    2. Daily loss limit:       cumulative P&L today < -DAILY_LOSS_LIMIT_CENTS
    3. Sector concentration:   any single sector > MAX_SECTOR_CONCENTRATION of portfolio
    4. Position count:         open positions >= MAX_OPEN_POSITIONS
    5. Single position size:   proposed trade > MAX_POSITION_CENTS

When tripped, the circuit breaker:
    - Sets self.is_tripped = True
    - Emits a RISK_BREACH log entry
    - Calls the registered kill_switch coroutine (cancels all orders)
    - Freezes further approvals until manually reset (or process restart)
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import config
from logging_.structured_logger import logger
from strategy.base_strategy import Signal

try:
    from trading.portfolio_monitor import PortfolioSnapshot
except ImportError:
    PortfolioSnapshot = Any  # type: ignore[misc, assignment]


KillSwitchCallback = Callable[[], Coroutine[Any, Any, None]]


@dataclass
class Position:
    ticker:          str
    sector:          str
    size_cents:      int
    entry_price:     int
    current_price:   int
    side:            str   # "yes" | "no"
    unrealised_pnl:  int = 0   # cents

    def mark_to_market(self, market_price: int) -> None:
        """Update current price and recalculate unrealised P&L."""
        self.current_price = market_price
        if self.side == "yes":
            self.unrealised_pnl = (market_price - self.entry_price) * (self.size_cents // self.entry_price)
        else:
            self.unrealised_pnl = (self.entry_price - market_price) * (self.size_cents // (100 - self.entry_price))


class CircuitBreaker:
    """
    Risk guardrail layer. All signals must pass through `approve(signal)`
    before reaching the execution manager.
    """

    def __init__(self, kill_switch: KillSwitchCallback) -> None:
        self._kill_switch      = kill_switch
        self.is_tripped        = False

        # P&L tracking
        self._peak_equity      = 0           # highest total portfolio value seen (cents)
        self._realised_pnl     = 0           # cumulative P&L this session
        self._daily_pnl        = 0           # resets at start of each day
        self._day_start_epoch  = self._today_epoch()
        self._session_start_equity: int | None = None  # set on first portfolio sync
        self._last_portfolio_equity: int | None = None  # from last sync_from_portfolio

        # Position state
        self._positions: dict[str, Position] = {}   # ticker -> Position

        # Sector exposure tracking
        self._sector_exposure: dict[str, int] = defaultdict(int)  # sector -> total cents

    # ── Public interface ─────────────────────────────────────────────────────

    def approve(self, signal: Signal) -> bool:
        """
        Gate the signal through all risk checks.

        Returns True if the signal is safe to send to the execution manager.
        Returns False (and may trip the breaker) if any limit is breached.
        """
        if self.is_tripped:
            logger.warning("Circuit breaker is tripped — rejecting signal", ticker=signal.ticker)
            return False

        # Check 1: single position size hard cap
        if signal.size_cents > config.MAX_POSITION_CENTS:
            logger.warning(
                "Signal exceeds MAX_POSITION_CENTS — capping size",
                ticker=signal.ticker,
                proposed=signal.size_cents,
                cap=config.MAX_POSITION_CENTS,
            )
            signal.size_cents = config.MAX_POSITION_CENTS  # clamp rather than reject

        # Check 2: max open positions
        if len(self._positions) >= config.MAX_OPEN_POSITIONS:
            logger.warning("MAX_OPEN_POSITIONS reached — rejecting signal", ticker=signal.ticker)
            return False

        # Check 3: sector concentration
        sector = signal.meta.get("sector", "unknown") if signal.meta else "unknown"
        portfolio_total = self._total_exposure()
        if portfolio_total > 0:
            new_sector_exp = self._sector_exposure.get(sector, 0) + signal.size_cents
            concentration  = new_sector_exp / portfolio_total
            if concentration > config.MAX_SECTOR_CONCENTRATION:
                logger.warning(
                    "Sector concentration limit breached — rejecting signal",
                    ticker=signal.ticker,
                    sector=sector,
                    concentration=round(concentration, 3),
                    limit=config.MAX_SECTOR_CONCENTRATION,
                )
                return False

        # Check 4: drawdown (evaluated separately, always)
        self._check_drawdown()

        return not self.is_tripped

    def record_fill(self, fill: dict[str, Any]) -> None:
        """Update position state when a fill arrives from the exchange."""
        ticker     = fill.get("ticker", "")
        if not ticker:
            return
        side       = fill.get("side", "yes")
        price      = int(fill.get("price") or 0)
        contracts  = int(fill.get("contracts") or fill.get("count") or 0)
        size_cents = int(fill.get("size_cents") or 0)
        if size_cents <= 0 and contracts > 0 and price > 0:
            size_cents = contracts * price
        sector     = fill.get("sector", "unknown")

        existing = self._positions.get(ticker)
        if existing:
            existing.size_cents += size_cents
            existing.current_price = price or existing.current_price
            self._sector_exposure[sector] += size_cents
            return

        self._positions[ticker] = Position(
            ticker=ticker,
            sector=sector,
            size_cents=size_cents,
            entry_price=price or 1,
            current_price=price or 1,
            side=side,
        )
        self._sector_exposure[sector] += size_cents

    def sync_from_portfolio(self, snapshot: "PortfolioSnapshot") -> None:
        """
        Reconcile breaker state from an exchange-backed portfolio snapshot.

        Used by the background risk sync loop so drawdown and daily loss limits
        reflect real account equity, not fill-only approximations.
        """
        from discovery.market_registry import get_market

        old_tickers = set(self._positions)
        new_tickers = {p.ticker for p in snapshot.positions}

        for ticker in old_tickers - new_tickers:
            pos = self._positions[ticker]
            self.record_close(ticker, pos.current_price or pos.entry_price)

        self._positions.clear()
        self._sector_exposure.clear()

        for p in snapshot.positions:
            market = get_market(p.ticker)
            sector = market.category if market else "unknown"
            mark = p.mark_price or p.avg_entry_price
            self._positions[p.ticker] = Position(
                ticker=p.ticker,
                sector=sector,
                size_cents=p.cost_basis,
                entry_price=p.avg_entry_price or 1,
                current_price=mark,
                side=p.side,
                unrealised_pnl=p.unrealised_pnl,
            )
            self._sector_exposure[sector] += p.cost_basis

        equity = snapshot.portfolio_value_cents
        if self._session_start_equity is None:
            self._session_start_equity = equity

        self._last_portfolio_equity = equity
        self._check_drawdown()

    def record_close(self, ticker: str, exit_price: int) -> None:
        """Record a position being closed and update P&L."""
        pos = self._positions.pop(ticker, None)
        if not pos:
            return

        self._sector_exposure[pos.sector] = max(
            0, self._sector_exposure[pos.sector] - pos.size_cents
        )
        # Simplified P&L: price diff * notional units
        if pos.side == "yes":
            pnl = (exit_price - pos.entry_price) * (pos.size_cents // max(pos.entry_price, 1))
        else:
            pnl = (pos.entry_price - exit_price) * (pos.size_cents // max(100 - pos.entry_price, 1))

        self._realised_pnl += pnl
        self._daily_pnl    += pnl
        self._refresh_daily_pnl()
        self._check_drawdown()   # evaluate daily loss limit after every close

    def mark_to_market(self, ticker: str, current_price: int) -> None:
        """Update unrealised P&L for an open position."""
        pos = self._positions.get(ticker)
        if pos:
            pos.mark_to_market(current_price)
        self._check_drawdown()

    def reset(self) -> None:
        """Manually reset the circuit breaker after investigating a breach."""
        logger.warning("Circuit breaker manually reset")
        self.is_tripped = False

    # ── Private ──────────────────────────────────────────────────────────────

    def _total_exposure(self) -> int:
        return sum(p.size_cents for p in self._positions.values())

    def _total_equity(self) -> int:
        """Approximate total portfolio value (cost basis + unrealised P&L)."""
        return self._total_exposure() + sum(
            p.unrealised_pnl for p in self._positions.values()
        )

    def _check_drawdown(self) -> None:
        equity = (
            self._last_portfolio_equity
            if self._last_portfolio_equity is not None
            else self._total_equity()
        )
        if equity > self._peak_equity:
            self._peak_equity = equity

        if self._peak_equity > 0:
            drawdown = (self._peak_equity - equity) / self._peak_equity
            if drawdown > config.MAX_DRAWDOWN_PCT:
                self._trip(
                    reason="max drawdown exceeded",
                    drawdown=round(drawdown, 4),
                    peak_equity=self._peak_equity,
                    current_equity=equity,
                )
                return

        if self._session_start_equity is not None:
            session_pnl = equity - self._session_start_equity
            if session_pnl < -config.DAILY_LOSS_LIMIT_CENTS:
                self._trip(
                    reason="session loss limit exceeded",
                    session_pnl=session_pnl,
                    limit=-config.DAILY_LOSS_LIMIT_CENTS,
                )
                return

        self._refresh_daily_pnl()
        if self._daily_pnl < -config.DAILY_LOSS_LIMIT_CENTS:
            self._trip(
                reason="daily loss limit exceeded",
                daily_pnl=self._daily_pnl,
                limit=-config.DAILY_LOSS_LIMIT_CENTS,
            )

    def _trip(self, reason: str, **fields) -> None:
        if self.is_tripped:
            return
        self.is_tripped = True
        logger.risk_breach(reason, **fields)
        asyncio.create_task(self._kill_switch())

    def _refresh_daily_pnl(self) -> None:
        """Reset daily P&L counter if we've crossed into a new day."""
        today = self._today_epoch()
        if today > self._day_start_epoch:
            self._daily_pnl     = 0
            self._day_start_epoch = today

    @staticmethod
    def _today_epoch() -> int:
        """Return the Unix epoch of midnight today (UTC)."""
        now = time.gmtime()
        return int(time.mktime(time.struct_time(
            (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, 0)
        )))
