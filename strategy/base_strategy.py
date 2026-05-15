"""
strategy/base_strategy.py

Abstract base class all strategies must implement.
The strategy engine calls `evaluate(tick)` and expects either a Signal or None.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Side(str, Enum):
    YES = "yes"
    NO  = "no"


@dataclass
class Signal:
    """
    A trading signal produced by a strategy.

    Attributes:
        ticker:       Kalshi market ticker.
        side:         YES or NO.
        size_cents:   Recommended position size in cents (already Kelly-scaled).
        limit_price:  Limit price in cents (1–99).
        edge:         Raw edge before vig (probability - market_price / 100).
        edge_to_vig:  Edge expressed as a ratio over the half-spread (vig proxy).
        confidence:   Model confidence 0–1 (used by Kelly formula).
        strategy:     Name of the strategy that generated this signal.
        meta:         Optional extra metadata for logging.
    """
    ticker:      str
    side:        Side
    size_cents:  int
    limit_price: int
    edge:        float
    edge_to_vig: float
    confidence:  float
    strategy:    str
    meta:        dict[str, Any] | None = None


class BaseStrategy(ABC):
    """
    Pluggable strategy interface.

    Every strategy must implement `evaluate(tick) -> Signal | None`.
    The strategy engine calls this on each normalised tick from the ingestor.
    Returning None means "no trade this tick".
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy identifier."""
        ...

    @abstractmethod
    def evaluate(self, tick: dict[str, Any]) -> Signal | None:
        """
        Given a normalised market tick, return a Signal or None.

        Args:
            tick: Snapshot dict from OrderBook.snapshot(), plus:
                  - "event_type": "snapshot" | "delta" | "trade"

        Returns:
            Signal if the strategy wants to trade, else None.
        """
        ...

    def on_fill(self, fill: dict[str, Any]) -> None:
        """
        Optional hook called when one of this strategy's orders fills.
        Override to update internal state (e.g., track inventory).
        """
