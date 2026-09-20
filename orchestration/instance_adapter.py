"""
orchestration/instance_adapter.py

Apply a StrategyInstance onto argparse Namespace, config module attributes,
and build_strategy kwargs — without loosening env-profile risk ceilings.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import config
from discovery.discovery_presets import apply_preset, preset_for_strategy
from discovery.ticker_selector import TickerCriteria, resolve_discover_category
from logging_.structured_logger import logger
from orchestration.strategy_instance import StrategyInstance
from strategy.factory import _parse_comp_pairs, _parse_model_probs


def apply_instance_runtime(
    instance: StrategyInstance,
    args: argparse.Namespace,
) -> None:
    """
    Patch config + os.environ for persistence/risk and merge instance fields
    into ``args`` (CLI-explicit values win).
    """
    _apply_persistence(instance)
    _apply_risk_sleeve(instance)
    _merge_args_from_instance(instance, args)


def build_strategy_kwargs(instance: StrategyInstance) -> dict[str, Any]:
    """Map strategy_params → build_strategy kwargs (plus parsed model/comp)."""
    params = dict(instance.strategy_params)
    kwargs: dict[str, Any] = {}

    model_probs = params.pop("model_probs", None)
    if isinstance(model_probs, dict):
        kwargs["model_probs"] = {
            str(k): float(v) for k, v in model_probs.items()
        }
    elif isinstance(model_probs, str):
        kwargs["model_probs"] = _parse_model_probs(model_probs)

    comp_pairs = params.pop("comp_pairs", None)
    if comp_pairs:
        kwargs["comp_pairs"] = _normalize_comp_pairs(comp_pairs)

    if "no_entry_max" in params:
        kwargs["gu_no_entry_max"] = bool(params.pop("no_entry_max"))

    if "require_model_edge" in params:
        # factory reads env; set env so build_strategy honors it
        flag = bool(params.pop("require_model_edge"))
        os.environ["KALSHI_HP_REQUIRE_MODEL_EDGE"] = "true" if flag else "false"

    # Remaining keys already match build_strategy parameter names
    for key, value in params.items():
        kwargs[key] = value

    return kwargs


def criteria_from_instance(instance: StrategyInstance) -> TickerCriteria | None:
    """Build discovery criteria from instance.universe (None if static-only)."""
    uni = instance.universe
    if uni.mode == "static":
        return None

    d = uni.discover
    criteria = TickerCriteria(
        category=resolve_discover_category(d.category),
        top_n=d.top_n,
        min_volume_24h=d.min_volume_24h,
        min_yes_ask=d.min_yes_ask,
        max_yes_ask=d.max_yes_ask,
        max_spread=d.max_spread,
        min_fee_adjusted_roi_pct=d.min_fee_adjusted_roi_pct,
        rank_by=d.rank_by,
        screener_strategy=instance.strategy,
        activity_hours=d.activity_hours,
        full_scan=d.full_scan or (d.activity_hours is not None) or _has_drilldown(d),
        tradeable_only=d.tradeable_only,
        live_only=d.live_only and getattr(config, "LIVE_TRADING_ONLY", True),
        max_minutes_to_close=d.max_minutes_to_close,
        tag=d.tag,
        sport=d.sport,
        competition=d.competition,
        scope=d.scope,
        series_ticker=d.series_ticker,
    )

    preset_name = d.preset
    if preset_name is None:
        preset_name = preset_for_strategy(instance.strategy)
    elif preset_name == "none":
        preset_name = None

    if preset_name:
        # Explicit discover fields in the instance override preset defaults
        skip = _explicit_discover_fields(d)
        criteria = apply_preset(criteria, preset_name, skip_fields=skip)

    return criteria


def initial_tickers_hint(instance: StrategyInstance) -> list[str]:
    """Static seed tickers (may be empty when mode=discover)."""
    return list(instance.universe.static_tickers)


def _apply_persistence(instance: StrategyInstance) -> None:
    p = instance.persistence
    if p.db_path:
        path = str(p.db_path)
        os.environ["KALSHI_DB_PATH"] = path
        if config.ENV == "demo":
            os.environ["KALSHI_DEMO_DB_PATH"] = path
        elif config.ENV == "production":
            os.environ["KALSHI_PROD_DB_PATH"] = path
        config.DB_PATH = path

    if p.log_file:
        path = str(p.log_file)
        os.environ["KALSHI_LOG_FILE"] = path
        if config.ENV == "demo":
            os.environ["KALSHI_DEMO_LOG_FILE"] = path
        elif config.ENV == "production":
            os.environ["KALSHI_PROD_LOG_FILE"] = path
        config.LOG_FILE = path
        _rebind_logger_file(path)


def _apply_risk_sleeve(instance: StrategyInstance) -> None:
    r = instance.risk
    if r.max_position_cents is not None:
        capped = min(int(r.max_position_cents), int(config.MAX_POSITION_CENTS))
        if capped < int(r.max_position_cents):
            logger.warning(
                "Instance max_position_cents capped by env profile",
                instance_id=instance.id,
                requested=r.max_position_cents,
                ceiling=config.MAX_POSITION_CENTS,
            )
        config.MAX_POSITION_CENTS = capped
        os.environ["KALSHI_MAX_POSITION_CENTS"] = str(capped)

    if r.max_concurrent_positions is not None:
        # 0 = unlimited in CLI; allow instance to set it
        val = int(r.max_concurrent_positions)
        config.MAX_CONCURRENT_POSITIONS = val
        os.environ["KALSHI_MAX_CONCURRENT_POSITIONS"] = str(val)

    if r.daily_loss_limit_cents is not None:
        capped = min(int(r.daily_loss_limit_cents), int(config.DAILY_LOSS_LIMIT_CENTS))
        if capped < int(r.daily_loss_limit_cents):
            logger.warning(
                "Instance daily_loss_limit_cents capped by env profile",
                instance_id=instance.id,
                requested=r.daily_loss_limit_cents,
                ceiling=config.DAILY_LOSS_LIMIT_CENTS,
            )
        config.DAILY_LOSS_LIMIT_CENTS = capped
        os.environ["KALSHI_DAILY_LOSS_LIMIT_CENTS"] = str(capped)

    if r.max_drawdown_pct is not None:
        capped = min(float(r.max_drawdown_pct), float(config.MAX_DRAWDOWN_PCT))
        if capped < float(r.max_drawdown_pct):
            logger.warning(
                "Instance max_drawdown_pct capped by env profile",
                instance_id=instance.id,
                requested=r.max_drawdown_pct,
                ceiling=config.MAX_DRAWDOWN_PCT,
            )
        config.MAX_DRAWDOWN_PCT = capped
        os.environ["KALSHI_MAX_DRAWDOWN_PCT"] = str(capped)

    if r.max_sector_concentration is not None:
        config.MAX_SECTOR_CONCENTRATION = float(r.max_sector_concentration)
        os.environ["KALSHI_MAX_SECTOR_CONCENTRATION"] = str(
            r.max_sector_concentration
        )


def _merge_args_from_instance(
    instance: StrategyInstance,
    args: argparse.Namespace,
) -> None:
    """Fill unset CLI fields from the instance (CLI wins when set)."""
    # Instance defines the worker strategy type
    args.strategy = instance.strategy

    rt = instance.runtime
    if args.monitor_interval is None and rt.monitor_interval is not None:
        args.monitor_interval = rt.monitor_interval
    if not args.quiet and rt.quiet:
        args.quiet = True
    if not args.auto_take_profit and rt.auto_take_profit:
        args.auto_take_profit = True
    if getattr(args, "max_runtime_minutes", None) in (None, 0) and rt.max_runtime_minutes:
        args.max_runtime_minutes = rt.max_runtime_minutes

    life = instance.lifecycle
    if life.on_flat == "exit" and not args.exit_when_flat:
        args.exit_when_flat = True
    if life.on_all_settled == "exit" and not args.exit_on_settle:
        args.exit_on_settle = True

    risk = instance.risk
    if args.max_concurrent_positions is None and risk.max_concurrent_positions is not None:
        args.max_concurrent_positions = risk.max_concurrent_positions

    uni = instance.universe
    if uni.mode in ("discover", "hybrid"):
        if not args.discover:
            args.discover = True
        d = uni.discover
        _set_if_none(args, "discover_category", d.category)
        _set_if_none(args, "discover_top", d.top_n)
        _set_if_none(args, "discover_min_volume", d.min_volume_24h or None)
        _set_if_none(args, "discover_min_yes_ask", d.min_yes_ask)
        _set_if_none(args, "discover_max_yes_ask", d.max_yes_ask)
        _set_if_none(args, "discover_max_spread", d.max_spread)
        _set_if_none(args, "discover_activity_hours", d.activity_hours)
        _set_if_none(args, "discover_max_minutes_to_close", d.max_minutes_to_close)
        _set_if_none(args, "discover_rank_by", d.rank_by)
        _set_if_none(args, "discover_min_fee_roi", d.min_fee_adjusted_roi_pct)
        _set_if_none(args, "discover_tag", d.tag)
        _set_if_none(args, "discover_sport", d.sport)
        _set_if_none(args, "discover_competition", d.competition)
        _set_if_none(args, "discover_scope", d.scope)
        _set_if_none(args, "discover_series", d.series_ticker)
        if d.preset is not None and args.discover_preset is None:
            args.discover_preset = d.preset
        if d.full_scan:
            args.discover_full_scan = True
        if not d.tradeable_only:
            args.discover_no_tradeable_filter = True
        if not d.live_only:
            args.no_live_only = True
        else:
            # Keep runtime entry gates aligned with instance discovery windows.
            # Kalshi Sports close_time is often settlement (days), not the event.
            if d.max_minutes_to_close is not None:
                config.LIVE_MAX_MINUTES_TO_CLOSE = float(d.max_minutes_to_close)
                os.environ["KALSHI_LIVE_MAX_MINUTES_TO_CLOSE"] = str(
                    d.max_minutes_to_close
                )
            if d.activity_hours is not None:
                mins = float(d.activity_hours) * 60.0
                config.LIVE_MAX_MINUTES_SINCE_UPDATE = mins
                os.environ["KALSHI_LIVE_MAX_MINUTES_SINCE_UPDATE"] = str(mins)

    if uni.mode in ("static", "hybrid") and uni.static_tickers and not args.tickers:
        args.tickers = ",".join(uni.static_tickers)

    # Strategy params → args (only if CLI left them None / default)
    p = instance.strategy_params
    _map_param(args, "entry_max", p.get("entry_max"))
    _map_param(args, "hedge_trigger", p.get("hedge_trigger"))
    _map_param(args, "hedge_offset", p.get("hedge_offset"))
    _map_param(args, "hedge_mode", p.get("hedge_mode"))
    _map_param(args, "stop_loss", p.get("stop_loss"))
    if p.get("no_entry_max") and not args.gu_no_entry_max:
        args.gu_no_entry_max = True
    for key in (
        "gu_entry_mode", "gu_exit_mode", "gu_limit_offset", "gu_max_spread",
        "gu_hedge_style", "gu_max_cycles",
        "hp_min_yes_ask", "hp_max_yes_ask", "hp_entry_mode", "hp_exit_mode",
        "hp_post_fill", "hp_stake_cents", "hp_take_profit_pct",
        "hp_take_profit_offset", "hp_stop_loss", "hp_stop_loss_cents",
        "hp_max_spread", "hp_tp_style", "hp_max_cycles",
        "mr_lookback", "mr_min_samples", "mr_entry_deviation",
        "mr_take_profit_offset", "mr_stop_loss_cents", "mr_min_volatility",
        "mr_entry_max", "mr_entry_min", "mr_short_min_yes_ask",
        "mr_short_max_yes_ask", "mr_stake_cents", "mr_max_spread",
        "mr_trade_direction", "mr_exit_target", "mr_entry_mode", "mr_exit_mode",
        "mr_post_fill", "mr_max_cycles", "mr_limit_offset",
    ):
        if key in p:
            _map_param(args, key, p[key])

    if "model_probs" in p and not args.model_prob:
        mp = p["model_probs"]
        if isinstance(mp, dict):
            args.model_prob = [f"{k}:{v}" for k, v in mp.items()]
    if "comp_pairs" in p and not args.comp_pairs:
        pairs = _normalize_comp_pairs(p["comp_pairs"])
        args.comp_pairs = [f"{a}:{b}" for a, b in pairs]

    args._instance_id = instance.id
    args._instance_loaded = True


def _rebind_logger_file(path: str) -> None:
    """Point the singleton logger's file handler at a new path."""
    import logging
    from logging_.structured_logger import _JsonFormatter

    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logger._logger
    # Remove existing FileHandlers
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
            handler.close()
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(_JsonFormatter())
    root.addHandler(fh)


def _normalize_comp_pairs(raw: Any) -> list[tuple[str, str]]:
    if isinstance(raw, str):
        return _parse_comp_pairs(raw)
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            parts = item.split(":")
            if len(parts) != 2:
                raise ValueError(f"Invalid comp pair {item!r}")
            pairs.append((parts[0].strip(), parts[1].strip()))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]).strip(), str(item[1]).strip()))
        else:
            raise ValueError(f"Invalid comp pair {item!r}")
    return pairs


def _has_drilldown(d: Any) -> bool:
    return any([d.tag, d.sport, d.competition, d.scope, d.series_ticker])


def _explicit_discover_fields(d: Any) -> frozenset[str]:
    """Fields set on DiscoverConfig that should not be overwritten by presets."""
    # Treat non-default / non-None overrides as explicit. For ints with defaults
    # (top_n, min_volume), always treat as explicit when present in YAML — the
    # parser always fills them, so we skip only None-able overrides that were set.
    skip: set[str] = {
        "top_n",
        "min_volume_24h",
        "rank_by",
        "full_scan",
        "tradeable_only",
        "live_only",
    }
    for name in (
        "min_yes_ask", "max_yes_ask", "max_spread", "activity_hours",
        "max_minutes_to_close", "min_fee_adjusted_roi_pct",
        "tag", "sport", "competition", "scope", "series_ticker",
    ):
        if getattr(d, name, None) is not None:
            skip.add(name)
    return frozenset(skip)


def _set_if_none(args: argparse.Namespace, name: str, value: Any) -> None:
    if getattr(args, name, None) is None and value is not None:
        setattr(args, name, value)


def _map_param(args: argparse.Namespace, name: str, value: Any) -> None:
    if value is None:
        return
    if getattr(args, name, None) is None:
        setattr(args, name, value)
