"""
discovery/discovery_presets.py

Strategy-aligned discovery defaults for ticker selection.

Presets fill filter/rank fields that were not set explicitly on the CLI or in
KALSHI_DISCOVER_* env vars. Use --discover-preset NAME to force a preset, or rely
on auto-selection when --discover is used with --strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from discovery.ticker_selector import TickerCriteria

RankBy = Literal["volume", "fee_adjusted_roi"]


@dataclass(frozen=True)
class DiscoveryPreset:
    """Default discovery filters for a trading strategy."""

    name: str
    description: str
    top_n: int = 10
    min_volume_24h: int = 0
    min_yes_ask: int | None = None
    max_yes_ask: int | None = None
    max_spread: int | None = None
    min_fee_adjusted_roi_pct: float | None = None
    rank_by: RankBy = "volume"
    activity_hours: float | None = None
    full_scan: bool = False


STRATEGY_DISCOVERY_PRESETS: dict[str, DiscoveryPreset] = {
    "high_prob": DiscoveryPreset(
        name="high_prob",
        description="High implied P(YES), modest payout, fee-aware ROI ranking",
        top_n=10,
        min_volume_24h=200,
        min_yes_ask=85,
        max_yes_ask=97,
        max_spread=8,
        min_fee_adjusted_roi_pct=1.5,
        rank_by="fee_adjusted_roi",
        activity_hours=24.0,
        full_scan=True,
    ),
    "green_up": DiscoveryPreset(
        name="green_up",
        description="Underdog YES entries (low ask), active markets",
        top_n=10,
        min_volume_24h=500,
        max_yes_ask=35,
        max_spread=12,
        rank_by="volume",
        activity_hours=48.0,
    ),
    "kelly": DiscoveryPreset(
        name="kelly",
        description="Liquid markets with tradeable spreads for model edge",
        top_n=15,
        min_volume_24h=100,
        max_spread=10,
        rank_by="volume",
    ),
    "arb": DiscoveryPreset(
        name="arb",
        description="High-volume markets in the same category for pair scanning",
        top_n=25,
        min_volume_24h=50,
        max_spread=15,
        rank_by="volume",
        full_scan=True,
    ),
}


def preset_for_strategy(strategy: str) -> str | None:
    """Map bot strategy name to a discovery preset, if one exists."""
    key = strategy.strip().lower()
    if key in STRATEGY_DISCOVERY_PRESETS:
        return key
    return None


def apply_preset(
    criteria: TickerCriteria,
    preset_name: str,
    *,
    skip_fields: frozenset[str] = frozenset(),
) -> TickerCriteria:
    """
    Overlay preset defaults onto criteria.

    Fields listed in skip_fields were set explicitly by the user (CLI) and are
    not overwritten.
    """
    preset = STRATEGY_DISCOVERY_PRESETS.get(preset_name)
    if preset is None:
        raise ValueError(
            f"Unknown discovery preset {preset_name!r}. "
            f"Choose: {', '.join(STRATEGY_DISCOVERY_PRESETS)}"
        )

    def _pick(field: str, current, default):
        if field in skip_fields:
            return current
        return default if current is None else current

    # top_n / min_volume: only apply preset when still at generic defaults
    top_n = criteria.top_n
    if "top_n" not in skip_fields and top_n == 10:
        top_n = preset.top_n

    min_vol = criteria.min_volume_24h
    if "min_volume_24h" not in skip_fields and min_vol == 0:
        min_vol = preset.min_volume_24h

    full_scan = criteria.full_scan
    if "full_scan" not in skip_fields and not full_scan and preset.full_scan:
        full_scan = preset.full_scan

    activity = _pick("activity_hours", criteria.activity_hours, preset.activity_hours)

    return replace(
        criteria,
        top_n=top_n,
        min_volume_24h=min_vol,
        min_yes_ask=_pick("min_yes_ask", criteria.min_yes_ask, preset.min_yes_ask),
        max_yes_ask=_pick("max_yes_ask", criteria.max_yes_ask, preset.max_yes_ask),
        max_spread=_pick("max_spread", criteria.max_spread, preset.max_spread),
        min_fee_adjusted_roi_pct=_pick(
            "min_fee_adjusted_roi_pct",
            criteria.min_fee_adjusted_roi_pct,
            preset.min_fee_adjusted_roi_pct,
        ),
        rank_by=criteria.rank_by if "rank_by" in skip_fields else preset.rank_by,
        activity_hours=activity,
        full_scan=full_scan,
        preset_name=preset.name,
    )
