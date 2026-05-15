"""
metrics/performance.py

Extended performance analytics built on top of the blotter.

Computes everything the calculator.py didn't cover:

    Win Rate          — % of closed trades that were profitable
    Profit Factor     — gross profit / gross loss (> 1.5 is healthy)
    Sortino Ratio     — like Sharpe but only penalises downside volatility
    Avg Hold Time     — mean minutes per closed trade, by strategy
    P&L by Category   — net profit broken down by Politics / Economics / etc.
    P&L by Strategy   — net profit broken down by kelly / green_up / arb / manual
    Best/Worst Day    — the single best and worst trading day
    Streak Analysis   — current win/loss streak and longest streak
    Kelly Calibration — compares model-predicted win rate vs actual win rate
                        to detect if the edge estimate is overstated
    Expectancy        — average $ won per trade (accounts for win rate + size)

All methods take a `days` parameter and read exclusively from the Blotter
(parent_trades table) — not from the legacy MetricsStore — so the data is
always based on the rich, fee-adjusted blotter records.
"""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import config
from metrics.blotter import Blotter, ParentRecord


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class WinRateStats:
    total_trades:   int
    wins:           int
    losses:         int
    breakeven:      int        # net_pnl == 0
    win_rate_pct:   float
    loss_rate_pct:  float
    avg_win_usd:    float
    avg_loss_usd:   float
    largest_win_usd:  float
    largest_loss_usd: float

    def summary(self) -> str:
        return (
            f"Win rate: {self.win_rate_pct:.1f}%  "
            f"({self.wins}W / {self.losses}L / {self.breakeven}BE)  "
            f"Avg win: ${self.avg_win_usd:.2f}  Avg loss: ${self.avg_loss_usd:.2f}  "
            f"Best: ${self.largest_win_usd:.2f}  Worst: ${self.largest_loss_usd:.2f}"
        )


@dataclass
class StreakStats:
    current_streak:       int     # positive = win streak, negative = loss streak
    current_streak_type:  str     # "win" | "loss" | "none"
    longest_win_streak:   int
    longest_loss_streak:  int
    last_10_results:      list[str]   # ["W","W","L","W",...] most recent first


@dataclass
class KellyCalibration:
    """
    Compares what Kelly fraction was used vs what the actual outcome justifies.

    If actual_win_rate << model_win_rate, Kelly is over-betting.
    Rule of thumb: if actual / model < 0.80, reduce KELLY_DIVISOR.
    """
    strategy:           str
    trade_count:        int
    model_win_rate:     float   # average confidence/implied_prob stored in strategy_meta
    actual_win_rate:    float   # empirical win rate from blotter
    calibration_ratio:  float   # actual / model — 1.0 is perfect, < 0.80 means over-confident
    recommendation:     str


# ── Performance engine ────────────────────────────────────────────────────────

class PerformanceAnalytics:
    """
    Rich performance analytics for the paper-trading review process.

    Usage:
        perf = PerformanceAnalytics(blotter)
        report = perf.full_report(days=30)
        perf.print_report(report)
    """

    def __init__(self, blotter: Blotter) -> None:
        self._blotter = blotter

    # ── Master report ─────────────────────────────────────────────────────────

    def full_report(self, days: int = 30) -> dict[str, Any]:
        """
        Compute every metric and return as a single structured dict.
        Suitable for logging, JSON export, or feeding a dashboard.
        """
        closed = self._blotter.query_trades(status="closed",  days=days)
        settled = self._blotter.query_trades(status="settled", days=days)
        all_closed = closed + settled

        return {
            "window_days":        days,
            "generated_at":       datetime.now(timezone.utc).isoformat(),
            "trade_count":        len(all_closed),
            "win_rate":           self._win_rate_stats(all_closed),
            "profit_factor":      self.profit_factor(all_closed),
            "expectancy_usd":     self.expectancy(all_closed),
            "sortino_ratio":      self.sortino_ratio(all_closed),
            "avg_hold_minutes":   self.avg_hold_time(all_closed),
            "pnl_by_strategy":    self._blotter.pnl_by_strategy(days=days),
            "pnl_by_category":    self._blotter.pnl_by_category(days=days),
            "streak":             self.streak_analysis(all_closed),
            "best_day":           self.best_day(all_closed),
            "worst_day":          self.worst_day(all_closed),
            "daily_pnl_series":   self.daily_pnl_series(all_closed),
            "kelly_calibration":  self.kelly_calibration(all_closed),
            "open_risk":          self._open_risk_summary(),
        }

    # ── Win rate ──────────────────────────────────────────────────────────────

    def win_rate(self, days: int = 30) -> WinRateStats:
        closed  = self._blotter.query_trades(status="closed",  days=days)
        settled = self._blotter.query_trades(status="settled", days=days)
        return self._win_rate_stats(closed + settled)

    def _win_rate_stats(self, trades: list[ParentRecord]) -> dict[str, Any]:
        if not trades:
            return {
                "total": 0, "wins": 0, "losses": 0, "breakeven": 0,
                "win_rate_pct": 0.0, "loss_rate_pct": 0.0,
                "avg_win_usd": 0.0, "avg_loss_usd": 0.0,
                "largest_win_usd": 0.0, "largest_loss_usd": 0.0,
            }

        pnls   = [t.net_pnl_cents or 0 for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        be     = [p for p in pnls if p == 0]
        n      = len(pnls)

        return {
            "total":           n,
            "wins":            len(wins),
            "losses":          len(losses),
            "breakeven":       len(be),
            "win_rate_pct":    round(len(wins) / n * 100, 2) if n else 0.0,
            "loss_rate_pct":   round(len(losses) / n * 100, 2) if n else 0.0,
            "avg_win_usd":     round(statistics.mean(wins) / 100, 2) if wins else 0.0,
            "avg_loss_usd":    round(statistics.mean(losses) / 100, 2) if losses else 0.0,
            "largest_win_usd":  round(max(wins) / 100, 2) if wins else 0.0,
            "largest_loss_usd": round(min(losses) / 100, 2) if losses else 0.0,
        }

    # ── Profit factor ─────────────────────────────────────────────────────────

    def profit_factor(self, trades: list[ParentRecord] | None = None, days: int = 30) -> float | None:
        """
        Gross profit / gross loss.

        Interpretation:
            > 2.0  excellent
            1.5–2.0  good
            1.0–1.5  marginal
            < 1.0  losing strategy

        Returns None if no losing trades exist (undefined).
        """
        if trades is None:
            closed  = self._blotter.query_trades(status="closed",  days=days)
            settled = self._blotter.query_trades(status="settled", days=days)
            trades  = closed + settled

        gross_profit = sum(t.net_pnl_cents for t in trades if (t.net_pnl_cents or 0) > 0)
        gross_loss   = abs(sum(t.net_pnl_cents for t in trades if (t.net_pnl_cents or 0) < 0))

        if gross_loss == 0:
            return None   # no losing trades — undefined (not infinite)
        return round(gross_profit / gross_loss, 3)

    # ── Expectancy ────────────────────────────────────────────────────────────

    def expectancy(self, trades: list[ParentRecord] | None = None, days: int = 30) -> float | None:
        """
        Average dollar won per trade.

        Expectancy = (Win Rate × Avg Win) + (Loss Rate × Avg Loss)

        Positive expectancy means the strategy makes money on average.
        A good target for binary prediction markets: > $0.50 per trade.
        """
        if trades is None:
            closed  = self._blotter.query_trades(status="closed",  days=days)
            settled = self._blotter.query_trades(status="settled", days=days)
            trades  = closed + settled

        if not trades:
            return None

        pnls     = [t.net_pnl_cents or 0 for t in trades]
        n        = len(pnls)
        wins     = [p for p in pnls if p > 0]
        losses   = [p for p in pnls if p < 0]

        win_rate  = len(wins) / n
        loss_rate = len(losses) / n
        avg_win   = statistics.mean(wins) if wins else 0
        avg_loss  = statistics.mean(losses) if losses else 0

        expectancy_cents = (win_rate * avg_win) + (loss_rate * avg_loss)
        return round(expectancy_cents / 100, 4)

    # ── Sortino ratio ─────────────────────────────────────────────────────────

    def sortino_ratio(self, trades: list[ParentRecord] | None = None, days: int = 30) -> float | None:
        """
        Sortino ratio — annualised return / downside deviation.

        Unlike Sharpe, only penalises volatility below the target return (0).
        More appropriate for binary outcome distributions where large wins
        are not a problem.

        Sortino = (Mean Daily Return - Risk Free Rate) / Downside Deviation
        """
        if trades is None:
            closed  = self._blotter.query_trades(status="closed",  days=days)
            settled = self._blotter.query_trades(status="settled", days=days)
            trades  = closed + settled

        daily = self._to_daily_pnl(trades)
        if len(daily) < 2:
            return None

        daily_values = list(daily.values())
        mean_daily   = statistics.mean(daily_values)
        daily_rfr    = config.RISK_FREE_RATE_ANNUAL / 365.0

        # Downside deviation: std of returns below 0 (target = 0)
        downside = [min(r, 0) for r in daily_values]
        downside_sq = [d ** 2 for d in downside]
        if not any(d < 0 for d in downside):
            return None   # no losing days — undefined

        downside_dev = math.sqrt(statistics.mean(downside_sq))
        if downside_dev == 0:
            return None

        sortino = (mean_daily - daily_rfr) / downside_dev * math.sqrt(365)
        return round(sortino, 4)

    # ── Average hold time ─────────────────────────────────────────────────────

    def avg_hold_time(self, trades: list[ParentRecord] | None = None, days: int = 30) -> dict[str, Any]:
        """
        Average hold time in minutes, broken down by strategy.
        """
        if trades is None:
            closed  = self._blotter.query_trades(status="closed",  days=days)
            settled = self._blotter.query_trades(status="settled", days=days)
            trades  = closed + settled

        if not trades:
            return {"overall_minutes": None, "by_strategy": {}}

        all_hold   = [t.hold_minutes for t in trades if t.hold_minutes]
        by_strategy: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            if t.hold_minutes:
                by_strategy[t.strategy].append(t.hold_minutes)

        return {
            "overall_minutes": round(statistics.mean(all_hold), 1) if all_hold else None,
            "overall_hours":   round(statistics.mean(all_hold) / 60, 2) if all_hold else None,
            "by_strategy": {
                strat: {
                    "avg_minutes": round(statistics.mean(holds), 1),
                    "min_minutes": round(min(holds), 1),
                    "max_minutes": round(max(holds), 1),
                    "trade_count": len(holds),
                }
                for strat, holds in by_strategy.items()
            },
        }

    # ── Streak analysis ───────────────────────────────────────────────────────

    def streak_analysis(self, trades: list[ParentRecord] | None = None, days: int = 30) -> dict[str, Any]:
        """
        Current win/loss streak and historical longest streaks.
        Trades ordered oldest-first for streak calculation.
        """
        if trades is None:
            closed  = self._blotter.query_trades(status="closed",  days=days)
            settled = self._blotter.query_trades(status="settled", days=days)
            trades  = closed + settled

        if not trades:
            return {
                "current_streak": 0, "current_type": "none",
                "longest_win_streak": 0, "longest_loss_streak": 0,
                "last_10": [],
            }

        # Sort oldest first
        ordered = sorted(trades, key=lambda t: t.entry_time)
        results = ["W" if (t.net_pnl_cents or 0) > 0 else ("L" if (t.net_pnl_cents or 0) < 0 else "B")
                   for t in ordered]

        # Current streak (from end)
        current_type  = results[-1] if results else "B"
        current_count = 0
        for r in reversed(results):
            if r == current_type:
                current_count += 1
            else:
                break
        if current_type == "B":
            current_count = 0

        # Longest streaks
        max_win = max_loss = cur_win = cur_loss = 0
        for r in results:
            if r == "W":
                cur_win  += 1
                cur_loss  = 0
            elif r == "L":
                cur_loss += 1
                cur_win   = 0
            else:
                cur_win = cur_loss = 0
            max_win  = max(max_win,  cur_win)
            max_loss = max(max_loss, cur_loss)

        return {
            "current_streak":       current_count if current_type == "W" else -current_count,
            "current_type":         "win" if current_type == "W" else ("loss" if current_type == "L" else "none"),
            "longest_win_streak":   max_win,
            "longest_loss_streak":  max_loss,
            "last_10":              results[-10:][::-1],   # most recent first
        }

    # ── Best / worst day ──────────────────────────────────────────────────────

    def best_day(self, trades: list[ParentRecord] | None = None, days: int = 30) -> dict | None:
        return self._extreme_day(trades, days, best=True)

    def worst_day(self, trades: list[ParentRecord] | None = None, days: int = 30) -> dict | None:
        return self._extreme_day(trades, days, best=False)

    def _extreme_day(self, trades, days, best: bool) -> dict | None:
        if trades is None:
            closed  = self._blotter.query_trades(status="closed",  days=days)
            settled = self._blotter.query_trades(status="settled", days=days)
            trades  = closed + settled

        daily = self._to_daily_pnl(trades)
        if not daily:
            return None

        target = max(daily, key=daily.get) if best else min(daily, key=daily.get)
        return {
            "date":    target,
            "pnl_usd": round(daily[target] / 100, 2),
        }

    # ── Daily P&L series ──────────────────────────────────────────────────────

    def daily_pnl_series(self, trades: list[ParentRecord] | None = None, days: int = 30) -> list[dict]:
        """
        Returns a time-ordered list of {date, pnl_usd, cumulative_pnl_usd}
        suitable for charting.
        """
        if trades is None:
            closed  = self._blotter.query_trades(status="closed",  days=days)
            settled = self._blotter.query_trades(status="settled", days=days)
            trades  = closed + settled

        daily  = self._to_daily_pnl(trades)
        result = []
        cumulative = 0
        for date in sorted(daily):
            cumulative += daily[date]
            result.append({
                "date":           date,
                "pnl_usd":        round(daily[date] / 100, 2),
                "cumulative_usd": round(cumulative / 100, 2),
            })
        return result

    # ── Kelly calibration ─────────────────────────────────────────────────────

    def kelly_calibration(self, trades: list[ParentRecord] | None = None, days: int = 30) -> list[dict]:
        """
        For each strategy, compare the model's predicted win probability
        (stored in strategy_meta at signal time) against the actual win rate.

        A calibration_ratio < 0.80 means the model is overconfident —
        the actual win rate is meaningfully below what Kelly assumed.
        Recommendation: increase KELLY_DIVISOR or tighten MIN_EDGE_TO_VIG.

        A ratio between 0.90 and 1.10 is well-calibrated.
        A ratio > 1.10 means the model is conservative — could size up.
        """
        if trades is None:
            closed  = self._blotter.query_trades(status="closed",  days=days)
            settled = self._blotter.query_trades(status="settled", days=days)
            trades  = closed + settled

        # Group by strategy
        by_strategy: dict[str, list[ParentRecord]] = defaultdict(list)
        for t in trades:
            if t.strategy:
                by_strategy[t.strategy].append(t)

        results = []
        for strategy, strat_trades in by_strategy.items():
            if len(strat_trades) < 5:
                continue   # too few trades for meaningful calibration

            # Pull model win rate from legs' strategy_meta confidence field
            model_probs = []
            for trade in strat_trades:
                legs = self._blotter.get_legs_for_trade(trade.trade_id)
                for leg in legs:
                    conf = leg.strategy_meta.get("model_prob") or leg.strategy_meta.get("confidence")
                    if conf is not None:
                        model_probs.append(float(conf))
                        break   # one entry leg per trade is enough

            if not model_probs:
                continue

            model_win_rate  = statistics.mean(model_probs)
            actual_wins     = sum(1 for t in strat_trades if (t.net_pnl_cents or 0) > 0)
            actual_win_rate = actual_wins / len(strat_trades)
            ratio           = actual_win_rate / model_win_rate if model_win_rate > 0 else 0

            if ratio < 0.80:
                recommendation = (
                    f"Model is OVERCONFIDENT (ratio={ratio:.2f}). "
                    f"Consider increasing KELLY_DIVISOR from {config.KELLY_DIVISOR} to {config.KELLY_DIVISOR + 2} "
                    f"or tightening MIN_EDGE_TO_VIG."
                )
            elif ratio > 1.10:
                recommendation = (
                    f"Model is CONSERVATIVE (ratio={ratio:.2f}). "
                    f"Could reduce KELLY_DIVISOR from {config.KELLY_DIVISOR} to {max(config.KELLY_DIVISOR - 1, 2)} "
                    f"if performance is consistent over 30+ trades."
                )
            else:
                recommendation = f"Model is WELL-CALIBRATED (ratio={ratio:.2f}). No adjustment needed."

            results.append({
                "strategy":          strategy,
                "trade_count":       len(strat_trades),
                "model_win_rate":    round(model_win_rate, 4),
                "actual_win_rate":   round(actual_win_rate, 4),
                "calibration_ratio": round(ratio, 4),
                "recommendation":    recommendation,
            })

        return results

    # ── Open risk summary ─────────────────────────────────────────────────────

    def _open_risk_summary(self) -> dict[str, Any]:
        """Summary of currently open trades and their cost basis."""
        open_trades = self._blotter.open_positions_summary()
        total_at_risk = sum(t["total_cost_cents"] for t in open_trades)
        by_category: dict[str, int] = defaultdict(int)
        for t in open_trades:
            by_category[t["category"]] += t["total_cost_cents"]
        return {
            "open_trade_count":   len(open_trades),
            "total_at_risk_usd":  round(total_at_risk / 100, 2),
            "by_category":        {k: round(v / 100, 2) for k, v in by_category.items()},
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _to_daily_pnl(trades: list[ParentRecord]) -> dict[str, float]:
        """Aggregate net_pnl_cents into {YYYY-MM-DD: cents} buckets."""
        daily: dict[str, float] = defaultdict(float)
        for t in trades:
            pnl = t.net_pnl_cents or 0
            day = t.entry_time[:10]   # YYYY-MM-DD
            daily[day] += pnl
        return dict(daily)

    # ── Print report ──────────────────────────────────────────────────────────

    def print_report(self, report: dict[str, Any] | None = None, days: int = 30) -> None:
        """Print a formatted performance report to stdout."""
        if report is None:
            report = self.full_report(days=days)

        w = 75
        print(f"\n{'═' * w}")
        print(f"  PERFORMANCE REPORT  —  Last {report['window_days']} days  "
              f"({report['trade_count']} closed trades)")
        print(f"{'═' * w}")

        # Win rate
        wr = report["win_rate"]
        print(f"\n  WIN RATE & OUTCOMES")
        print(f"  {'Trades:':<30} {wr.get('total', 0)}  "
              f"({wr.get('wins', 0)}W / {wr.get('losses', 0)}L / {wr.get('breakeven', 0)}BE)")
        print(f"  {'Win rate:':<30} {wr.get('win_rate_pct', 0):.1f}%")
        print(f"  {'Avg win:':<30} ${wr.get('avg_win_usd', 0):.2f}")
        print(f"  {'Avg loss:':<30} ${wr.get('avg_loss_usd', 0):.2f}")
        print(f"  {'Largest win:':<30} ${wr.get('largest_win_usd', 0):.2f}")
        print(f"  {'Largest loss:':<30} ${wr.get('largest_loss_usd', 0):.2f}")

        # Key ratios
        pf = report.get("profit_factor")
        print(f"\n  KEY RATIOS")
        print(f"  {'Profit factor:':<30} {pf:.3f}" if pf else f"  {'Profit factor:':<30} —")
        print(f"  {'Expectancy:':<30} ${report.get('expectancy_usd') or 0:.4f} / trade")
        so = report.get("sortino_ratio")
        print(f"  {'Sortino ratio:':<30} {so:.4f}" if so else f"  {'Sortino ratio:':<30} —")

        # Hold time
        ht = report.get("avg_hold_minutes", {})
        print(f"\n  HOLD TIME")
        print(f"  {'Avg hold (all):':<30} {ht.get('overall_minutes') or '—'} min")
        for strat, vals in (ht.get("by_strategy") or {}).items():
            print(f"  {'  ' + strat[:26]:<30} {vals['avg_minutes']:.0f} min  "
                  f"(min={vals['min_minutes']:.0f}  max={vals['max_minutes']:.0f})")

        # Streak
        st = report.get("streak", {})
        print(f"\n  STREAKS")
        cur = st.get("current_streak", 0)
        cur_type = st.get("current_type", "none")
        print(f"  {'Current streak:':<30} {abs(cur)} {cur_type}")
        print(f"  {'Longest win streak:':<30} {st.get('longest_win_streak', 0)}")
        print(f"  {'Longest loss streak:':<30} {st.get('longest_loss_streak', 0)}")
        print(f"  {'Last 10 results:':<30} {' '.join(st.get('last_10', []))}")

        # Best / worst day
        bd = report.get("best_day")
        wd = report.get("worst_day")
        print(f"\n  DAILY EXTREMES")
        if bd:
            print(f"  {'Best day:':<30} {bd['date']}  ${bd['pnl_usd']:+.2f}")
        if wd:
            print(f"  {'Worst day:':<30} {wd['date']}  ${wd['pnl_usd']:+.2f}")

        # P&L by strategy
        print(f"\n  P&L BY STRATEGY")
        for row in report.get("pnl_by_strategy", []):
            pnl_str = f"${row['total_pnl_usd']:+.2f}"
            print(f"  {row['group'][:28]:<28}  {row['trade_count']:>3} trades  "
                  f"{row['win_rate_pct']:>5.1f}% win  {pnl_str:>10}  "
                  f"avg {row['avg_hold_minutes']:.0f}m")

        # P&L by category
        print(f"\n  P&L BY CATEGORY")
        for row in report.get("pnl_by_category", []):
            pnl_str = f"${row['total_pnl_usd']:+.2f}"
            print(f"  {row['group'][:28]:<28}  {row['trade_count']:>3} trades  "
                  f"{row['win_rate_pct']:>5.1f}% win  {pnl_str:>10}")

        # Kelly calibration
        calib = report.get("kelly_calibration", [])
        if calib:
            print(f"\n  KELLY CALIBRATION")
            for c in calib:
                ratio_str = f"{c['calibration_ratio']:.2f}"
                status = "✓" if 0.80 <= c["calibration_ratio"] <= 1.10 else "⚠"
                print(f"  {status} {c['strategy'][:25]:<25}  "
                      f"model={c['model_win_rate']*100:.0f}%  "
                      f"actual={c['actual_win_rate']*100:.0f}%  "
                      f"ratio={ratio_str}")
                print(f"    → {c['recommendation']}")

        # Open risk
        risk = report.get("open_risk", {})
        print(f"\n  OPEN RISK")
        print(f"  {'Open positions:':<30} {risk.get('open_trade_count', 0)}")
        print(f"  {'Total at risk:':<30} ${risk.get('total_at_risk_usd', 0):.2f}")
        for cat, amt in (risk.get("by_category") or {}).items():
            print(f"  {'  ' + cat:<30} ${amt:.2f}")

        print(f"\n{'═' * w}\n")
