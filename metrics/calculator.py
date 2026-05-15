"""
metrics/calculator.py

Computes the four key trading metrics from stored data:
    1. Sharpe ratio     — risk-adjusted return
    2. Max drawdown     — largest peak-to-trough decline
    3. Fill rate        — % of limit orders that execute
    4. Edge-to-vig      — average edge expressed over the half-spread
"""

import math
import statistics
from typing import Any

import config
from metrics.metrics_store import MetricsStore


class MetricsCalculator:
    """Reads from MetricsStore and computes dashboard metrics."""

    def __init__(self, store: MetricsStore) -> None:
        self._store = store

    # ── Public API ────────────────────────────────────────────────────────────

    def all_metrics(self, days: int = config.SHARPE_WINDOW_DAYS) -> dict[str, Any]:
        """Return all four metrics as a single dict, ready for dashboard display."""
        return {
            "sharpe_ratio":    self.sharpe_ratio(days),
            "max_drawdown":    self.max_drawdown(days),
            "fill_rate":       self.fill_rate(),
            "avg_edge_to_vig": self.avg_edge_to_vig(days),
            "window_days":     days,
        }

    # ── Sharpe ratio ──────────────────────────────────────────────────────────

    def sharpe_ratio(self, days: int = config.SHARPE_WINDOW_DAYS) -> float | None:
        """
        Annualised Sharpe ratio over the given window.

        Uses daily P&L from closed trades. Returns None if insufficient data.
        """
        daily_pnl = self._daily_pnl_series(days)
        if len(daily_pnl) < 2:
            return None

        try:
            mean_daily = statistics.mean(daily_pnl)
            std_daily  = statistics.stdev(daily_pnl)
        except statistics.StatisticsError:
            return None

        if std_daily == 0:
            return None

        # Risk-free rate converted to daily (trading 365 days)
        daily_rfr   = config.RISK_FREE_RATE_ANNUAL / 365.0
        daily_sharpe = (mean_daily - daily_rfr) / std_daily
        annualised   = daily_sharpe * math.sqrt(365)

        return round(annualised, 4)

    # ── Max drawdown ──────────────────────────────────────────────────────────

    def max_drawdown(self, days: int = config.SHARPE_WINDOW_DAYS) -> dict[str, float]:
        """
        Maximum peak-to-trough drawdown over the equity curve.

        Returns:
            {
                "max_drawdown_pct": 0.073,   # 7.3% largest decline
                "max_drawdown_cents": 3650,  # absolute cents lost
            }
        """
        snapshots = self._store.get_equity_series(days)
        if not snapshots:
            return {"max_drawdown_pct": 0.0, "max_drawdown_cents": 0}

        values    = [s["equity_cents"] for s in snapshots]
        peak      = values[0]
        max_dd    = 0
        max_dd_c  = 0

        for v in values:
            if v > peak:
                peak = v
            dd   = peak - v
            dd_c = dd
            if dd > max_dd:
                max_dd   = dd
                max_dd_c = dd_c

        max_dd_pct = (max_dd / peak) if peak > 0 else 0.0

        return {
            "max_drawdown_pct":   round(max_dd_pct, 5),
            "max_drawdown_cents": max_dd_c,
        }

    # ── Fill rate ─────────────────────────────────────────────────────────────

    def fill_rate(self, days: int = 7) -> dict[str, Any]:
        """
        Percentage of limit orders that were executed (filled).

        A low fill rate may indicate limit prices are too passive.
        """
        return self._store.get_signal_fill_rate(days)

    # ── Edge-to-vig ───────────────────────────────────────────────────────────

    def avg_edge_to_vig(self, days: int = config.SHARPE_WINDOW_DAYS) -> float | None:
        """
        Average edge-to-vig ratio across all signals in the window.

        A ratio < 1 means we're trading below the vig — unprofitable on average.
        A ratio > 1.5 is a healthy target for a binary market strategy.
        """
        trades = self._store.get_closed_trades(days)
        ratios = [
            t.get("edge_to_vig")                  # stored at signal time
            for t in trades
            if t.get("edge_to_vig") is not None
        ]

        if not ratios:
            return None

        return round(statistics.mean(ratios), 4)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _daily_pnl_series(self, days: int) -> list[float]:
        """Aggregate closed trade P&L into daily buckets (cents)."""
        trades = self._store.get_closed_trades(days)
        if not trades:
            return []

        daily: dict[str, float] = {}
        for t in trades:
            pnl = t.get("realised_pnl") or 0
            # Convert µs timestamp to YYYY-MM-DD key
            day_key = self._us_to_day(t["ts_us"])
            daily[day_key] = daily.get(day_key, 0.0) + pnl

        return list(daily.values())

    @staticmethod
    def _us_to_day(ts_us: int) -> str:
        import datetime
        dt = datetime.datetime.utcfromtimestamp(ts_us / 1_000_000)
        return dt.strftime("%Y-%m-%d")
