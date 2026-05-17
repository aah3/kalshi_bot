"""
strategy/position_limits.py

Count open / in-flight positions for concurrency limits.
"""

from __future__ import annotations

from strategy.base_strategy import BaseStrategy
from strategy.green_up_strategy import GreenUpStrategy, PositionState as GUState
from strategy.high_prob_strategy import HighProbStrategy, PositionState as HPState


def count_open_positions(
    strategy: BaseStrategy,
    *,
    exclude_ticker: str | None = None,
) -> int:
    """
    Positions that reserve capital or have a live/pending order.

    Does not count green_up SCANNING (watching only) or closed states.
    ``exclude_ticker`` omits one market (used before submitting a new entry
    that already moved to WATCHING in ``evaluate()``).
    """
    if isinstance(strategy, GreenUpStrategy):
        open_states = {
            GUState.WATCHING,
            GUState.ENTERED,
            GUState.HEDGING,
            GUState.STOPPING,
        }
        return sum(
            1
            for ticker, p in strategy._positions.items()
            if ticker != exclude_ticker and p.state in open_states
        )

    if isinstance(strategy, HighProbStrategy):
        open_states = {
            HPState.WATCHING,
            HPState.ENTERED,
            HPState.EXIT_PENDING,
        }
        return sum(
            1
            for ticker, p in strategy._positions.items()
            if ticker != exclude_ticker and p.state in open_states
        )

    return 0
