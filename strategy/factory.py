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
from strategy.green_up_strategy import GreenUpStrategy, HedgeMode
from strategy.execution_price import EntryPriceMode
from strategy.high_prob_strategy import HighProbStrategy, PostFillMode
from strategy.kelly_strategy import KellyStrategy

VALID_STRATEGIES = ("kelly", "green_up", "arb", "high_prob")


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
    hedge_mode: str | None = None,
    stop_loss: float | None = None,
    comp_pairs: list[tuple[str, str]] | None = None,
    hp_min_yes_ask: int | None = None,
    hp_max_yes_ask: int | None = None,
    hp_entry_mode: str | None = None,
    hp_post_fill: str | None = None,
    hp_stake_cents: int | None = None,
    hp_take_profit_pct: float | None = None,
    gu_entry_mode: str | None = None,
    gu_exit_mode: str | None = None,
    hp_exit_mode: str | None = None,
) -> BaseStrategy:
    """
    Build a strategy instance by name.

    Args:
        name:           One of kelly, green_up, arb, high_prob.
        tickers:        Markets to watch (green_up registers each via add_watch_ticker).
        model_probs:    Kelly only — ticker -> P(YES wins).
        entry_max:      Green-up max YES ask for entry (cents).
        hedge_trigger:  Green-up YES bid to trigger hedge (cents).
        hedge_mode:     full_green | stake_back | partial.
        stop_loss:      Green-up stop fraction below entry.
        comp_pairs:     Arb only — complementary ticker pairs.
        hp_*:           High-probability strategy tunables.
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
        strat = HighProbStrategy(
            min_yes_ask=hp_min_yes_ask if hp_min_yes_ask is not None else _env_int(
                "KALSHI_HP_MIN_YES_ASK", config.HP_MIN_YES_ASK
            ),
            max_yes_ask=hp_max_yes_ask if hp_max_yes_ask is not None else _env_int(
                "KALSHI_HP_MAX_YES_ASK", config.HP_MAX_YES_ASK
            ),
            min_roi_pct=_env_float("KALSHI_HP_MIN_ROI_PCT", config.HP_MIN_ROI_PCT),
            max_spread_cents=_env_int("KALSHI_HP_MAX_SPREAD", config.HP_MAX_SPREAD_CENTS),
            stake_cents=hp_stake_cents if hp_stake_cents is not None else _env_int(
                "KALSHI_HP_STAKE_CENTS", config.HP_STAKE_CENTS
            ),
            entry_price_mode=entry_map.get(entry_key, EntryPriceMode.PASSIVE),
            exit_price_mode=entry_map.get(exit_key, EntryPriceMode.PASSIVE),
            limit_offset_cents=_env_int("KALSHI_HP_LIMIT_OFFSET", config.HP_LIMIT_OFFSET),
            post_fill_mode=post_map.get(post_key, PostFillMode.HOLD_TO_SETTLEMENT),
            take_profit_offset_cents=_env_int(
                "KALSHI_HP_TAKE_PROFIT_OFFSET", config.HP_TAKE_PROFIT_OFFSET
            ),
            take_profit_pct=(
                hp_take_profit_pct
                if hp_take_profit_pct is not None
                else getattr(config, "HP_TAKE_PROFIT_PCT", None)
            ),
            stop_loss_pct=_env_float("KALSHI_HP_STOP_LOSS", config.HP_STOP_LOSS_PCT),
            require_model_edge=os.getenv("KALSHI_HP_REQUIRE_MODEL_EDGE", "").lower()
            in ("1", "true", "yes"),
        )
        if model_probs:
            for ticker, prob in model_probs.items():
                strat.set_model_probability(ticker, prob)
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
        strat = GreenUpStrategy(
            entry_max_price=entry_max if entry_max is not None else int(
                os.getenv("KALSHI_GREEN_UP_ENTRY_MAX", "25")
            ),
            hedge_trigger_price=hedge_trigger if hedge_trigger is not None else int(
                os.getenv("KALSHI_GREEN_UP_HEDGE_TRIGGER", "68")
            ),
            hedge_mode=mode_map.get(mode_key, HedgeMode.FULL_GREEN),
            stop_loss_threshold=stop_loss if stop_loss is not None else float(
                os.getenv("KALSHI_GREEN_UP_STOP_LOSS", "0.40")
            ),
            entry_price_mode=price_mode_map.get(entry_key, EntryPriceMode.PASSIVE),
            exit_price_mode=price_mode_map.get(exit_key, EntryPriceMode.PASSIVE),
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
