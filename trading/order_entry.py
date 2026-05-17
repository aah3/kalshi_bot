"""
trading/order_entry.py

Manual order entry — place market or limit orders directly on a selected ticker
without going through the strategy engine.

This is the human-in-the-loop interface used during paper trading and live
sessions when you want to manually act on a screener result.

Supports:
  - Market orders  (fills immediately at best available price)
  - Limit orders   (rests in the book at your specified price)
  - GTC / IOC / FOK time-in-force options
  - YES and NO sides
  - Immediate validation against current order book before submitting

Order lifecycle
───────────────
    place_order()  →  validates against live book  →  submits to Kalshi REST
                   →  returns OrderReceipt
                   →  caller polls get_order_status() or waits for WS fill

Kalshi REST endpoints used
──────────────────────────
    POST   /portfolio/orders            create order
    GET    /portfolio/orders/{id}       poll order status
    DELETE /portfolio/orders/{id}       cancel resting order
    GET    /portfolio/orders            list open orders
    GET    /portfolio/positions         list current positions
    GET    /portfolio/balance           account balance
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import aiohttp

import config
from credentials.credential_manager import CredentialManager
from discovery.market_client import MarketClient, OrderBookSnapshot
from discovery.orderbook_parse import market_order_yes_price, worst_market_fill_price
from execution.rate_limiter import BucketType, RateLimiter
from logging_.structured_logger import logger


# ── Order types ───────────────────────────────────────────────────────────────

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT  = "limit"


class OrderSide(str, Enum):
    YES = "yes"
    NO  = "no"


class TimeInForce(str, Enum):
    """Kalshi API values (CLI aliases: gtc / ioc / fok)."""
    GTC = "good_till_canceled"
    IOC = "immediate_or_cancel"
    FOK = "fill_or_kill"

    @classmethod
    def from_cli(cls, value: str) -> "TimeInForce":
        aliases = {
            "gtc": cls.GTC,
            "good_till_canceled": cls.GTC,
            "ioc": cls.IOC,
            "immediate_or_cancel": cls.IOC,
            "fok": cls.FOK,
            "fill_or_kill": cls.FOK,
        }
        key = value.strip().lower()
        if key not in aliases:
            raise ValueError(f"Unknown time_in_force {value!r}")
        return aliases[key]


class OrderStatus(str, Enum):
    PENDING   = "pending"
    RESTING   = "resting"
    FILLED    = "filled"
    CANCELLED = "cancelled"
    REJECTED  = "rejected"

    @classmethod
    def from_kalshi(cls, value: str | None) -> "OrderStatus":
        """Map Kalshi API status strings to internal enum values."""
        key = (value or "pending").strip().lower()
        aliases = {
            "pending": cls.PENDING,
            "resting": cls.RESTING,
            "executed": cls.FILLED,
            "filled": cls.FILLED,
            "canceled": cls.CANCELLED,
            "cancelled": cls.CANCELLED,
            "rejected": cls.REJECTED,
        }
        return aliases.get(key, cls.PENDING)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class OrderRequest:
    """
    A fully-specified order before submission.

    For market orders: limit_price is None, Kalshi fills at best available.
    For limit orders:  limit_price must be in [1, 99] cents.

    count = number of contracts (each contract pays $1 at resolution).
    Each contract costs limit_price cents to buy YES,
    or (100 - limit_price) cents to buy NO.
    """
    ticker:       str
    side:         OrderSide
    order_type:   OrderType
    count:        int              # number of contracts (integer, not cents)
    limit_price:  int | None       # cents (1-99) for limit; None for market
    market_max_price: int | None = None  # optional cap for market (else from book)
    time_in_force: TimeInForce    = TimeInForce.GTC
    action:       str             = "buy"   # buy | sell
    note:         str             = ""   # optional human label for this trade

    @property
    def yes_price(self) -> int | None:
        """Kalshi always expresses price as YES price."""
        if self.limit_price is None:
            return None
        return self.limit_price if self.side == OrderSide.YES else 100 - self.limit_price

    @property
    def estimated_cost_cents(self) -> int | None:
        """Estimated total cost in cents. None for market orders."""
        if self.limit_price is None:
            return None
        price = self.limit_price if self.side == OrderSide.YES else 100 - self.limit_price
        return price * self.count

    @property
    def max_payout_cents(self) -> int:
        """Maximum payout if position wins: always $1 per contract."""
        return self.count * 100

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty = valid)."""
        errors: list[str] = []
        if not self.ticker:
            errors.append("ticker is required")
        if self.count <= 0:
            errors.append(f"count must be > 0, got {self.count}")
        if self.action not in ("buy", "sell"):
            errors.append(f"action must be buy or sell, got {self.action!r}")
        if self.order_type == OrderType.LIMIT:
            if self.limit_price is None:
                errors.append("limit_price required for LIMIT orders")
            elif not (1 <= self.limit_price <= 99):
                errors.append(f"limit_price must be 1-99, got {self.limit_price}")
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            errors.append("limit_price must be None for MARKET orders")
        if self.market_max_price is not None and not (1 <= self.market_max_price <= 99):
            errors.append(
                f"market_max_price must be 1-99, got {self.market_max_price}"
            )
        return errors


@dataclass
class OrderReceipt:
    """Returned immediately after order submission."""
    order_id:         str
    client_order_id:  str
    ticker:           str
    side:             OrderSide
    order_type:       OrderType
    count:            int
    limit_price:      int | None
    yes_price:        int | None
    status:           OrderStatus
    submitted_at:     datetime
    estimated_cost_cents: int | None
    max_payout_cents: int
    note:             str = ""

    def summary(self) -> str:
        cost_str = f"${self.estimated_cost_cents/100:.2f}" if self.estimated_cost_cents else "market"
        return (
            f"ORDER {self.order_id[:8]}  "
            f"{self.order_type.value.upper()} {self.side.value.upper()} "
            f"{self.count} × {self.ticker}  "
            f"@ {self.limit_price or 'MKT'}c  "
            f"cost≈{cost_str}  "
            f"max_payout=${self.max_payout_cents/100:.2f}  "
            f"status={self.status.value}"
        )


@dataclass
class OrderStatus_Detail:
    """Full order status from a poll or fill event."""
    order_id:          str
    ticker:            str
    side:              str
    order_type:        str
    status:            OrderStatus
    count:             int              # contracts requested
    filled_count:      int              # contracts filled so far
    remaining_count:   int              # contracts still resting
    yes_price:         int | None
    avg_fill_price:    int | None       # average yes_price across fills
    total_cost_cents:  int              # actual money spent
    created_at:        datetime | None
    updated_at:        datetime | None

    @property
    def fill_pct(self) -> float:
        return (self.filled_count / self.count * 100) if self.count > 0 else 0.0

    def summary(self) -> str:
        return (
            f"{self.order_id[:8]}  {self.status.value:<10}  "
            f"filled {self.filled_count}/{self.count} ({self.fill_pct:.0f}%)  "
            f"avg_price={self.avg_fill_price or '?'}c  "
            f"cost=${self.total_cost_cents/100:.2f}"
        )


# ── Order entry client ────────────────────────────────────────────────────────

class OrderEntry:
    """
    Manual order placement and status management.

    Usage (as async context manager):
        async with OrderEntry(creds, limiter) as oe:
            book    = await oe.preview_order(request)
            receipt = await oe.place_order(request)
            status  = await oe.get_order_status(receipt.order_id)
    """

    def __init__(
        self,
        credentials: CredentialManager,
        rate_limiter: RateLimiter,
    ) -> None:
        self._creds   = credentials
        self._limiter = rate_limiter
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "OrderEntry":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS)
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    # ── Pre-trade validation ──────────────────────────────────────────────────

    async def preview_order(
        self,
        request: OrderRequest,
        market_client: MarketClient,
    ) -> dict[str, Any]:
        """
        Fetch the live order book and show what the order will cost and where
        it sits relative to current prices. Does NOT submit anything.

        Returns a rich preview dict suitable for CLI display.
        """
        errors = request.validate()
        if errors:
            return {"valid": False, "errors": errors}

        book = await market_client.get_order_book(request.ticker)
        if not book:
            return {"valid": False, "errors": ["Market not found or no book data"]}

        best_bid = book.best_bid
        best_ask = book.best_ask
        spread   = book.spread

        # Position relative to the book
        price    = request.limit_price
        book_position = None
        warning   = None

        if price and book.best_ask and book.best_bid:
            if request.side == OrderSide.YES:
                if price >= best_ask:
                    book_position = "aggressive — will likely fill immediately"
                elif price >= best_bid:
                    book_position = "inside spread — may or may not fill"
                else:
                    book_position = "passive — rests below best bid, slow fill"
            else:  # NO side; NO ask = 100 - YES bid
                no_ask = 100 - best_bid
                no_bid = 100 - best_ask
                if price >= no_ask:
                    book_position = "aggressive — will likely fill immediately"
                elif price >= no_bid:
                    book_position = "inside spread"
                else:
                    book_position = "passive — rests below best NO bid"

        # Slippage / cost estimate for market orders (walk YES ask ladder)
        slippage_estimate = None
        est_cost_cents: int | None = request.estimated_cost_cents
        est_yes_price: int | None = request.yes_price

        if request.order_type == OrderType.MARKET and request.side == OrderSide.YES:
            if book.yes_asks:
                runnable  = 0
                cost      = 0
                remaining = request.count
                for lvl_price, lvl_qty in book.yes_asks:
                    take = min(remaining, lvl_qty)
                    cost     += take * lvl_price
                    runnable += take
                    remaining -= take
                    if remaining <= 0:
                        break
                avg_fill  = cost // runnable if runnable else None
                slippage  = (avg_fill - best_ask) if avg_fill is not None and best_ask else None
                est_cost_cents = cost if runnable else None
                est_yes_price  = worst_market_fill_price(book, "yes", request.count)
                slippage_estimate = {
                    "avg_fill_price_cents": avg_fill,
                    "total_cost_cents":     cost,
                    "slippage_cents":       slippage,
                    "fully_fillable":       remaining == 0,
                    "fillable_count":       runnable,
                }
            elif best_ask is not None:
                est_cost_cents = best_ask * request.count
                est_yes_price  = best_ask
                slippage_estimate = {
                    "avg_fill_price_cents": best_ask,
                    "total_cost_cents":     est_cost_cents,
                    "slippage_cents":       0,
                    "fully_fillable":       None,
                    "fillable_count":       None,
                }
        elif request.order_type == OrderType.MARKET and request.side == OrderSide.NO:
            cap = worst_market_fill_price(book, "no", request.count)
            est_yes_price = 100 - cap
            est_cost_cents = cap * request.count
            if best_bid is not None:
                slippage_estimate = {
                    "avg_fill_price_cents": cap,
                    "total_cost_cents":     est_cost_cents,
                    "slippage_cents":       cap - (100 - best_bid),
                    "fully_fillable":       None,
                    "fillable_count":       None,
                }

        market_cap = None
        if request.order_type == OrderType.MARKET:
            market_cap = request.market_max_price
            if market_cap is None and book:
                market_cap = market_order_yes_price(
                    book,
                    request.side.value,
                    request.action,
                    request.count,
                )

        implied_roi = None
        if est_cost_cents and est_cost_cents > 0:
            implied_roi = round(
                (request.max_payout_cents - est_cost_cents) / est_cost_cents * 100, 1
            )

        return {
            "valid":              True,
            "ticker":             request.ticker,
            "action":             request.action,
            "side":               request.side.value,
            "order_type":         request.order_type.value,
            "count":              request.count,
            "limit_price":        price,
            "yes_price":          est_yes_price,
            "market_cap":         market_cap,
            "estimated_cost_usd": round(est_cost_cents / 100, 2) if est_cost_cents else None,
            "max_payout_usd":     round(request.max_payout_cents / 100, 2),
            "implied_roi_pct":    implied_roi,
            "book": {
                "best_bid":  best_bid,
                "best_ask":  best_ask,
                "spread":    spread,
                "mid_price": round(book.mid_price, 1) if book.mid_price is not None else None,
            },
            "book_position":       book_position,
            "slippage_estimate":   slippage_estimate,
            "warning":             warning,
        }

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> OrderReceipt | None:
        """
        Submit a market or limit order to Kalshi.

        Returns an OrderReceipt on success, or None on failure.
        """
        errors = request.validate()
        if errors:
            logger.error("Order validation failed", errors=errors, ticker=request.ticker)
            return None

        client_order_id = str(uuid.uuid4())
        tif = request.time_in_force.value
        if request.order_type == OrderType.MARKET and tif == TimeInForce.GTC.value:
            tif = TimeInForce.IOC.value

        body: dict[str, Any] = {
            "ticker":          request.ticker,
            "client_order_id": client_order_id,
            "type":            request.order_type.value,
            "action":          request.action,
            "side":            request.side.value,
            "count":           request.count,
            "count_fp":        f"{request.count:.2f}",
            "time_in_force":   tif,
        }

        submit_yes_price: int | None = None
        submit_no_price: int | None = None

        if request.order_type == OrderType.LIMIT and request.yes_price is not None:
            if request.side == OrderSide.YES:
                body["yes_price"] = request.yes_price
                submit_yes_price = request.yes_price
            else:
                body["no_price"] = 100 - request.yes_price
                submit_no_price = body["no_price"]
        elif request.order_type == OrderType.MARKET:
            cap = request.market_max_price
            if cap is None:
                async with MarketClient(self._creds, self._limiter) as mc:
                    book = await mc.get_order_book(request.ticker)
                if book is None:
                    logger.error("No order book for market order", ticker=request.ticker)
                    return None
                cap = market_order_yes_price(
                    book,
                    request.side.value,
                    request.action,
                    request.count,
                )
            if request.side == OrderSide.YES:
                body["yes_price"] = cap
                submit_yes_price = cap
            else:
                body["no_price"] = cap
                submit_no_price = cap

        body_str = json.dumps(body, separators=(",", ":"))
        path     = "/portfolio/orders"
        sign_path = f"/trade-api/v2{path}"
        headers  = self._creds.sign_request("POST", sign_path, body=body_str)

        logger.order_sent(
            ticker=request.ticker,
            side=request.side.value,
            action=request.action,
            order_type=request.order_type.value,
            count=request.count,
            limit_price=request.limit_price,
            yes_price=submit_yes_price or request.yes_price,
            time_in_force=tif,
            client_order_id=client_order_id,
            note=request.note,
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
                    logger.error("Rate limited on order placement", ticker=request.ticker)
                    return None

                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error(
                        f"Order rejected: HTTP {resp.status}",
                        ticker=request.ticker,
                        body=text[:400],
                    )
                    return None

                self._limiter.reset_backoff(BucketType.WRITE)
                raw   = await resp.json()
                order = raw.get("order", raw)

                receipt = OrderReceipt(
                    order_id=order.get("order_id", client_order_id),
                    client_order_id=client_order_id,
                    ticker=request.ticker,
                    side=request.side,
                    order_type=request.order_type,
                    count=request.count,
                    limit_price=request.limit_price,
                    yes_price=submit_yes_price or request.yes_price,
                    status=OrderStatus.from_kalshi(order.get("status")),
                    submitted_at=datetime.now(timezone.utc),
                    estimated_cost_cents=request.estimated_cost_cents,
                    max_payout_cents=request.max_payout_cents,
                    note=request.note,
                )

                logger.info(
                    "Order submitted successfully",
                    order_id=receipt.order_id,
                    ticker=request.ticker,
                    summary=receipt.summary(),
                )
                return receipt

            except aiohttp.ClientError as exc:
                logger.error(f"HTTP error placing order: {exc}", ticker=request.ticker)
                return None

    # ── Order status ──────────────────────────────────────────────────────────

    async def get_order_status(self, order_id: str) -> OrderStatus_Detail | None:
        """Poll the current status of one order."""
        path    = f"/portfolio/orders/{order_id}"
        headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")

        async with self._limiter.throttle(BucketType.READ):
            try:
                resp = await self._session.get(
                    f"{config.BASE_URL}{path}", headers=headers
                )
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                raw   = await resp.json()
                order = raw.get("order", raw)
                return self._parse_order_status(order)
            except aiohttp.ClientError as exc:
                logger.error(f"Error fetching order status: {exc}", order_id=order_id)
                return None

    async def list_open_orders(self) -> list[OrderStatus_Detail]:
        """Fetch all currently resting orders from the exchange."""
        path    = "/portfolio/orders"
        headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")

        async with self._limiter.throttle(BucketType.READ):
            resp = await self._session.get(
                f"{config.BASE_URL}{path}",
                params={"status": "resting"},
                headers=headers,
            )
            resp.raise_for_status()
            raw = await resp.json()
            return [self._parse_order_status(o) for o in raw.get("orders", [])]

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific resting order. Returns True on success."""
        path    = f"/portfolio/orders/{order_id}"
        headers = self._creds.sign_request("DELETE", f"/trade-api/v2{path}")

        async with self._limiter.throttle(BucketType.WRITE):
            try:
                resp = await self._session.delete(
                    f"{config.BASE_URL}{path}", headers=headers
                )
                success = resp.status in (200, 204)
                if success:
                    logger.info("Order cancelled", order_id=order_id)
                return success
            except aiohttp.ClientError as exc:
                logger.error(f"Error cancelling order: {exc}", order_id=order_id)
                return False

    async def get_balance(self) -> dict[str, Any]:
        """Fetch current account balance."""
        path    = "/portfolio/balance"
        headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")

        async with self._limiter.throttle(BucketType.READ):
            resp = await self._session.get(f"{config.BASE_URL}{path}", headers=headers)
            resp.raise_for_status()
            return await resp.json()

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_order_status(raw: dict[str, Any]) -> OrderStatus_Detail:
        def _dt(v: str | None) -> datetime | None:
            if not v:
                return None
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None

        def _fp_count(key: str) -> int:
            val = raw.get(key)
            if val is None:
                return 0
            try:
                return int(float(val))
            except (TypeError, ValueError):
                return int(val or 0)

        filled_count   = _fp_count("fill_count_fp") or int(raw.get("filled_count", 0) or 0)
        count          = _fp_count("initial_count_fp") or int(raw.get("count", 0) or 0)
        avg_fill_price = raw.get("avg_price") or raw.get("yes_price")
        total_cost     = int(raw.get("total_cost", 0) or 0)

        status = OrderStatus.from_kalshi(raw.get("status"))

        return OrderStatus_Detail(
            order_id=raw.get("order_id", ""),
            ticker=raw.get("ticker", ""),
            side=raw.get("side", ""),
            order_type=raw.get("type", "limit"),
            status=status,
            count=count,
            filled_count=filled_count,
            remaining_count=max(count - filled_count, 0),
            yes_price=raw.get("yes_price"),
            avg_fill_price=int(avg_fill_price) if avg_fill_price else None,
            total_cost_cents=total_cost,
            created_at=_dt(raw.get("created_time")),
            updated_at=_dt(raw.get("updated_time") or raw.get("expiration_time")),
        )
