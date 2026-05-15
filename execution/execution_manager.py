"""
execution/execution_manager.py

Handles the full order lifecycle:
  - Submitting limit orders to Kalshi's REST API
  - Auto-refreshing the auth token every TOKEN_REFRESH_INTERVAL_SECONDS
  - Cancelling all resting orders on graceful shutdown (SIGINT)
  - Logging every ORDER_SENT and FILL_RECEIVED event
"""

import asyncio
import json
import uuid
from typing import Any

import aiohttp

import config
from credentials.credential_manager import CredentialManager
from execution.rate_limiter import BucketType, RateLimiter
from logging_.structured_logger import logger
from strategy.base_strategy import Signal


class ExecutionError(Exception):
    """Raised when an order cannot be placed or confirmed."""


class ExecutionManager:
    """
    Submits orders, manages the session token lifecycle, and provides
    a cancel-all method for graceful shutdown.

    Args:
        credentials: CredentialManager instance.
        rate_limiter: Shared RateLimiter instance.
    """

    def __init__(
        self,
        credentials: CredentialManager,
        rate_limiter: RateLimiter,
    ) -> None:
        self._creds        = credentials
        self._limiter      = rate_limiter
        self._session: aiohttp.ClientSession | None = None
        self._open_orders: dict[str, dict] = {}   # order_id -> order metadata
        self._token_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the HTTP session and start the background token refresh loop."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS)
        )
        self._token_task = asyncio.create_task(self._token_refresh_loop())
        logger.info("ExecutionManager started", env=config.ENV)

    async def stop(self) -> None:
        """Cancel all resting orders then close the session. Call on SIGINT."""
        logger.shutdown(open_orders=list(self._open_orders.keys()))
        await self.cancel_all_orders()

        if self._token_task:
            self._token_task.cancel()
            try:
                await self._token_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()

        logger.info("ExecutionManager stopped cleanly")

    # ── Order management ──────────────────────────────────────────────────────

    async def submit_order(self, signal: Signal) -> dict[str, Any] | None:
        """
        Place a limit order derived from a Signal.

        Returns the exchange order dict on success, or None on failure.
        """
        client_order_id = str(uuid.uuid4())
        body = {
            "ticker":           signal.ticker,
            "client_order_id":  client_order_id,
            "type":             "limit",
            "action":           "buy",
            "side":             signal.side.value,
            "count":            signal.size_cents,   # Kalshi uses cents as count unit
            "yes_price":        signal.limit_price if signal.side.value == "yes" else 100 - signal.limit_price,
            "expiration_ts":    None,   # GTC
        }

        body_str = json.dumps(body)
        path     = "/trade-api/v2/portfolio/orders"
        headers  = self._creds.sign_request("POST", path, body=body_str)

        logger.order_sent(
            ticker=signal.ticker,
            side=signal.side.value,
            size_cents=signal.size_cents,
            limit_price=signal.limit_price,
            client_order_id=client_order_id,
            strategy=signal.strategy,
            edge=signal.edge,
            edge_to_vig=signal.edge_to_vig,
        )

        async with self._limiter.throttle(BucketType.WRITE):
            try:
                resp = await self._session.post(
                    f"{config.BASE_URL}{path}",
                    data=body_str,
                    headers=headers,
                )

                if resp.status == 429:
                    await self._limiter.on_429(BucketType.WRITE)
                    return None

                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error(
                        f"Order rejected: HTTP {resp.status}",
                        ticker=signal.ticker,
                        response=text[:300],
                    )
                    return None

                self._limiter.reset_backoff(BucketType.WRITE)
                order = (await resp.json()).get("order", {})
                self._open_orders[order.get("order_id", client_order_id)] = order
                return order

            except aiohttp.ClientError as exc:
                logger.error(f"HTTP error placing order: {exc}", ticker=signal.ticker)
                return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single resting order by ID. Returns True on success."""
        path    = f"/trade-api/v2/portfolio/orders/{order_id}"
        headers = self._creds.sign_request("DELETE", path)

        async with self._limiter.throttle(BucketType.WRITE):
            try:
                resp = await self._session.delete(
                    f"{config.BASE_URL}{path}",
                    headers=headers,
                )

                if resp.status == 429:
                    await self._limiter.on_429(BucketType.WRITE)
                    return False

                success = resp.status in (200, 204)
                if success:
                    self._open_orders.pop(order_id, None)
                    logger.info("Order cancelled", order_id=order_id)
                else:
                    text = await resp.text()
                    logger.warning(
                        f"Cancel failed: HTTP {resp.status}",
                        order_id=order_id,
                        response=text[:200],
                    )
                return success

            except aiohttp.ClientError as exc:
                logger.error(f"HTTP error cancelling order: {exc}", order_id=order_id)
                return False

    async def cancel_all_orders(self) -> None:
        """Cancel every tracked resting order. Used by the kill switch and shutdown."""
        if not self._open_orders:
            logger.info("No open orders to cancel")
            return

        order_ids = list(self._open_orders.keys())
        logger.info(f"Cancelling {len(order_ids)} open orders", order_ids=order_ids)
        results = await asyncio.gather(
            *[self.cancel_order(oid) for oid in order_ids],
            return_exceptions=True,
        )
        failed = [oid for oid, ok in zip(order_ids, results) if ok is not True]
        if failed:
            logger.error(f"Failed to cancel {len(failed)} orders", failed_ids=failed)

    def record_fill(self, fill_event: dict[str, Any]) -> None:
        """
        Call when a fill is received from the WebSocket trade stream.
        Removes the order from open_orders tracking and logs the fill.
        """
        order_id   = fill_event.get("order_id", "")
        filled_qty = fill_event.get("count", 0)
        price      = fill_event.get("yes_price", 0)

        self._open_orders.pop(order_id, None)

        logger.fill_received(
            order_id=order_id,
            filled_cents=filled_qty,
            price=price,
            ticker=fill_event.get("ticker", ""),
            side=fill_event.get("side", ""),
        )

    @property
    def open_orders(self) -> dict[str, dict]:
        return dict(self._open_orders)

    # ── Token refresh loop ────────────────────────────────────────────────────

    async def _token_refresh_loop(self) -> None:
        """
        Background task: refresh the auth token every 25 minutes.
        Kalshi tokens expire after 30 minutes; we refresh with 5 min to spare.
        """
        while True:
            await asyncio.sleep(config.TOKEN_REFRESH_INTERVAL_SECONDS)
            try:
                await self._refresh_token()
            except Exception as exc:
                logger.error(f"Token refresh failed: {exc}")

    async def _refresh_token(self) -> None:
        path    = "/trade-api/v2/auth/refresh"
        headers = self._creds.sign_request("POST", path)

        async with self._limiter.throttle(BucketType.WRITE):
            resp = await self._session.post(f"{config.BASE_URL}{path}", headers=headers)

            if resp.status == 200:
                logger.token_refresh(status="ok")
            else:
                text = await resp.text()
                logger.warning("Token refresh non-200", status=resp.status, body=text[:200])
