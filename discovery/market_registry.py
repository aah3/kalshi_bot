"""In-memory market metadata from discovery (used for live entry gates)."""

from __future__ import annotations

from discovery.market_client import MarketSummary

_by_ticker: dict[str, MarketSummary] = {}


def set_markets(markets: list[MarketSummary]) -> None:
    global _by_ticker
    _by_ticker = {m.ticker: m for m in markets}


def register_markets(markets: list[MarketSummary]) -> None:
    """Merge market metadata into the registry without clearing existing entries."""
    for m in markets:
        if m.ticker:
            _by_ticker[m.ticker] = m


def get_market(ticker: str) -> MarketSummary | None:
    return _by_ticker.get(ticker)


def clear() -> None:
    _by_ticker.clear()
