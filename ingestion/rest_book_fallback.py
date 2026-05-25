"""
ingestion/rest_book_fallback.py

Background poll: when WebSocket order books go stale, refresh via REST and
re-drive strategy.evaluate() with the updated quotes.
"""

from __future__ import annotations

import asyncio

import config
from discovery.market_client import MarketClient
from ingestion.book_freshness import book_age_seconds, is_book_stale
from ingestion.market_ingestor import MarketIngestor
from logging_.structured_logger import logger


async def run_rest_book_fallback_loop(
    *,
    ingestor: MarketIngestor,
    market_client: MarketClient,
    shutdown_event: asyncio.Event,
    poll_seconds: float | None = None,
    stale_seconds: float | None = None,
) -> None:
    """
    Periodically REST-refresh stale WS books and push ticks to the strategy.

    No-op when ``stale_seconds <= 0`` (feature disabled).
    """
    poll = poll_seconds if poll_seconds is not None else config.WS_BOOK_REST_FALLBACK_POLL_SECONDS
    stale = stale_seconds if stale_seconds is not None else config.WS_BOOK_REST_FALLBACK_SECONDS

    if stale <= 0:
        logger.info("REST book fallback disabled (WS_BOOK_REST_FALLBACK_SECONDS <= 0)")
        return

    logger.info(
        "REST book fallback started",
        poll_seconds=poll,
        stale_seconds=stale,
        tickers=ingestor.tickers,
    )

    while not shutdown_event.is_set():
        try:
            await _refresh_stale_books(ingestor, market_client, stale)
        except Exception as exc:
            logger.warning(f"REST book fallback poll failed: {exc}")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=poll)
            break
        except asyncio.TimeoutError:
            pass


async def _refresh_stale_books(
    ingestor: MarketIngestor,
    market_client: MarketClient,
    stale_seconds: float,
) -> None:
    stale_tickers = [
        ticker
        for ticker in ingestor.tickers
        if is_book_stale(ingestor.get_book(ticker), stale_seconds)
    ]
    if not stale_tickers:
        return

    for ticker in stale_tickers:
        ws_age = book_age_seconds(ingestor.get_book(ticker))
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
            "REST book refresh (WS stale)",
            ticker=ticker,
            ws_age_seconds=round(ws_age, 1) if ws_age is not None else None,
            best_bid=snap.best_bid,
            best_ask=snap.best_ask,
            spread=snap.spread,
        )
        ingestor.push_tick(ticker, event_type="rest_refresh")


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
