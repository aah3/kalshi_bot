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
from strategy.kelly_strategy import KellyStrategy

VALID_STRATEGIES = ("kelly", "green_up", "arb")


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
) -> BaseStrategy:
    """
    Build a strategy instance by name.

    Args:
        name:           One of kelly, green_up, arb.
        tickers:        Markets to watch (green_up registers each via add_watch_ticker).
        model_probs:    Kelly only — ticker -> P(YES wins).
        entry_max:      Green-up max YES ask for entry (cents).
        hedge_trigger:  Green-up YES bid to trigger hedge (cents).
        hedge_mode:     full_green | stake_back | partial.
        stop_loss:      Green-up stop fraction below entry.
        comp_pairs:     Arb only — complementary ticker pairs.
    """
    key = name.strip().lower()
    if key not in VALID_STRATEGIES:
        raise ValueError(
            f"Unknown strategy {name!r}. Choose: {', '.join(VALID_STRATEGIES)}"
        )

    if key == "kelly":
        strat = KellyStrategy(model_probabilities=model_probs or {})
        return strat

    if key == "green_up":
        mode_map = {
            "full_green": HedgeMode.FULL_GREEN,
            "stake_back": HedgeMode.STAKE_BACK,
            "partial":    HedgeMode.PARTIAL,
        }
        mode_key = (hedge_mode or os.getenv("KALSHI_GREEN_UP_HEDGE_MODE", "full_green")).lower()
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
        )
        for ticker in tickers:
            strat.add_watch_ticker(ticker)
        return strat

    # arb
    strat = ArbitrageStrategy()
    for a, b in comp_pairs or []:
        strat.register_complementary(a, b)
    return strat


def build_strategy_from_env(tickers: list[str]) -> BaseStrategy:
    """Convenience wrapper: read strategy options from environment variables."""
    return build_strategy(
        os.getenv("KALSHI_STRATEGY", "kelly"),
        tickers,
        model_probs=_parse_model_probs(os.getenv("KALSHI_MODEL_PROB")),
        comp_pairs=_parse_comp_pairs(os.getenv("KALSHI_ARB_PAIRS")),
    )
