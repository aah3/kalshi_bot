"""
strategy/position_limits.py

Count open / in-flight positions for concurrency limits.
"""

from __future__ import annotations

from strategy.base_strategy import BaseStrategy
from strategy.green_up_strategy import GreenUpStrategy, PositionState as GUState
from strategy.high_prob_strategy import HighProbStrategy, PositionState as HPState
from strategy.mean_reversion_strategy import MeanReversionStrategy, PositionState as MRState


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

    if isinstance(strategy, MeanReversionStrategy):
        open_states = {
            MRState.WATCHING,
            MRState.ENTERED,
            MRState.EXIT_PENDING,
        }
        return sum(
            1
            for ticker, p in strategy._positions.items()
            if ticker != exclude_ticker and p.state in open_states
        )

    return 0


def ticker_is_protected(
    strategy: BaseStrategy,
    ticker: str,
    *,
    open_order_tickers: set[str] | None = None,
    blotter_open_tickers: set[str] | None = None,
) -> bool:
    """
    True when a ticker must not be unsubscribed / unwatched.

    Protects in-flight strategy states, hedged inventory (green_up HEDGED),
    resting exchange orders, and blotter open/hedged parent trades.
    SCANNING / CLOSED / STOPPED (flat) are not protected by strategy state alone.
    """
    if open_order_tickers and ticker in open_order_tickers:
        return True
    if blotter_open_tickers and ticker in blotter_open_tickers:
        return True

    if isinstance(strategy, GreenUpStrategy):
        pos = strategy._positions.get(ticker)
        if pos is None:
            return False
        return pos.state in {
            GUState.WATCHING,
            GUState.ENTERED,
            GUState.HEDGING,
            GUState.HEDGED,
            GUState.STOPPING,
        }

    if isinstance(strategy, HighProbStrategy):
        pos = strategy._positions.get(ticker)
        if pos is None:
            return False
        return pos.state in {
            HPState.WATCHING,
            HPState.ENTERED,
            HPState.EXIT_PENDING,
        }

    if isinstance(strategy, MeanReversionStrategy):
        pos = strategy._positions.get(ticker)
        if pos is None:
            return False
        return pos.state in {
            MRState.WATCHING,
            MRState.ENTERED,
            MRState.EXIT_PENDING,
        }

    return False
