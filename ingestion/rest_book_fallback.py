"""
ingestion/rest_book_fallback.py

Background poll: when WebSocket order books go stale, refresh via REST and
re-drive strategy.evaluate() with the updated quotes.
"""

from __future__ import annotations

import asyncio

import config
from discovery.market_client import MarketClient
from ingestion.book_freshness import (
    book_age_seconds,
    book_needs_rest_refresh,
    is_book_crossed,
)
from ingestion.market_ingestor import MarketIngestor
from logging_.structured_logger import logger


async def run_rest_book_fallback_loop(
    *,
    ingestor: MarketIngestor,
    market_client: MarketClient,
    shutdown_event: asyncio.Event,
    poll_seconds: float | None = None,
    stale_seconds: float | None = None,
    escalate_after_polls: int | None = None,
) -> None:
    """
    Periodically REST-refresh stale WS books and push ticks to the strategy.

    No-op when ``stale_seconds <= 0`` (feature disabled).

    Escalation (WS-health alarm): when WS books stay stale for
    ``escalate_after_polls`` consecutive polls, the socket is almost certainly
    silently stalled (the REST refresh masks it for market data, but the WS-only
    ``fill`` channel is still dead). We then force a WS reconnect so the fill
    channel is re-established. ``escalate_after_polls <= 0`` disables escalation.
    """
    poll = poll_seconds if poll_seconds is not None else config.WS_BOOK_REST_FALLBACK_POLL_SECONDS
    stale = stale_seconds if stale_seconds is not None else config.WS_BOOK_REST_FALLBACK_SECONDS
    escalate_after = (
        escalate_after_polls
        if escalate_after_polls is not None
        else config.WS_STALE_ESCALATE_POLLS
    )

    if stale <= 0:
        logger.info("REST book fallback disabled (WS_BOOK_REST_FALLBACK_SECONDS <= 0)")
        return

    logger.info(
        "REST book fallback started",
        poll_seconds=poll,
        stale_seconds=stale,
        escalate_after_polls=escalate_after,
        tickers=ingestor.tickers,
    )

    consecutive_stale_polls = 0
    while not shutdown_event.is_set():
        try:
            stale_count = await _refresh_stale_books(ingestor, market_client, stale)
        except Exception as exc:
            logger.warning(f"REST book fallback poll failed: {exc}")
            stale_count = 0

        if stale_count > 0:
            consecutive_stale_polls += 1
            if (
                escalate_after > 0
                and consecutive_stale_polls >= escalate_after
                and hasattr(ingestor, "force_reconnect")
            ):
                logger.warning(
                    "WS health alarm: books stale across consecutive polls — "
                    "forcing WS reconnect to restore the fill channel",
                    consecutive_stale_polls=consecutive_stale_polls,
                    stale_tickers=stale_count,
                    escalate_after_polls=escalate_after,
                )
                try:
                    await ingestor.force_reconnect("rest_fallback_stale_escalation")
                except Exception as exc:
                    logger.warning(f"Forced WS reconnect failed: {exc}")
                consecutive_stale_polls = 0
        else:
            consecutive_stale_polls = 0

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=poll)
            break
        except asyncio.TimeoutError:
            pass


async def _refresh_stale_books(
    ingestor: MarketIngestor,
    market_client: MarketClient,
    stale_seconds: float,
) -> int:
    """Refresh stale WS books from REST. Returns the number of stale tickers."""
    refresh_tickers = [
        ticker
        for ticker in ingestor.tickers
        if book_needs_rest_refresh(ingestor.get_book(ticker), stale_seconds)
    ]
    if not refresh_tickers:
        return 0

    for ticker in refresh_tickers:
        ws_book = ingestor.get_book(ticker)
        ws_age = book_age_seconds(ws_book)
        reason = "crossed" if is_book_crossed(ws_book) else "stale"
        try:
            snap = await market_client.get_order_book(ticker, depth=5)
        except Exception as exc:
            logger.warning(
                "REST book fetch failed",
                ticker=ticker,
                ws_age_seconds=round(ws_age, 1) if ws_age is not None else None,
                error=str(exc),
            )
            continue

        if snap is None:
            logger.warning(
                "REST book empty",
                ticker=ticker,
                ws_age_seconds=round(ws_age, 1) if ws_age is not None else None,
            )
            continue

        if not ingestor.apply_rest_snapshot(snap):
            continue

        logger.info(
            f"REST book refresh (WS {reason})",
            ticker=ticker,
            reason=reason,
            ws_age_seconds=round(ws_age, 1) if ws_age is not None else None,
            ws_best_bid=ws_book.best_bid if ws_book else None,
            ws_best_ask=ws_book.best_ask if ws_book else None,
            best_bid=snap.best_bid,
            best_ask=snap.best_ask,
            spread=snap.spread,
        )
        ingestor.push_tick(ticker, event_type="rest_refresh")

    return len(refresh_tickers)


def make_market_client_from_session(
    portfolio_monitor,
    credentials,
    rate_limiter,
) -> MarketClient | None:
    """Build a MarketClient reusing the portfolio monitor's aiohttp session."""
    if portfolio_monitor is None or portfolio_monitor._session is None:
        return None
    client = MarketClient(credentials, rate_limiter)
    client._session = portfolio_monitor._session
    return client
