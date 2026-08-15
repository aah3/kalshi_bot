"""
orchestration/strategy_instance.py

Declarative StrategyInstance schema (YAML or JSON) for continuous single-strategy
workers. Maps 1:1 onto existing CLI / build_strategy knobs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from strategy.factory import VALID_STRATEGIES

PriceMode = Literal[
    "passive",
    "cross_spread",
    "market",
    "limit_at_ask",
    "limit_at_bid",
    "limit_at_mid",
    "limit_offset",
]

UniverseMode = Literal["discover", "static", "hybrid"]
DropAction = Literal["drop", "drop_if_flat", "keep"]
LifecycleAction = Literal["keep_running", "exit"]
ShutdownAction = Literal["cancel_resting", "flatten", "hold"]
RankBy = Literal["volume", "fee_adjusted_roi", "screener"]

PRICE_MODES = frozenset({
    "passive", "cross_spread", "market",
    "limit_at_ask", "limit_at_bid", "limit_at_mid", "limit_offset",
})
GREEN_UP_PARAM_KEYS = frozenset({
    "entry_max", "no_entry_max", "hedge_trigger", "hedge_offset", "hedge_mode",
    "stop_loss", "gu_entry_mode", "gu_exit_mode", "gu_limit_offset",
    "gu_max_spread", "gu_hedge_style", "gu_max_cycles",
})
HIGH_PROB_PARAM_KEYS = frozenset({
    "hp_min_yes_ask", "hp_max_yes_ask", "hp_entry_mode", "hp_exit_mode",
    "hp_post_fill", "hp_stake_cents", "hp_take_profit_pct", "hp_take_profit_offset",
    "hp_stop_loss", "hp_stop_loss_cents", "hp_max_spread", "hp_tp_style",
    "hp_max_cycles", "model_probs", "require_model_edge",
})
MEAN_REV_PARAM_KEYS = frozenset({
    "mr_lookback", "mr_min_samples", "mr_entry_deviation", "mr_take_profit_offset",
    "mr_stop_loss_cents", "mr_min_volatility", "mr_entry_max", "mr_entry_min",
    "mr_short_min_yes_ask", "mr_short_max_yes_ask", "mr_stake_cents", "mr_max_spread",
    "mr_trade_direction", "mr_exit_target", "mr_entry_mode", "mr_exit_mode",
    "mr_post_fill", "mr_max_cycles", "mr_limit_offset",
})
KELLY_PARAM_KEYS = frozenset({"model_probs"})
ARB_PARAM_KEYS = frozenset({"comp_pairs"})

STRATEGY_PARAM_KEYS: dict[str, frozenset[str]] = {
    "green_up": GREEN_UP_PARAM_KEYS,
    "high_prob": HIGH_PROB_PARAM_KEYS,
    "mean_reversion": MEAN_REV_PARAM_KEYS,
    "kelly": KELLY_PARAM_KEYS,
    "arb": ARB_PARAM_KEYS,
}


class StrategyInstanceError(ValueError):
    """Invalid StrategyInstance config."""


@dataclass(frozen=True)
class DropPolicy:
    closed: DropAction = "drop"
    fail_filters: DropAction = "drop_if_flat"
    not_in_top_n: DropAction = "drop_if_flat"


@dataclass(frozen=True)
class DiscoverConfig:
    category: str = "Trending"
    preset: str | None = None
    top_n: int = 10
    min_volume_24h: int = 0
    min_yes_ask: int | None = None
    max_yes_ask: int | None = None
    max_spread: int | None = None
    activity_hours: float | None = None
    max_minutes_to_close: float | None = None
    rank_by: RankBy = "volume"
    min_fee_adjusted_roi_pct: float | None = None
    full_scan: bool = False
    tradeable_only: bool = True
    live_only: bool = True
    tag: str | None = None
    sport: str | None = None
    competition: str | None = None
    scope: str | None = None
    series_ticker: str | None = None


@dataclass(frozen=True)
class UniverseConfig:
    mode: UniverseMode = "discover"
    refresh_seconds: int | None = None
    max_watchlist: int = 10
    static_tickers: tuple[str, ...] = ()
    drop_policy: DropPolicy = field(default_factory=DropPolicy)
    discover: DiscoverConfig = field(default_factory=DiscoverConfig)


@dataclass(frozen=True)
class RiskSleeve:
    max_position_cents: int | None = None
    max_concurrent_positions: int | None = None
    daily_loss_limit_cents: int | None = None
    max_drawdown_pct: float | None = None
    max_sector_concentration: float | None = None


@dataclass(frozen=True)
class PersistenceConfig:
    db_path: str | None = None
    log_file: str | None = None


@dataclass(frozen=True)
class LifecycleConfig:
    on_flat: LifecycleAction = "keep_running"
    on_all_settled: LifecycleAction = "keep_running"
    on_shutdown: ShutdownAction = "cancel_resting"
    flatten_before: str | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    monitor_interval: float | None = None
    quiet: bool = False
    auto_take_profit: bool = False


@dataclass(frozen=True)
class ScheduleConfig:
    """Phase 2 stub — null schedule means always on."""

    windows: tuple[dict[str, Any], ...] | None = None
    stop_new_entries_at: str | None = None


@dataclass(frozen=True)
class StrategyInstance:
    id: str
    strategy: str
    enabled: bool = True
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    strategy_params: dict[str, Any] = field(default_factory=dict)
    risk: RiskSleeve = field(default_factory=RiskSleeve)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    schedule: ScheduleConfig | None = None


def load_strategy_instance(
    path: str | Path,
    *,
    instance_id: str | None = None,
) -> StrategyInstance:
    """
    Load a StrategyInstance from YAML or JSON.

    Supports:
      - bare instance document (fields at top level)
      - portfolio document with ``instances: [...]`` (select by instance_id
        or first enabled entry)
    """
    path = Path(path)
    if not path.is_file():
        raise StrategyInstanceError(f"Instance file not found: {path}")

    raw = _load_raw(path)
    if not isinstance(raw, dict):
        raise StrategyInstanceError(f"Instance file must be a mapping: {path}")

    if "instances" in raw:
        instances = raw["instances"]
        if not isinstance(instances, list) or not instances:
            raise StrategyInstanceError(
                f"Portfolio file {path} has empty or invalid 'instances'"
            )
        chosen = _select_from_portfolio(instances, instance_id)
    else:
        if instance_id and raw.get("id") not in (None, instance_id):
            raise StrategyInstanceError(
                f"Instance id mismatch: file has {raw.get('id')!r}, "
                f"requested {instance_id!r}"
            )
        chosen = raw

    return parse_strategy_instance(chosen)


def parse_strategy_instance(data: dict[str, Any]) -> StrategyInstance:
    """Validate and build a StrategyInstance from a plain dict."""
    if not isinstance(data, dict):
        raise StrategyInstanceError("StrategyInstance must be a mapping")

    instance_id = data.get("id")
    if not instance_id or not isinstance(instance_id, str):
        raise StrategyInstanceError("StrategyInstance.id is required (non-empty string)")
    instance_id = instance_id.strip()
    if not instance_id:
        raise StrategyInstanceError("StrategyInstance.id is required (non-empty string)")

    strategy = str(data.get("strategy", "")).strip().lower()
    if strategy not in VALID_STRATEGIES:
        raise StrategyInstanceError(
            f"Unknown strategy {strategy!r}. Choose: {', '.join(VALID_STRATEGIES)}"
        )

    enabled = bool(data.get("enabled", True))
    universe = _parse_universe(data.get("universe") or {})
    params = data.get("strategy_params") or {}
    if not isinstance(params, dict):
        raise StrategyInstanceError("strategy_params must be a mapping")
    params = dict(params)
    _validate_strategy_params(strategy, params)

    risk = _parse_dataclass(RiskSleeve, data.get("risk") or {}, "risk")
    persistence = _parse_dataclass(
        PersistenceConfig, data.get("persistence") or {}, "persistence"
    )
    lifecycle = _parse_dataclass(
        LifecycleConfig, data.get("lifecycle") or {}, "lifecycle"
    )
    runtime = _parse_dataclass(RuntimeConfig, data.get("runtime") or {}, "runtime")

    schedule_raw = data.get("schedule")
    schedule = None
    if schedule_raw is not None:
        if not isinstance(schedule_raw, dict):
            raise StrategyInstanceError("schedule must be a mapping or null")
        windows = schedule_raw.get("windows")
        if windows is not None and not isinstance(windows, list):
            raise StrategyInstanceError("schedule.windows must be a list or null")
        schedule = ScheduleConfig(
            windows=tuple(windows) if windows else None,
            stop_new_entries_at=schedule_raw.get("stop_new_entries_at"),
        )

    if universe.mode == "static" and not universe.static_tickers:
        raise StrategyInstanceError(
            "universe.mode=static requires universe.static_tickers"
        )
    if universe.refresh_seconds is not None and universe.refresh_seconds <= 0:
        raise StrategyInstanceError("universe.refresh_seconds must be > 0 or null")
    if universe.max_watchlist < 1:
        raise StrategyInstanceError("universe.max_watchlist must be >= 1")

    return StrategyInstance(
        id=instance_id,
        strategy=strategy,
        enabled=enabled,
        universe=universe,
        strategy_params=params,
        risk=risk,
        persistence=persistence,
        lifecycle=lifecycle,
        runtime=runtime,
        schedule=schedule,
    )


def _select_from_portfolio(
    instances: list[Any],
    instance_id: str | None,
) -> dict[str, Any]:
    parsed: list[dict[str, Any]] = []
    for item in instances:
        if not isinstance(item, dict):
            raise StrategyInstanceError("Each portfolio instance must be a mapping")
        parsed.append(item)

    if instance_id:
        for item in parsed:
            if item.get("id") == instance_id:
                return item
        raise StrategyInstanceError(
            f"No instance with id={instance_id!r} in portfolio file"
        )

    for item in parsed:
        if item.get("enabled", True):
            return item
    raise StrategyInstanceError("No enabled instances in portfolio file")


def _load_raw(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise StrategyInstanceError(
                "PyYAML is required for .yaml/.yml instance files — "
                "pip install PyYAML"
            ) from exc
        return yaml.safe_load(text)
    if suffix == ".json":
        return json.loads(text)
    # Peek: try JSON first, then YAML
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:
            raise StrategyInstanceError(
                f"Cannot parse {path}: install PyYAML or use .json"
            ) from exc
        return yaml.safe_load(text)


def _parse_universe(data: dict[str, Any]) -> UniverseConfig:
    if not isinstance(data, dict):
        raise StrategyInstanceError("universe must be a mapping")

    mode = data.get("mode", "discover")
    if mode not in ("discover", "static", "hybrid"):
        raise StrategyInstanceError(f"Invalid universe.mode: {mode!r}")

    drop_raw = data.get("drop_policy") or {}
    if not isinstance(drop_raw, dict):
        raise StrategyInstanceError("universe.drop_policy must be a mapping")
    drop = DropPolicy(
        closed=_as_drop(drop_raw.get("closed", "drop"), "drop_policy.closed"),
        fail_filters=_as_drop(
            drop_raw.get("fail_filters", "drop_if_flat"), "drop_policy.fail_filters"
        ),
        not_in_top_n=_as_drop(
            drop_raw.get("not_in_top_n", "drop_if_flat"), "drop_policy.not_in_top_n"
        ),
    )

    discover_raw = data.get("discover") or {}
    if not isinstance(discover_raw, dict):
        raise StrategyInstanceError("universe.discover must be a mapping")
    discover = _parse_discover(discover_raw)

    static = data.get("static_tickers") or []
    if not isinstance(static, list):
        raise StrategyInstanceError("universe.static_tickers must be a list")
    tickers = tuple(str(t).strip() for t in static if str(t).strip())

    refresh = data.get("refresh_seconds")
    if refresh is not None:
        refresh = int(refresh)

    return UniverseConfig(
        mode=mode,  # type: ignore[arg-type]
        refresh_seconds=refresh,
        max_watchlist=int(data.get("max_watchlist", discover.top_n or 10)),
        static_tickers=tickers,
        drop_policy=drop,
        discover=discover,
    )


def _parse_discover(data: dict[str, Any]) -> DiscoverConfig:
    rank_by = data.get("rank_by", "volume")
    if rank_by not in ("volume", "fee_adjusted_roi", "screener"):
        raise StrategyInstanceError(f"Invalid discover.rank_by: {rank_by!r}")

    preset = data.get("preset")
    if preset is not None:
        preset = str(preset).strip()
        if preset.lower() == "none":
            preset = "none"

    return DiscoverConfig(
        category=str(data.get("category") or "Trending"),
        preset=preset,
        top_n=int(data.get("top_n", 10)),
        min_volume_24h=int(data.get("min_volume_24h", 0)),
        min_yes_ask=_opt_int(data.get("min_yes_ask")),
        max_yes_ask=_opt_int(data.get("max_yes_ask")),
        max_spread=_opt_int(data.get("max_spread")),
        activity_hours=_opt_float(data.get("activity_hours")),
        max_minutes_to_close=_opt_float(data.get("max_minutes_to_close")),
        rank_by=rank_by,  # type: ignore[arg-type]
        min_fee_adjusted_roi_pct=_opt_float(data.get("min_fee_adjusted_roi_pct")),
        full_scan=bool(data.get("full_scan", False)),
        tradeable_only=bool(data.get("tradeable_only", True)),
        live_only=bool(data.get("live_only", True)),
        tag=_opt_str(data.get("tag")),
        sport=_opt_str(data.get("sport")),
        competition=_opt_str(data.get("competition")),
        scope=_opt_str(data.get("scope")),
        series_ticker=_opt_str(data.get("series_ticker")),
    )


def _validate_strategy_params(strategy: str, params: dict[str, Any]) -> None:
    allowed = STRATEGY_PARAM_KEYS.get(strategy, frozenset())
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise StrategyInstanceError(
            f"Unknown strategy_params for {strategy}: {', '.join(unknown)}"
        )

    for key in (
        "gu_entry_mode", "gu_exit_mode", "hp_entry_mode", "hp_exit_mode",
        "mr_entry_mode", "mr_exit_mode",
    ):
        if key in params and params[key] not in PRICE_MODES:
            raise StrategyInstanceError(
                f"Invalid {key}={params[key]!r}; choose from {sorted(PRICE_MODES)}"
            )

    if "hedge_mode" in params and params["hedge_mode"] not in (
        "full_green", "stake_back", "partial",
    ):
        raise StrategyInstanceError(
            f"Invalid hedge_mode={params['hedge_mode']!r}"
        )
    if "gu_hedge_style" in params and params["gu_hedge_style"] not in (
        "trigger", "resting",
    ):
        raise StrategyInstanceError(
            f"Invalid gu_hedge_style={params['gu_hedge_style']!r}"
        )
    if "hp_post_fill" in params and params["hp_post_fill"] not in (
        "hold", "resting_take_profit", "resting_stop", "tp_and_stop",
    ):
        raise StrategyInstanceError(
            f"Invalid hp_post_fill={params['hp_post_fill']!r}"
        )
    if "hp_tp_style" in params and params["hp_tp_style"] not in ("fixed", "at_ask"):
        raise StrategyInstanceError(
            f"Invalid hp_tp_style={params['hp_tp_style']!r}"
        )
    if "model_probs" in params and params["model_probs"] is not None:
        if not isinstance(params["model_probs"], dict):
            raise StrategyInstanceError("model_probs must be a mapping of ticker→prob")
    if "comp_pairs" in params and params["comp_pairs"] is not None:
        if not isinstance(params["comp_pairs"], list):
            raise StrategyInstanceError(
                "comp_pairs must be a list of [ticker_a, ticker_b] or 'A:B' strings"
            )


def _parse_dataclass(cls: type, data: dict[str, Any], label: str) -> Any:
    if not isinstance(data, dict):
        raise StrategyInstanceError(f"{label} must be a mapping")
    allowed = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise StrategyInstanceError(f"Unknown {label} fields: {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = data[f.name]
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise StrategyInstanceError(f"Invalid {label}: {exc}") from exc


def _as_drop(value: Any, label: str) -> DropAction:
    if value not in ("drop", "drop_if_flat", "keep"):
        raise StrategyInstanceError(f"Invalid {label}: {value!r}")
    return value  # type: ignore[return-value]


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
