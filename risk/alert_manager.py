"""
risk/alert_manager.py

Real-time alert system for position-level and portfolio-level events.

Runs as a background asyncio task alongside the portfolio monitor.
On each evaluation cycle it checks every open position against five
independent alert conditions and emits structured log entries at
WARNING level for every triggered alert.

────────────────────────────────────────────────────────────────────────────────
ALERT TYPES
────────────────────────────────────────────────────────────────────────────────

  PROFIT_TARGET     Position unrealised P&L >= PROFIT_TARGET_PCT of cost basis
                    e.g. cost=$100, target=60% → alert when value >= $160

  POSITION_STOP     Position unrealised P&L <= -POSITION_STOP_LOSS_PCT
                    (softer than circuit breaker — warns before hard kill)

  EXPIRY_WARNING    Market closes in <= EXPIRY_WARNING_MINUTES with open position
                    Two thresholds: 60 min (early warning) and 10 min (urgent)

  FILL_TIMEOUT      A submitted limit order has not filled after FILL_TIMEOUT_MINUTES
                    Suggests the limit price is too passive

  BALANCE_LOW       Account cash balance drops below MIN_ACCOUNT_BALANCE_CENTS

────────────────────────────────────────────────────────────────────────────────
ALERT SUPPRESSION
────────────────────────────────────────────────────────────────────────────────

Each alert is suppressed for ALERT_COOLDOWN_SECONDS after firing to prevent
log spam. The cooldown resets if the condition clears and re-triggers.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

    alert_mgr = AlertManager(blotter=blotter)

    # Start as background task (called from main.py)
    asyncio.create_task(alert_mgr.run(portfolio_monitor, interval_seconds=30))

    # Or: one-shot evaluation (e.g. after each tick)
    alerts = await alert_mgr.evaluate(snapshot)
    for alert in alerts:
        print(alert.message)
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import config
from logging_.structured_logger import logger
from metrics.blotter import Blotter
from trading.portfolio_monitor import PortfolioMonitor, PortfolioSnapshot, Position


# ── Tunables ──────────────────────────────────────────────────────────────────

ALERT_COOLDOWN_SECONDS:    float = 300.0   # 5 min between repeats of same alert
EXPIRY_WARNING_MINUTES:    float = 60.0    # first expiry warning
EXPIRY_URGENT_MINUTES:     float = 10.0    # urgent expiry warning
FILL_TIMEOUT_MINUTES:      float = 15.0    # warn if limit order not filled in 15 min
PROFIT_TARGET_PCT:         float = config.PROFIT_TARGET_PCT       # from config (default 0.60)
POSITION_STOP_PCT:         float = config.POSITION_STOP_LOSS_PCT  # from config (default 0.40)


# ── Alert types ───────────────────────────────────────────────────────────────

class AlertType(str, Enum):
    PROFIT_TARGET    = "PROFIT_TARGET"
    POSITION_STOP    = "POSITION_STOP"
    EXPIRY_WARNING   = "EXPIRY_WARNING"
    EXPIRY_URGENT    = "EXPIRY_URGENT"
    FILL_TIMEOUT     = "FILL_TIMEOUT"
    BALANCE_LOW      = "BALANCE_LOW"
    CIRCUIT_BREAKER  = "CIRCUIT_BREAKER"     # re-emitted from CircuitBreaker events


class AlertSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    alert_type:  AlertType
    severity:    AlertSeverity
    ticker:      str | None
    message:     str
    data:        dict[str, Any] = field(default_factory=dict)
    fired_at:    float          = field(default_factory=time.monotonic)

    def log(self) -> None:
        """Emit this alert to the structured logger."""
        if self.severity == AlertSeverity.CRITICAL:
            logger.risk_breach(self.message, alert_type=self.alert_type.value,
                               ticker=self.ticker, **self.data)
        else:
            logger.warning(self.message, alert_type=self.alert_type.value,
                           ticker=self.ticker, **self.data)


# ── Alert manager ─────────────────────────────────────────────────────────────

class AlertManager:
    """
    Evaluates all alert conditions on every portfolio refresh cycle.

    Maintains a cooldown map to suppress repeated alerts:
        key: (AlertType, ticker or "portfolio")
        value: monotonic timestamp of last fire
    """

    def __init__(self, blotter: Blotter | None = None) -> None:
        self._blotter  = blotter
        self._cooldown: dict[tuple, float] = {}   # (AlertType, key) -> last_fired_at
        self._open_orders: dict[str, float] = {}  # order_id -> submission timestamp

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(
        self,
        monitor:          PortfolioMonitor,
        interval_seconds: float = 30.0,
    ) -> None:
        """
        Background loop: refresh portfolio snapshot and evaluate alerts.
        Wire into main.py:
            asyncio.create_task(alert_mgr.run(portfolio_monitor))
        """
        logger.info("AlertManager started", interval_seconds=interval_seconds)
        while True:
            try:
                snapshot = await monitor.refresh()
                alerts   = await self.evaluate(snapshot)
                for alert in alerts:
                    alert.log()
            except asyncio.CancelledError:
                logger.info("AlertManager cancelled")
                return
            except Exception as exc:
                logger.error(f"AlertManager evaluation error: {exc}")
            await asyncio.sleep(interval_seconds)

    async def evaluate(self, snapshot: PortfolioSnapshot) -> list[Alert]:
        """
        Run all alert checks against a portfolio snapshot.
        Returns only alerts that are not suppressed by cooldown.
        """
        alerts: list[Alert] = []

        # Per-position checks
        for pos in snapshot.positions:
            alerts.extend(self._check_profit_target(pos))
            alerts.extend(self._check_position_stop(pos))
            alerts.extend(self._check_expiry(pos))

        # Portfolio-level checks
        alerts.extend(self._check_balance(snapshot.cash_balance_cents))

        # Open order fill timeout
        alerts.extend(self._check_fill_timeouts())

        return alerts

    def register_order(self, order_id: str, ticker: str) -> None:
        """
        Register a submitted limit order for fill-timeout tracking.
        Call this from main.py after every order submission.
        """
        self._open_orders[order_id] = time.monotonic()
        logger.debug("AlertManager: tracking order", order_id=order_id, ticker=ticker)

    def confirm_fill(self, order_id: str) -> None:
        """Remove an order from fill-timeout tracking once it fills."""
        self._open_orders.pop(order_id, None)

    def cancel_order_tracking(self, order_id: str) -> None:
        """Remove an order from tracking when it is cancelled."""
        self._open_orders.pop(order_id, None)

    # ── Per-position checks ───────────────────────────────────────────────────

    def _check_profit_target(self, pos: Position) -> list[Alert]:
        """Fire when unrealised P&L >= PROFIT_TARGET_PCT of cost basis."""
        if pos.cost_basis == 0 or pos.unrealised_pnl <= 0:
            return []

        pnl_pct = pos.unrealised_pnl / pos.cost_basis
        if pnl_pct < PROFIT_TARGET_PCT:
            return []

        key = (AlertType.PROFIT_TARGET, pos.ticker)
        if self._suppressed(key):
            return []
        self._arm(key)

        return [Alert(
            alert_type=AlertType.PROFIT_TARGET,
            severity=AlertSeverity.WARNING,
            ticker=pos.ticker,
            message=(
                f"PROFIT TARGET HIT: {pos.ticker} [{pos.side.upper()}]  "
                f"unrealised P&L = +${pos.unrealised_pnl/100:.2f} "
                f"({pnl_pct*100:.1f}% of cost)  "
                f"Consider taking profit or placing a stop."
            ),
            data={
                "cost_basis_usd":     round(pos.cost_basis / 100, 2),
                "unrealised_pnl_usd": round(pos.unrealised_pnl / 100, 2),
                "pnl_pct":            round(pnl_pct * 100, 1),
                "target_pct":         PROFIT_TARGET_PCT * 100,
                "mark_price":         pos.mark_price,
                "avg_entry_price":    pos.avg_entry_price,
                "contracts":          pos.contracts,
                "implied_prob_pct":   round(pos.implied_prob * 100, 1),
            },
        )]

    def _check_position_stop(self, pos: Position) -> list[Alert]:
        """Fire when unrealised P&L <= -POSITION_STOP_PCT of cost basis."""
        if pos.cost_basis == 0 or pos.unrealised_pnl >= 0:
            return []

        loss_pct = abs(pos.unrealised_pnl) / pos.cost_basis
        if loss_pct < POSITION_STOP_PCT:
            return []

        key = (AlertType.POSITION_STOP, pos.ticker)
        if self._suppressed(key):
            return []
        self._arm(key)

        return [Alert(
            alert_type=AlertType.POSITION_STOP,
            severity=AlertSeverity.CRITICAL,
            ticker=pos.ticker,
            message=(
                f"POSITION STOP ALERT: {pos.ticker} [{pos.side.upper()}]  "
                f"unrealised P&L = -${abs(pos.unrealised_pnl)/100:.2f} "
                f"(-{loss_pct*100:.1f}% of cost)  "
                f"Consider closing or hedging this position."
            ),
            data={
                "cost_basis_usd":     round(pos.cost_basis / 100, 2),
                "unrealised_pnl_usd": round(pos.unrealised_pnl / 100, 2),
                "loss_pct":           round(loss_pct * 100, 1),
                "stop_threshold_pct": POSITION_STOP_PCT * 100,
                "mark_price":         pos.mark_price,
                "avg_entry_price":    pos.avg_entry_price,
                "contracts":          pos.contracts,
            },
        )]

    def _check_expiry(self, pos: Position) -> list[Alert]:
        """Fire when a market with an open position is close to expiry."""
        mins = pos.minutes_to_close
        if mins is None:
            return []   # no expiry

        alerts: list[Alert] = []

        # Urgent (≤ 10 min)
        if mins <= EXPIRY_URGENT_MINUTES:
            key = (AlertType.EXPIRY_URGENT, pos.ticker)
            if not self._suppressed(key):
                self._arm(key, cooldown=120)   # 2-min cooldown on urgent
                alerts.append(Alert(
                    alert_type=AlertType.EXPIRY_URGENT,
                    severity=AlertSeverity.CRITICAL,
                    ticker=pos.ticker,
                    message=(
                        f"URGENT EXPIRY: {pos.ticker} closes in {mins:.0f} min  "
                        f"[{pos.side.upper()}  {pos.contracts} contracts  "
                        f"cost=${pos.cost_basis/100:.2f}  "
                        f"unrealised ${pos.unrealised_pnl/100:+.2f}]  "
                        f"Act now or let it expire."
                    ),
                    data={
                        "minutes_to_close": round(mins, 1),
                        "contracts":        pos.contracts,
                        "cost_basis_usd":   round(pos.cost_basis / 100, 2),
                        "unrealised_pnl":   round(pos.unrealised_pnl / 100, 2),
                        "implied_prob_pct": round(pos.implied_prob * 100, 1),
                    },
                ))

        # Early warning (≤ 60 min, > 10 min)
        elif mins <= EXPIRY_WARNING_MINUTES:
            key = (AlertType.EXPIRY_WARNING, pos.ticker)
            if not self._suppressed(key):
                self._arm(key)
                alerts.append(Alert(
                    alert_type=AlertType.EXPIRY_WARNING,
                    severity=AlertSeverity.WARNING,
                    ticker=pos.ticker,
                    message=(
                        f"EXPIRY WARNING: {pos.ticker} closes in {mins:.0f} min  "
                        f"[{pos.side.upper()}  unrealised ${pos.unrealised_pnl/100:+.2f}]"
                    ),
                    data={
                        "minutes_to_close": round(mins, 1),
                        "implied_prob_pct": round(pos.implied_prob * 100, 1),
                    },
                ))

        return alerts

    # ── Portfolio-level checks ────────────────────────────────────────────────

    def _check_balance(self, balance_cents: int) -> list[Alert]:
        """Fire when cash balance drops below MIN_ACCOUNT_BALANCE_CENTS."""
        if balance_cents >= config.MIN_ACCOUNT_BALANCE_CENTS:
            return []

        key = (AlertType.BALANCE_LOW, "portfolio")
        if self._suppressed(key):
            return []
        self._arm(key)

        return [Alert(
            alert_type=AlertType.BALANCE_LOW,
            severity=AlertSeverity.CRITICAL,
            ticker=None,
            message=(
                f"LOW BALANCE: account cash = ${balance_cents/100:.2f}  "
                f"(minimum: ${config.MIN_ACCOUNT_BALANCE_CENTS/100:.2f})  "
                f"No new positions will be opened."
            ),
            data={
                "balance_usd": round(balance_cents / 100, 2),
                "minimum_usd": round(config.MIN_ACCOUNT_BALANCE_CENTS / 100, 2),
            },
        )]

    def _check_fill_timeouts(self) -> list[Alert]:
        """Fire for any submitted limit order not filled within FILL_TIMEOUT_MINUTES."""
        alerts: list[Alert] = []
        now     = time.monotonic()
        timeout = FILL_TIMEOUT_MINUTES * 60.0

        for order_id, submitted_at in list(self._open_orders.items()):
            elapsed = now - submitted_at
            if elapsed < timeout:
                continue

            key = (AlertType.FILL_TIMEOUT, order_id)
            if self._suppressed(key):
                continue
            self._arm(key)

            alerts.append(Alert(
                alert_type=AlertType.FILL_TIMEOUT,
                severity=AlertSeverity.WARNING,
                ticker=None,
                message=(
                    f"FILL TIMEOUT: order {order_id[:8]}... has not filled "
                    f"after {elapsed/60:.0f} min. "
                    f"Consider cancelling and repricing closer to the ask."
                ),
                data={
                    "order_id":       order_id,
                    "elapsed_minutes": round(elapsed / 60, 1),
                    "timeout_minutes": FILL_TIMEOUT_MINUTES,
                },
            ))

        return alerts

    # ── Cooldown helpers ──────────────────────────────────────────────────────

    def _suppressed(self, key: tuple) -> bool:
        last = self._cooldown.get(key)
        if last is None:
            return False
        return (time.monotonic() - last) < ALERT_COOLDOWN_SECONDS

    def _arm(self, key: tuple, cooldown: float | None = None) -> None:
        """Record the fire time for this alert key."""
        self._cooldown[key] = time.monotonic()
        if cooldown is not None:
            # Temporarily override global cooldown for this key
            # (implemented by storing a negative offset so suppressed() sees full cooldown)
            self._cooldown[key] = time.monotonic() - (ALERT_COOLDOWN_SECONDS - cooldown)

    # ── Snapshot summary ──────────────────────────────────────────────────────

    def active_alert_summary(self) -> list[dict]:
        """
        Return a human-readable summary of all recently fired alerts
        (within the last 2× cooldown window).
        """
        now    = time.monotonic()
        window = ALERT_COOLDOWN_SECONDS * 2
        return [
            {
                "alert_type": k[0].value,
                "ticker":     k[1] if len(k) > 1 else "",
                "key":        k[1],
                "fired_ago_seconds": round(now - v, 0),
            }
            for k, v in self._cooldown.items()
            if (now - v) < window
        ]
