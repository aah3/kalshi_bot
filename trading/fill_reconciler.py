"""
trading/fill_reconciler.py

REST-based fill reconciliation — a safety net for the WebSocket-only fill path.

The bot normally learns about fills through the authenticated WebSocket ``fill``
channel. If that channel silently stalls (half-open socket, server-side
subscription drop), exits such as take-profit / stop-loss / hedge fills are never
delivered, leaving positions unmanaged and the blotter/strategy state diverged
from the exchange.

This loop periodically polls ``GET /portfolio/fills`` and re-drives the same
``on_fill`` callback the WebSocket uses for any fill that belongs to an order the
bot is still tracking. Deduplication (by fill id) lives in ``on_fill`` so the WS
and REST paths can run concurrently without double-counting.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import config
from ingestion.market_ingestor import normalize_fill_message
from logging_.structured_logger import logger

FetchFills    = Callable[[], Awaitable[list[dict[str, Any]]]]
OnFill        = Callable[[dict[str, Any]], None]
KnownOrderIds = Callable[[], set[str]]


async def run_fill_reconciliation_loop(
    *,
    fetch_fills: FetchFills,
    on_fill: OnFill,
    known_order_ids: KnownOrderIds,
    shutdown_event: asyncio.Event,
    interval_seconds: float | None = None,
    fills_limit: int = 100,
) -> None:
    """
    Periodically reconcile exchange fills against tracked open orders.

    No-op when ``interval_seconds <= 0`` (feature disabled).
    """
    interval = (
        interval_seconds
        if interval_seconds is not None
        else config.FILL_RECONCILE_SECONDS
    )
    if interval <= 0:
        logger.info("Fill reconciliation disabled (FILL_RECONCILE_SECONDS <= 0)")
        return

    logger.info("Fill reconciliation started", interval_seconds=interval)

    while not shutdown_event.is_set():
        try:
            await _reconcile_once(fetch_fills, on_fill, known_order_ids, fills_limit)
        except Exception as exc:
            logger.warning(f"Fill reconciliation poll failed: {exc}")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


async def _reconcile_once(
    fetch_fills: FetchFills,
    on_fill: OnFill,
    known_order_ids: KnownOrderIds,
    fills_limit: int,
) -> int:
    """Fetch recent fills and route any that match a tracked open order."""
    known = known_order_ids()
    if not known:
        return 0

    fills = await fetch_fills()
    recovered = 0
    for raw in fills:
        fill = normalize_fill_message(raw)
        order_id = fill.get("order_id", "")
        if not order_id or order_id not in known:
            # Not one of our still-open orders — either already reconciled via
            # the WS path (order popped from tracking) or not ours at all.
            continue
        if not fill.get("ticker"):
            continue

        logger.warning(
            "Fill reconciliation: recovering fill missed by WebSocket",
            order_id=order_id,
            ticker=fill.get("ticker"),
            side=fill.get("side"),
            action=fill.get("action"),
            price=fill.get("price"),
            contracts=fill.get("contracts"),
            source="rest_reconcile",
        )
        # on_fill dedups by fill id, so a fill the WS also delivers is harmless.
        on_fill(fill)
        recovered += 1

    if recovered:
        logger.info("Fill reconciliation recovered fills", count=recovered)
    return recovered
