"""In-memory market metadata from discovery (used for live entry gates)."""

from __future__ import annotations

from discovery.game_close import apply_effective_close
from discovery.market_client import MarketSummary

_by_ticker: dict[str, MarketSummary] = {}


def _store_market(market: MarketSummary) -> None:
    if market.ticker:
        apply_effective_close(market)
        _by_ticker[market.ticker] = market


def set_markets(markets: list[MarketSummary]) -> None:
    global _by_ticker
    _by_ticker = {}
    for m in markets:
        _store_market(m)


def register_markets(markets: list[MarketSummary]) -> None:
    """Merge market metadata into the registry without clearing existing entries."""
    for m in markets:
        _store_market(m)


def get_market(ticker: str) -> MarketSummary | None:
    return _by_ticker.get(ticker)


def clear() -> None:
    _by_ticker.clear()
