"""
strategy/factory.py

Instantiate a strategy by name (shared by main.py and tools/replay.py).
"""

from __future__ import annotations

import os
from typing import Any

import config
from strategy.arbitrage_strategy import ArbitrageStrategy
from strategy.base_strategy import BaseStrategy
from strategy.green_up_strategy import (
    GreenUpStrategy,
    HedgeMode,
    parse_hedge_style,
    parse_stop_loss_cents,
    resolve_entry_max_price,
)
from strategy.price_targets import parse_hedge_offset_cents
from strategy.execution_price import EntryPriceMode
from strategy.high_prob_strategy import (
    HighProbStrategy,
    PostFillMode,
    parse_hp_stop_loss_cents,
    parse_take_profit_style,
)
from strategy.kelly_strategy import KellyStrategy
from strategy.mean_reversion_strategy import (
    MeanReversionStrategy,
    PostFillMode as MRPostFillMode,
    parse_exit_target,
    parse_mr_stop_loss_cents,
    parse_trade_direction,
)

VALID_STRATEGIES = ("kelly", "green_up", "arb", "high_prob", "mean_reversion")


def _parse_model_probs(raw: str | None) -> dict[str, float]:
    """Parse 'TICKER:0.62,TICKER2:0.55' into a probability map."""
    if not raw:
        return {}
    result: dict[str, float] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                f"Invalid model probability {item!r} — expected TICKER:PROB"
            )
        ticker, prob_s = item.split(":", 1)
        result[ticker.strip()] = float(prob_s.strip())
    return result


def _parse_comp_pairs(raw: str | None) -> list[tuple[str, str]]:
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid arb pair {item!r} — expected TICKER_A:TICKER_B"
            )
        pairs.append((parts[0].strip(), parts[1].strip()))
    return pairs


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw) if raw is not None else default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw) if raw is not None else default


def build_strategy(
    name: str,
    tickers: list[str],
    *,
    model_probs: dict[str, float] | None = None,
    entry_max: int | None = None,
    hedge_trigger: int | None = None,
    hedge_offset: int | None = None,
    hedge_mode: str | None = None,
    stop_loss: int | float | None = None,
    comp_pairs: list[tuple[str, str]] | None = None,
    hp_min_yes_ask: int | None = None,
    hp_max_yes_ask: int | None = None,
    hp_entry_mode: str | None = None,
    hp_post_fill: str | None = None,
    hp_stake_cents: int | None = None,
    hp_take_profit_pct: float | None = None,
    hp_take_profit_offset: int | None = None,
    hp_stop_loss: float | None = None,
    hp_stop_loss_cents: int | None = None,
    hp_max_spread: int | None = None,
    hp_tp_style: str | None = None,
    hp_max_cycles: int | None = None,
    gu_entry_mode: str | None = None,
    gu_exit_mode: str | None = None,
    gu_limit_offset: int | None = None,
    gu_max_cycles: int | None = None,
    gu_max_spread: int | None = None,
    gu_no_entry_max: bool = False,
    gu_hedge_style: str | None = None,
    hp_exit_mode: str | None = None,
    mr_lookback: int | None = None,
    mr_entry_deviation: int | None = None,
    mr_take_profit_offset: int | None = None,
    mr_stop_loss_cents: int | None = None,
    mr_min_volatility: float | None = None,
    mr_entry_max: int | None = None,
    mr_entry_min: int | None = None,
    mr_short_min_yes_ask: int | None = None,
    mr_short_max_yes_ask: int | None = None,
    mr_stake_cents: int | None = None,
    mr_max_spread: int | None = None,
    mr_trade_direction: str | None = None,
    mr_exit_target: str | None = None,
    mr_entry_mode: str | None = None,
    mr_exit_mode: str | None = None,
    mr_post_fill: str | None = None,
    mr_max_cycles: int | None = None,
    mr_limit_offset: int | None = None,
    mr_min_samples: int | None = None,
) -> BaseStrategy:
    """
    Build a strategy instance by name.

    Args:
        name:           One of kelly, green_up, arb, high_prob.
        tickers:        Markets to watch (green_up registers each via add_watch_ticker).
        model_probs:    Kelly only — ticker -> P(YES wins).
        entry_max:      Green-up max YES ask for entry (cents).
        hedge_trigger:  Green-up YES bid to trigger hedge (absolute mode, cents).
        hedge_offset:   Green-up hedge when YES bid >= entry + N cents (relative mode).
        hedge_mode:     full_green | stake_back | partial.
        stop_loss:      Green-up stop in cents per contract (YES bid drop from entry).
        comp_pairs:     Arb only — complementary ticker pairs.
        hp_*:           High-probability strategy tunables.
        mr_*:           Mean-reversion strategy tunables.
    """
    key = name.strip().lower()
    if key not in VALID_STRATEGIES:
        raise ValueError(
            f"Unknown strategy {name!r}. Choose: {', '.join(VALID_STRATEGIES)}"
        )

    if key == "kelly":
        return KellyStrategy(model_probabilities=model_probs or {})

    price_mode_map = {
        "passive":      EntryPriceMode.PASSIVE,
        "cross_spread": EntryPriceMode.CROSS_SPREAD,
        "market":       EntryPriceMode.MARKET,
        "limit_at_ask": EntryPriceMode.LIMIT_AT_ASK,
        "limit_at_bid": EntryPriceMode.LIMIT_AT_BID,
        "limit_at_mid": EntryPriceMode.LIMIT_AT_MID,
        "limit_offset": EntryPriceMode.LIMIT_OFFSET,
    }

    if key == "high_prob":
        entry_map = price_mode_map
        post_map = {
            "hold":                PostFillMode.HOLD_TO_SETTLEMENT,
            "resting_take_profit": PostFillMode.RESTING_TAKE_PROFIT,
            "resting_stop":        PostFillMode.RESTING_STOP_LOSS,
            "tp_and_stop":         PostFillMode.TAKE_PROFIT_AND_STOP,
        }
        entry_key = (
            hp_entry_mode or os.getenv("KALSHI_HP_ENTRY_MODE", "passive")
        ).lower()
        exit_key = (
            hp_exit_mode or os.getenv("KALSHI_HP_EXIT_MODE", "passive")
        ).lower()
        post_key = (
            hp_post_fill or os.getenv("KALSHI_HP_POST_FILL", "hold")
        ).lower()
        stop_loss_cents_raw = (
            hp_stop_loss_cents
            if hp_stop_loss_cents is not None
            else os.getenv("KALSHI_HP_STOP_LOSS_CENTS")
        )
        tp_style_key = (
            hp_tp_style or os.getenv("KALSHI_HP_TP_STYLE", "fixed")
        ).lower()
        strat = HighProbStrategy(
            min_yes_ask=hp_min_yes_ask if hp_min_yes_ask is not None else _env_int(
                "KALSHI_HP_MIN_YES_ASK", config.HP_MIN_YES_ASK
            ),
            max_yes_ask=hp_max_yes_ask if hp_max_yes_ask is not None else _env_int(
                "KALSHI_HP_MAX_YES_ASK", config.HP_MAX_YES_ASK
            ),
            min_roi_pct=_env_float("KALSHI_HP_MIN_ROI_PCT", config.HP_MIN_ROI_PCT),
            max_spread_cents=(
                hp_max_spread
                if hp_max_spread is not None
                else _env_int("KALSHI_HP_MAX_SPREAD", config.HP_MAX_SPREAD_CENTS)
            ),
            stake_cents=hp_stake_cents if hp_stake_cents is not None else _env_int(
                "KALSHI_HP_STAKE_CENTS", config.HP_STAKE_CENTS
            ),
            entry_price_mode=entry_map.get(entry_key, EntryPriceMode.PASSIVE),
            exit_price_mode=entry_map.get(exit_key, EntryPriceMode.PASSIVE),
            limit_offset_cents=_env_int("KALSHI_HP_LIMIT_OFFSET", config.HP_LIMIT_OFFSET),
            post_fill_mode=post_map.get(post_key, PostFillMode.HOLD_TO_SETTLEMENT),
            take_profit_offset_cents=(
                hp_take_profit_offset
                if hp_take_profit_offset is not None
                else _env_int(
                    "KALSHI_HP_TAKE_PROFIT_OFFSET", config.HP_TAKE_PROFIT_OFFSET
                )
            ),
            take_profit_pct=(
                hp_take_profit_pct
                if hp_take_profit_pct is not None
                else getattr(config, "HP_TAKE_PROFIT_PCT", None)
            ),
            stop_loss_pct=(
                hp_stop_loss
                if hp_stop_loss is not None
                else _env_float("KALSHI_HP_STOP_LOSS", config.HP_STOP_LOSS_PCT)
            ),
            stop_loss_cents=parse_hp_stop_loss_cents(stop_loss_cents_raw),
            tp_style=parse_take_profit_style(tp_style_key),
            max_cycles_per_ticker=(
                hp_max_cycles
                if hp_max_cycles is not None
                else _env_int("KALSHI_HP_MAX_CYCLES_PER_TICKER", 0)
            ),
            require_model_edge=os.getenv("KALSHI_HP_REQUIRE_MODEL_EDGE", "").lower()
            in ("1", "true", "yes"),
        )
        if model_probs:
            for ticker, prob in model_probs.items():
                strat.set_model_probability(ticker, prob)
        for ticker in tickers:
            strat.add_watch_ticker(ticker)
        return strat

    if key == "mean_reversion":
        entry_key = (
            mr_entry_mode or os.getenv("KALSHI_MR_ENTRY_MODE", "passive")
        ).lower()
        exit_key = (
            mr_exit_mode or os.getenv("KALSHI_MR_EXIT_MODE", "passive")
        ).lower()
        post_key = (
            mr_post_fill or os.getenv("KALSHI_MR_POST_FILL", config.MR_POST_FILL)
        ).lower()
        post_map = {
            "hold":                MRPostFillMode.HOLD_TO_SETTLEMENT,
            "resting_take_profit": MRPostFillMode.RESTING_TAKE_PROFIT,
            "resting_stop":        MRPostFillMode.RESTING_STOP_LOSS,
            "tp_and_stop":         MRPostFillMode.TAKE_PROFIT_AND_STOP,
        }
        direction_key = (
            mr_trade_direction or os.getenv("KALSHI_MR_TRADE_DIRECTION", config.MR_TRADE_DIRECTION)
        ).lower()
        exit_target_key = (
            mr_exit_target or os.getenv("KALSHI_MR_EXIT_TARGET", config.MR_EXIT_TARGET)
        ).lower()
        stop_loss_cents_raw = (
            mr_stop_loss_cents
            if mr_stop_loss_cents is not None
            else os.getenv("KALSHI_MR_STOP_LOSS_CENTS")
        )
        strat = MeanReversionStrategy(
            lookback_ticks=(
                mr_lookback
                if mr_lookback is not None
                else _env_int("KALSHI_MR_LOOKBACK_TICKS", config.MR_LOOKBACK_TICKS)
            ),
            min_samples=(
                mr_min_samples
                if mr_min_samples is not None
                else _env_int("KALSHI_MR_MIN_SAMPLES", config.MR_MIN_SAMPLES)
            ),
            entry_deviation_cents=(
                mr_entry_deviation
                if mr_entry_deviation is not None
                else _env_int("KALSHI_MR_ENTRY_DEVIATION", config.MR_ENTRY_DEVIATION_CENTS)
            ),
            take_profit_offset_cents=(
                mr_take_profit_offset
                if mr_take_profit_offset is not None
                else _env_int("KALSHI_MR_TAKE_PROFIT_OFFSET", config.MR_TAKE_PROFIT_OFFSET)
            ),
            exit_target=parse_exit_target(exit_target_key),
            stop_loss_cents=parse_mr_stop_loss_cents(
                stop_loss_cents_raw if stop_loss_cents_raw is not None else config.MR_STOP_LOSS_CENTS
            ),
            min_volatility_cents=(
                mr_min_volatility
                if mr_min_volatility is not None
                else _env_float("KALSHI_MR_MIN_VOLATILITY", config.MR_MIN_VOLATILITY_CENTS)
            ),
            entry_max_price=(
                mr_entry_max
                if mr_entry_max is not None
                else _env_int("KALSHI_MR_ENTRY_MAX", config.MR_ENTRY_MAX_PRICE)
            ),
            entry_min_price=(
                mr_entry_min
                if mr_entry_min is not None
                else _env_int("KALSHI_MR_ENTRY_MIN", config.MR_ENTRY_MIN_PRICE)
            ),
            short_min_yes_ask=(
                mr_short_min_yes_ask
                if mr_short_min_yes_ask is not None
                else _env_int("KALSHI_MR_SHORT_MIN_YES_ASK", config.MR_SHORT_MIN_YES_ASK)
            ),
            short_max_yes_ask=(
                mr_short_max_yes_ask
                if mr_short_max_yes_ask is not None
                else _env_int("KALSHI_MR_SHORT_MAX_YES_ASK", config.MR_SHORT_MAX_YES_ASK)
            ),
            max_spread_cents=(
                mr_max_spread
                if mr_max_spread is not None
                else _env_int("KALSHI_MR_MAX_SPREAD", config.MR_MAX_SPREAD_CENTS)
            ),
            stake_cents=(
                mr_stake_cents
                if mr_stake_cents is not None
                else _env_int("KALSHI_MR_STAKE_CENTS", config.MR_STAKE_CENTS)
            ),
            trade_direction=parse_trade_direction(direction_key),
            entry_price_mode=price_mode_map.get(entry_key, EntryPriceMode.PASSIVE),
            exit_price_mode=price_mode_map.get(exit_key, EntryPriceMode.PASSIVE),
            limit_offset_cents=(
                mr_limit_offset
                if mr_limit_offset is not None
                else _env_int("KALSHI_MR_LIMIT_OFFSET", config.MR_LIMIT_OFFSET)
            ),
            post_fill_mode=post_map.get(post_key, MRPostFillMode.TAKE_PROFIT_AND_STOP),
            max_cycles_per_ticker=(
                mr_max_cycles
                if mr_max_cycles is not None
                else _env_int("KALSHI_MR_MAX_CYCLES_PER_TICKER", 0)
            ),
        )
        for ticker in tickers:
            strat.add_watch_ticker(ticker)
        return strat

    if key == "green_up":
        mode_map = {
            "full_green": HedgeMode.FULL_GREEN,
            "stake_back": HedgeMode.STAKE_BACK,
            "partial":    HedgeMode.PARTIAL,
        }
        mode_key = (hedge_mode or os.getenv("KALSHI_GREEN_UP_HEDGE_MODE", "full_green")).lower()
        entry_key = (
            gu_entry_mode or os.getenv("KALSHI_GREEN_UP_ENTRY_MODE", "passive")
        ).lower()
        exit_key = (
            gu_exit_mode or os.getenv("KALSHI_GREEN_UP_EXIT_MODE", "passive")
        ).lower()
        limit_off = (
            gu_limit_offset
            if gu_limit_offset is not None
            else _env_int(
                "KALSHI_GREEN_UP_LIMIT_OFFSET",
                _env_int("KALSHI_HP_LIMIT_OFFSET", config.HP_LIMIT_OFFSET),
            )
        )
        max_cycles = (
            gu_max_cycles
            if gu_max_cycles is not None
            else _env_int("KALSHI_GREEN_UP_MAX_CYCLES_PER_TICKER", 0)
        )
        hedge_offset_raw = (
            hedge_offset
            if hedge_offset is not None
            else os.getenv("KALSHI_GREEN_UP_HEDGE_OFFSET")
        )
        resolved_entry_max = resolve_entry_max_price(
            entry_max,
            no_entry_max=gu_no_entry_max,
            env_value=os.getenv("KALSHI_GREEN_UP_ENTRY_MAX"),
        )
        hedge_style_key = (
            gu_hedge_style or os.getenv("KALSHI_GREEN_UP_HEDGE_STYLE", "trigger")
        ).lower()
        strat = GreenUpStrategy(
            entry_max_price=resolved_entry_max,
            hedge_trigger_price=hedge_trigger if hedge_trigger is not None else int(
                os.getenv("KALSHI_GREEN_UP_HEDGE_TRIGGER", "68")
            ),
            hedge_offset_cents=parse_hedge_offset_cents(hedge_offset_raw),
            hedge_mode=mode_map.get(mode_key, HedgeMode.FULL_GREEN),
            stop_loss_cents=parse_stop_loss_cents(
                stop_loss if stop_loss is not None else os.getenv("KALSHI_GREEN_UP_STOP_LOSS")
            ),
            max_spread_cents=(
                gu_max_spread
                if gu_max_spread is not None
                else _env_int(
                    "KALSHI_GREEN_UP_MAX_SPREAD",
                    config.GREEN_UP_MAX_SPREAD_CENTS,
                )
            ),
            hedge_style=parse_hedge_style(hedge_style_key),
            entry_price_mode=price_mode_map.get(entry_key, EntryPriceMode.PASSIVE),
            exit_price_mode=price_mode_map.get(exit_key, EntryPriceMode.PASSIVE),
            limit_offset_cents=limit_off,
            max_cycles_per_ticker=max_cycles,
        )
        for ticker in tickers:
            strat.add_watch_ticker(ticker)
        return strat

    if key == "arb":
        strat = ArbitrageStrategy()
        for a, b in comp_pairs or []:
            strat.register_complementary(a, b)
        return strat

    raise ValueError(f"Unhandled strategy {name!r}")


def build_strategy_from_env(tickers: list[str]) -> BaseStrategy:
    """Convenience wrapper: read strategy options from environment variables."""
    return build_strategy(
        os.getenv("KALSHI_STRATEGY", "kelly"),
        tickers,
        model_probs=_parse_model_probs(os.getenv("KALSHI_MODEL_PROB")),
        comp_pairs=_parse_comp_pairs(os.getenv("KALSHI_ARB_PAIRS")),
    )
