"""
risk/kelly_calibrator.py

Deep Kelly calibration engine.

────────────────────────────────────────────────────────────────────────────────
WHY CALIBRATION MATTERS
────────────────────────────────────────────────────────────────────────────────

The Kelly criterion is optimal ONLY when the probability estimate p is
accurate. If your model says p=0.65 but the true win rate is 0.50, you are
over-betting by a factor of (0.65/0.50) = 1.3×, which produces a smaller
bankroll growth rate and higher variance than quarter-Kelly alone.

This module measures the gap between model confidence and actual outcomes
across four dimensions:

  1. OVERALL CALIBRATION
     Actual win rate vs average model confidence. The calibration_ratio
     (actual/model) should sit between 0.85 and 1.10 for each strategy.

  2. BRIER SCORE
     Mean squared error between model probability and binary outcome (0/1).
     Lower is better. A coin-flip model scores 0.25; a perfect model scores 0.00.
     Target: < 0.20 for a viable edge.

  3. RELIABILITY DIAGRAM DATA
     Buckets model probabilities into deciles (0.0–0.1, 0.1–0.2, … 0.9–1.0)
     and compares the mean confidence in each bucket against the actual win
     rate. A well-calibrated model produces points along the diagonal.
     Used to feed the dashboard chart.

  4. KELLY DIVISOR RECOMMENDATION
     Given observed calibration, recommends whether to increase or decrease
     KELLY_DIVISOR to account for model error:
       - If calibration_ratio < 0.80: increase divisor by 2
       - If calibration_ratio < 0.90: increase divisor by 1
       - If calibration_ratio > 1.10: could decrease divisor by 1 (cautiously)
       - Otherwise: current divisor is appropriate

────────────────────────────────────────────────────────────────────────────────
DATA SOURCE
────────────────────────────────────────────────────────────────────────────────

Reads from the Blotter's parent_trades and trades (legs) tables.
The `confidence` / `model_prob` field stored in strategy_meta at signal time
is the model's probability estimate. The actual outcome is the `resolution`
field set at settlement ("yes" or "no").

Only settled trades are used — they have known ground-truth outcomes.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

    calibrator = KellyCalibrator(blotter)

    # Full calibration report
    report = calibrator.full_report(days=30)
    calibrator.print_report(report)

    # Per-strategy recommendation
    for rec in report["strategies"]:
        print(f"{rec['strategy']}: {rec['recommendation']}")

    # Reliability diagram data (for charting)
    for row in report["reliability_diagram"]:
        print(f"bucket {row['bucket_center']:.1f}: "
              f"model={row['mean_confidence']:.2f} actual={row['actual_win_rate']:.2f}")
"""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import config
from metrics.blotter import Blotter


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class TradeDataPoint:
    """One settled trade with its model probability and actual outcome."""
    trade_id:      str
    strategy:      str
    model_prob:    float   # model's P(YES wins) at signal time
    side:          str     # "yes" | "no"
    won:           bool    # True if resolution matched the held side
    edge:          float
    edge_to_vig:   float
    hold_minutes:  float


@dataclass
class StrategyCalibration:
    """Calibration metrics for one strategy."""
    strategy:           str
    n:                  int       # number of settled trades
    model_win_rate:     float     # average model confidence
    actual_win_rate:    float     # empirical win rate
    calibration_ratio:  float     # actual / model
    brier_score:        float     # mean squared error
    log_loss:           float     # cross-entropy loss
    avg_edge:           float
    avg_edge_to_vig:    float
    recommended_divisor: int
    recommendation:     str

    @property
    def is_well_calibrated(self) -> bool:
        return 0.85 <= self.calibration_ratio <= 1.10

    @property
    def calibration_grade(self) -> str:
        r = self.calibration_ratio
        if r >= 0.95 and r <= 1.05:
            return "A  (excellent)"
        elif r >= 0.90 and r <= 1.10:
            return "B  (good)"
        elif r >= 0.85 and r <= 1.15:
            return "C  (acceptable)"
        elif r >= 0.80:
            return "D  (over-confident — reduce sizing)"
        else:
            return "F  (severely over-confident — stop trading this strategy)"


@dataclass
class ReliabilityBucket:
    """One decile bucket for the reliability diagram."""
    bucket_lo:      float
    bucket_hi:      float
    bucket_center:  float
    n:              int
    mean_confidence: float   # average model prob in this bucket
    actual_win_rate: float   # actual fraction that won
    gap:            float    # actual_win_rate - mean_confidence (+ = under-confident)


# ── Calibrator ────────────────────────────────────────────────────────────────

class KellyCalibrator:
    """
    Deep calibration engine for all strategies in the blotter.
    Only uses settled trades — they have known ground-truth outcomes.
    """

    def __init__(self, blotter: Blotter) -> None:
        self._blotter = blotter

    # ── Public API ────────────────────────────────────────────────────────────

    def full_report(self, days: int = 30) -> dict[str, Any]:
        """
        Build the complete calibration report.

        Returns a dict with:
            "strategies":         list[StrategyCalibration.to_dict()]
            "reliability_diagram": list[ReliabilityBucket.to_dict()]  (all strategies combined)
            "overall":            aggregate stats across all strategies
            "generated_at":       ISO timestamp
            "days":               window
        """
        data_points = self._load_data_points(days)

        if not data_points:
            return {
                "generated_at":     datetime.now(timezone.utc).isoformat(),
                "days":             days,
                "strategies":       [],
                "reliability_diagram": [],
                "overall":          {"n": 0, "message": "No settled trades in window."},
            }

        # Per-strategy calibration
        by_strategy: dict[str, list[TradeDataPoint]] = defaultdict(list)
        for dp in data_points:
            by_strategy[dp.strategy].append(dp)

        strategy_reports = []
        for strategy, points in sorted(by_strategy.items()):
            if len(points) < 5:
                continue  # too few for meaningful stats
            cal = self._compute_strategy_calibration(strategy, points)
            strategy_reports.append(cal)

        # Overall reliability diagram (all strategies pooled)
        reliability = self._build_reliability_diagram(data_points)

        # Overall aggregate
        all_model = [dp.model_prob for dp in data_points]
        all_actual = [1.0 if dp.won else 0.0 for dp in data_points]
        overall_cal_ratio = (
            statistics.mean(all_actual) / statistics.mean(all_model)
            if statistics.mean(all_model) > 0 else 0.0
        )

        return {
            "generated_at":      datetime.now(timezone.utc).isoformat(),
            "days":              days,
            "strategies":        [self._cal_to_dict(c) for c in strategy_reports],
            "reliability_diagram": [self._bucket_to_dict(b) for b in reliability],
            "overall": {
                "n":                      len(data_points),
                "avg_model_confidence":   round(statistics.mean(all_model), 4),
                "avg_actual_win_rate":    round(statistics.mean(all_actual), 4),
                "overall_cal_ratio":      round(overall_cal_ratio, 4),
                "overall_brier_score":    round(self._brier(all_model, all_actual), 5),
                "overall_log_loss":       round(self._log_loss(all_model, all_actual), 5),
                "well_calibrated_strategies": sum(
                    1 for c in strategy_reports if c.is_well_calibrated
                ),
                "total_strategies": len(strategy_reports),
            },
        }

    def strategy_recommendation(self, strategy: str, days: int = 30) -> str:
        """Quick recommendation string for one strategy."""
        data_points = [
            dp for dp in self._load_data_points(days)
            if dp.strategy == strategy
        ]
        if len(data_points) < 5:
            return f"Insufficient data ({len(data_points)} settled trades). Need ≥ 5."
        cal = self._compute_strategy_calibration(strategy, data_points)
        return cal.recommendation

    # ── Computation ───────────────────────────────────────────────────────────

    def _compute_strategy_calibration(
        self, strategy: str, points: list[TradeDataPoint]
    ) -> StrategyCalibration:
        model_probs  = [dp.model_prob for dp in points]
        outcomes     = [1.0 if dp.won else 0.0 for dp in points]
        edges        = [dp.edge for dp in points]
        etv          = [dp.edge_to_vig for dp in points]

        model_wr  = statistics.mean(model_probs)
        actual_wr = statistics.mean(outcomes)
        ratio     = actual_wr / model_wr if model_wr > 0 else 0.0
        brier     = self._brier(model_probs, outcomes)
        ll        = self._log_loss(model_probs, outcomes)
        avg_edge  = statistics.mean(edges)
        avg_etv   = statistics.mean(etv)

        # Recommended divisor
        current   = config.KELLY_DIVISOR
        if ratio < 0.80:
            rec_divisor = min(current + 2, 16)
            rec_text = (
                f"OVER-CONFIDENT (ratio={ratio:.2f}, Brier={brier:.3f}). "
                f"Increase KELLY_DIVISOR from {current} → {rec_divisor}. "
                f"Model wins {actual_wr*100:.0f}% but predicts {model_wr*100:.0f}%."
            )
        elif ratio < 0.90:
            rec_divisor = min(current + 1, 16)
            rec_text = (
                f"SLIGHTLY OVER-CONFIDENT (ratio={ratio:.2f}). "
                f"Consider increasing KELLY_DIVISOR from {current} → {rec_divisor}."
            )
        elif ratio > 1.10 and len(points) >= 20:
            rec_divisor = max(current - 1, 2)
            rec_text = (
                f"CONSERVATIVE (ratio={ratio:.2f}). "
                f"Model is under-confident. "
                f"Could reduce KELLY_DIVISOR from {current} → {rec_divisor} "
                f"after 30+ more trades to confirm."
            )
        else:
            rec_divisor = current
            rec_text = (
                f"WELL-CALIBRATED (ratio={ratio:.2f}, Brier={brier:.3f}). "
                f"Current KELLY_DIVISOR={current} is appropriate."
            )

        return StrategyCalibration(
            strategy=strategy,
            n=len(points),
            model_win_rate=round(model_wr, 4),
            actual_win_rate=round(actual_wr, 4),
            calibration_ratio=round(ratio, 4),
            brier_score=round(brier, 5),
            log_loss=round(ll, 5),
            avg_edge=round(avg_edge, 5),
            avg_edge_to_vig=round(avg_etv, 4),
            recommended_divisor=rec_divisor,
            recommendation=rec_text,
        )

    def _build_reliability_diagram(
        self, points: list[TradeDataPoint], n_buckets: int = 10
    ) -> list[ReliabilityBucket]:
        """
        Split model probabilities into decile buckets and compute actual win rate
        per bucket. The resulting list plots the reliability (calibration) curve.
        """
        buckets: list[ReliabilityBucket] = []
        step = 1.0 / n_buckets

        for i in range(n_buckets):
            lo  = i * step
            hi  = lo + step
            mid = (lo + hi) / 2.0

            bucket_points = [
                dp for dp in points if lo <= dp.model_prob < hi
            ]
            if not bucket_points:
                continue

            mean_conf   = statistics.mean(dp.model_prob for dp in bucket_points)
            actual_wr   = statistics.mean(1.0 if dp.won else 0.0 for dp in bucket_points)
            gap         = actual_wr - mean_conf

            buckets.append(ReliabilityBucket(
                bucket_lo=round(lo, 2),
                bucket_hi=round(hi, 2),
                bucket_center=round(mid, 2),
                n=len(bucket_points),
                mean_confidence=round(mean_conf, 4),
                actual_win_rate=round(actual_wr, 4),
                gap=round(gap, 4),
            ))

        return buckets

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load_data_points(self, days: int) -> list[TradeDataPoint]:
        """
        Load all settled trades and extract model probability + outcome pairs.
        """
        settled = self._blotter.query_trades(status="settled", days=days)
        points  = []

        for trade in settled:
            if not trade.resolution or not trade.strategy:
                continue

            # Fetch the entry leg to get strategy_meta with model_prob
            legs = self._blotter.get_legs_for_trade(trade.trade_id)
            entry_legs = [
                l for l in legs
                if l.trade_type in ("entry", "leg_1", "manual")
            ]
            if not entry_legs:
                continue

            leg  = entry_legs[0]
            meta = leg.strategy_meta or {}

            model_prob = (
                meta.get("model_prob")
                or meta.get("confidence")
                or meta.get("implied_fair")
            )
            if model_prob is None:
                continue

            try:
                model_prob = float(model_prob)
            except (TypeError, ValueError):
                continue

            if not (0.01 <= model_prob <= 0.99):
                continue

            side = leg.side
            won  = (trade.resolution == side)

            points.append(TradeDataPoint(
                trade_id=trade.trade_id,
                strategy=trade.strategy,
                model_prob=model_prob,
                side=side,
                won=won,
                edge=meta.get("edge", 0.0) or 0.0,
                edge_to_vig=meta.get("edge_to_vig", 0.0) or 0.0,
                hold_minutes=trade.hold_minutes or 0.0,
            ))

        return points

    # ── Metrics ───────────────────────────────────────────────────────────────

    @staticmethod
    def _brier(probs: list[float], outcomes: list[float]) -> float:
        """Mean squared error between predicted probabilities and binary outcomes."""
        if not probs:
            return 0.0
        return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)

    @staticmethod
    def _log_loss(probs: list[float], outcomes: list[float], eps: float = 1e-9) -> float:
        """Binary cross-entropy loss — lower is better."""
        if not probs:
            return 0.0
        total = 0.0
        for p, o in zip(probs, outcomes):
            p = max(min(p, 1 - eps), eps)
            total += o * math.log(p) + (1 - o) * math.log(1 - p)
        return -total / len(probs)

    # ── Serialisers ───────────────────────────────────────────────────────────

    @staticmethod
    def _cal_to_dict(c: StrategyCalibration) -> dict:
        return {
            "strategy":            c.strategy,
            "n":                   c.n,
            "model_win_rate":      c.model_win_rate,
            "actual_win_rate":     c.actual_win_rate,
            "calibration_ratio":   c.calibration_ratio,
            "calibration_grade":   c.calibration_grade,
            "brier_score":         c.brier_score,
            "log_loss":            c.log_loss,
            "avg_edge":            c.avg_edge,
            "avg_edge_to_vig":     c.avg_edge_to_vig,
            "recommended_divisor": c.recommended_divisor,
            "recommendation":      c.recommendation,
            "is_well_calibrated":  c.is_well_calibrated,
        }

    @staticmethod
    def _bucket_to_dict(b: ReliabilityBucket) -> dict:
        return {
            "bucket_lo":       b.bucket_lo,
            "bucket_hi":       b.bucket_hi,
            "bucket_center":   b.bucket_center,
            "n":               b.n,
            "mean_confidence": b.mean_confidence,
            "actual_win_rate": b.actual_win_rate,
            "gap":             b.gap,
        }

    # ── Terminal printer ──────────────────────────────────────────────────────

    def print_report(self, report: dict[str, Any] | None = None, days: int = 30) -> None:
        if report is None:
            report = self.full_report(days=days)

        w = 75
        ov = report.get("overall", {})

        print(f"\n{'═' * w}")
        print(f"  KELLY CALIBRATION REPORT — last {report['days']} days")
        print(f"  Generated: {report['generated_at'][:19]} UTC")
        print(f"{'═' * w}")

        if ov.get("n", 0) == 0:
            print(f"\n  {ov.get('message', 'No data.')}\n{'═'*w}\n")
            return

        print(f"\n  OVERALL  ({ov['n']} settled trades across "
              f"{ov['total_strategies']} strategies)")
        print(f"  {'Avg model confidence:':<35} {ov['avg_model_confidence']*100:.1f}%")
        print(f"  {'Avg actual win rate:':<35} {ov['avg_actual_win_rate']*100:.1f}%")
        print(f"  {'Overall calibration ratio:':<35} {ov['overall_cal_ratio']:.4f}")
        print(f"  {'Overall Brier score:':<35} {ov['overall_brier_score']:.5f}")
        print(f"  {'Overall log loss:':<35} {ov['overall_log_loss']:.5f}")
        print(f"  {'Well-calibrated strategies:':<35} "
              f"{ov['well_calibrated_strategies']} / {ov['total_strategies']}")

        # Per-strategy
        for s in report.get("strategies", []):
            ok    = "✓" if s["is_well_calibrated"] else "⚠"
            grade = s["calibration_grade"]
            print(f"\n  {ok} [{s['strategy']}]  n={s['n']}  "
                  f"Grade: {grade}")
            print(f"    {'Model win rate:':<30} {s['model_win_rate']*100:.1f}%")
            print(f"    {'Actual win rate:':<30} {s['actual_win_rate']*100:.1f}%")
            print(f"    {'Calibration ratio:':<30} {s['calibration_ratio']:.4f}")
            print(f"    {'Brier score:':<30} {s['brier_score']:.5f}  "
                  f"(target < 0.20)")
            print(f"    {'Log loss:':<30} {s['log_loss']:.5f}")
            print(f"    {'Avg edge:':<30} {s['avg_edge']*100:.2f}%")
            print(f"    {'Avg edge-to-vig:':<30} {s['avg_edge_to_vig']:.3f}×")
            print(f"    {'Recommended Kelly divisor:':<30} {s['recommended_divisor']}")
            print(f"    → {s['recommendation']}")

        # Reliability diagram (ASCII)
        buckets = report.get("reliability_diagram", [])
        if buckets:
            print(f"\n  RELIABILITY DIAGRAM  (model prob → actual win rate)")
            print(f"  {'BUCKET':<12}  {'N':>4}  {'MODEL':>7}  "
                  f"{'ACTUAL':>7}  {'GAP':>7}  CALIBRATION")
            print(f"  {'─' * 65}")
            for b in buckets:
                gap_str  = f"{b['gap']:+.3f}"
                bar_len  = int(abs(b['gap']) * 20)
                bar_dir  = "▶" if b['gap'] > 0 else "◀"
                bar      = bar_dir * min(bar_len, 10) if bar_len > 0 else "●"
                print(f"  {b['bucket_lo']:.1f}–{b['bucket_hi']:.1f}      "
                      f"{b['n']:>4}  {b['mean_confidence']*100:>6.1f}%  "
                      f"{b['actual_win_rate']*100:>6.1f}%  {gap_str:>7}  {bar}")
            print(f"\n  ▶ = under-confident (actual > model)  "
                  f"◀ = over-confident (actual < model)")

        print(f"\n{'═' * w}\n")
