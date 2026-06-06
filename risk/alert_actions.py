"""
risk/alert_actions.py

Optional automated responses to portfolio alerts (e.g. take profit on PROFIT_TARGET).
"""

from __future__ import annotations

from typing import Any

import config
from logging_.structured_logger import logger
from risk.alert_manager import Alert, AlertType
from strategy.base_strategy import Side, Signal
from strategy.execution_price import EntryPriceMode, execution_meta, resolve_yes_sell_exit


_auto_tp_submitted: set[str] = set()


def reset_auto_take_profit_state() -> None:
    """Clear per-ticker auto-TP dedup (for tests)."""
    _auto_tp_submitted.clear()


async def handle_auto_take_profit(
    alert: Alert,
    *,
    execution: Any,
    strategy: Any,
    blotter: Any,
    ingestor: Any,
    circuit_breaker: Any,
    alert_manager: Any = None,
) -> bool:
    """
    Submit a cross-spread YES sell when a PROFIT_TARGET alert fires.

    Returns True when an exit order was submitted.
    """
    if alert.alert_type != AlertType.PROFIT_TARGET or not alert.ticker:
        return False

    ticker = alert.ticker
    if ticker in _auto_tp_submitted:
        return False

    from strategy.green_up_strategy import GreenUpStrategy, PositionState

    if isinstance(strategy, GreenUpStrategy):
        pos = strategy.get_position(ticker)
        if pos and pos.state in (
            PositionState.HEDGED,
            PositionState.HEDGING,
            PositionState.STOPPING,
        ):
            logger.info(
                "Auto take-profit skipped — green-up position not a bare YES leg",
                ticker=ticker,
                state=pos.state.value,
            )
            return False

    owned = alert.data.get("bot_owned_contracts")
    if owned is None and blotter is not None:
        try:
            legs = blotter.query_legs(ticker=ticker, status="open")
            owned = sum(leg.contracts for leg in legs if leg.side == "yes")
        except Exception:
            owned = None

    if not owned or owned <= 0:
        logger.info(
            "Auto take-profit skipped — no bot-owned YES contracts",
            ticker=ticker,
        )
        return False

    book = ingestor.get_book(ticker) if ingestor else None
    tick: dict[str, Any] = {"ticker": ticker}
    if book is not None:
        snap = book.snapshot()
        tick.update(snap)

    best_bid = tick.get("best_bid")
    best_ask = tick.get("best_ask")
    if best_bid is None:
        logger.warning("Auto take-profit skipped — no bid on book", ticker=ticker)
        return False
    if best_ask is None:
        best_ask = 99 if best_bid >= 99 else min(99, best_bid + 1)

    sell_price, order_type, tif = resolve_yes_sell_exit(
        EntryPriceMode.CROSS_SPREAD,
        best_bid,
        best_ask,
    )
    if sell_price <= 0 or sell_price >= 100:
        return False

    size_cents = owned * sell_price
    signal = Signal(
        ticker=ticker,
        side=Side.YES,
        size_cents=size_cents,
        limit_price=sell_price,
        edge=0.0,
        edge_to_vig=0.0,
        confidence=1.0,
        strategy=getattr(strategy, "name", "auto_take_profit"),
        meta={
            **execution_meta(
                order_type=order_type,
                time_in_force=tif,
                price_mode="cross_spread",
                phase="exit",
                action="sell",
            ),
            "auto_take_profit": True,
            "alert_type": alert.alert_type.value,
            "contracts": owned,
        },
    )

    if circuit_breaker and not circuit_breaker.approve(signal):
        logger.warning("Auto take-profit blocked by circuit breaker", ticker=ticker)
        return False

    result = await execution.submit_order(signal)
    if not result:
        return False

    _auto_tp_submitted.add(ticker)
    order_id = result.get("order_id", "")
    if alert_manager and order_id:
        alert_manager.register_order(order_id, ticker)
    logger.info(
        "Auto take-profit order submitted",
        ticker=ticker,
        order_id=order_id,
        contracts=owned,
        sell_price_cents=sell_price,
        target_pct=config.PROFIT_TARGET_PCT * 100,
    )
    return True
