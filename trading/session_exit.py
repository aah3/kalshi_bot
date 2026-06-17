"""
trading/session_exit.py

Optional auto-shutdown conditions for ``main.py`` session loops.

``--exit-on-settle``  — all session tickers finalized on Kalshi and blotter clean.
``--exit-when-flat``  — no in-flight bot work and the event/market window is over
                        (does not wait for official settlement).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from discovery.game_close import estimated_game_end, is_game_ticker
from discovery.market_client import MarketSummary

SETTLED_STATUSES = frozenset({"settled", "finalized"})
TERMINAL_MARKET_STATUSES = frozenset({"closed", "settled", "finalized"})
IN_FLIGHT_BLOTTER_STATUSES = frozenset({"open", "partially_hedged"})
IN_FLIGHT_STRATEGY_STATES = frozenset({
    "watching",
    "entered",
    "hedging",
    "stopping",
    "exit_pending",
})


def _session_set(session_tickers: list[str]) -> set[str]:
    return set(session_tickers)


def blotter_for_session(
    open_blotter: list[dict[str, Any]],
    session_tickers: list[str],
) -> list[dict[str, Any]]:
    tickers = _session_set(session_tickers)
    return [row for row in open_blotter if row.get("ticker") in tickers]


def orders_for_session(
    open_orders: dict[str, dict[str, Any]],
    session_tickers: list[str],
) -> list[str]:
    tickers = _session_set(session_tickers)
    return [
        order_id
        for order_id, order in open_orders.items()
        if order.get("ticker") in tickers
    ]


def market_is_done(
    ticker: str,
    api_status: str | None,
    market: MarketSummary | None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when the market/event is no longer actively tradeable."""
    status = (api_status or "open").lower()
    if status in TERMINAL_MARKET_STATUSES:
        return True

    now = now or datetime.now(timezone.utc)
    if is_game_ticker(ticker):
        game_end = estimated_game_end(ticker)
        if game_end is not None and now > game_end:
            return True

    if market is not None and market.minutes_to_close is not None:
        if market.minutes_to_close <= 0:
            return True

    return False


def in_flight_strategy_states(
    strategy_states: dict[str, str],
    session_tickers: list[str],
) -> dict[str, str]:
    tickers = _session_set(session_tickers)
    return {
        ticker: state
        for ticker, state in strategy_states.items()
        if ticker in tickers and state.lower() in IN_FLIGHT_STRATEGY_STATES
    }


def should_exit_on_settle(
    session_tickers: list[str],
    market_statuses: dict[str, dict[str, Any]],
    open_blotter: list[dict[str, Any]],
) -> str | None:
    """
    Return a human-readable reason when ``--exit-on-settle`` should stop the bot.

    Requires every session ticker to be ``settled``/``finalized`` on the exchange
    and no open blotter parents on those tickers.
    """
    if not session_tickers:
        return None

    pending: list[str] = []
    for ticker in session_tickers:
        status = (market_statuses.get(ticker, {}).get("status") or "open").lower()
        if status not in SETTLED_STATUSES:
            pending.append(f"{ticker} status={status!r}")

    if pending:
        return None

    session_blotter = blotter_for_session(open_blotter, session_tickers)
    if session_blotter:
        ids = ", ".join(row["trade_id"] for row in session_blotter[:5])
        return None

    return (
        f"all {len(session_tickers)} session ticker(s) settled; "
        "no open blotter trades on session tickers"
    )


def should_exit_when_flat(
    session_tickers: list[str],
    market_statuses: dict[str, dict[str, Any]],
    markets: dict[str, MarketSummary | None],
    open_blotter: list[dict[str, Any]],
    open_orders: dict[str, dict[str, Any]],
    strategy_states: dict[str, str],
    *,
    now: datetime | None = None,
) -> str | None:
    """
    Return a human-readable reason when ``--exit-when-flat`` should stop the bot.

    Requires no resting orders, no in-flight entry legs, no active strategy states,
    and every session ticker to be past its tradeable window.
    """
    if not session_tickers:
        return None

    order_ids = orders_for_session(open_orders, session_tickers)
    if order_ids:
        return None

    session_blotter = blotter_for_session(open_blotter, session_tickers)
    in_flight_blotter = [
        row for row in session_blotter
        if (row.get("status") or "open").lower() in IN_FLIGHT_BLOTTER_STATUSES
    ]
    if in_flight_blotter:
        return None

    active = in_flight_strategy_states(strategy_states, session_tickers)
    if active:
        return None

    not_done = [
        ticker
        for ticker in session_tickers
        if not market_is_done(
            ticker,
            market_statuses.get(ticker, {}).get("status"),
            markets.get(ticker),
            now=now,
        )
    ]
    if not_done:
        return None

    return (
        f"session flat on {len(session_tickers)} ticker(s); "
        "no orders, no in-flight legs, market/event window complete"
    )
