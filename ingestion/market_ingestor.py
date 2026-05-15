"""
ingestion/market_ingestor.py

Maintains a live, in-memory order book for all subscribed Kalshi markets.

Responsibilities:
  - Open and maintain a WebSocket connection to Kalshi's stream endpoint
  - Apply delta updates to a local order book (never trust stale REST snapshots)
  - Compute a real-time fair value (mid-price) for each market
  - Emit normalised tick dicts that the strategy engine consumes
  - Reconnect automatically on disconnect with exponential backoff
"""

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

import config
from logging_.structured_logger import logger


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class Level:
    """A single price level in the order book (in cents, 0–100)."""
    price: int   # cents
    quantity: int


@dataclass
class OrderBook:
    """
    Sparse order book for one Kalshi market (YES side only — NO = 100 - YES).

    Kalshi binary markets have a single YES/NO dimension; the book stores
    YES bid/ask levels. NO prices are the complement.
    """
    ticker: str
    yes_bids: dict[int, int] = field(default_factory=dict)  # price -> qty
    yes_asks: dict[int, int] = field(default_factory=dict)
    last_trade_price: int | None = None
    updated_at_us: int = 0

    @property
    def best_bid(self) -> int | None:
        return max(self.yes_bids) if self.yes_bids else None

    @property
    def best_ask(self) -> int | None:
        return min(self.yes_asks) if self.yes_asks else None

    @property
    def mid_price(self) -> float | None:
        """Fair value estimate: simple mid between best bid and ask."""
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def spread(self) -> int | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    def apply_delta(self, side: str, price: int, delta: int) -> None:
        """
        Apply a quantity delta to a book side.
        delta > 0 → add liquidity   delta < 0 → remove liquidity
        """
        book = self.yes_bids if side == "yes_bid" else self.yes_asks
        new_qty = book.get(price, 0) + delta
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty
        self.updated_at_us = int(time.time() * 1_000_000)

    def snapshot(self) -> dict[str, Any]:
        """Return a serialisable point-in-time snapshot of this book."""
        return {
            "ticker":             self.ticker,
            "best_bid":          self.best_bid,
            "best_ask":          self.best_ask,
            "mid_price":         self.mid_price,
            "spread":            self.spread,
            "last_trade_price":  self.last_trade_price,
            "updated_at_us":     self.updated_at_us,
        }


# ── Ingestor ─────────────────────────────────────────────────────────────────

TickCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
FillCallback = Callable[[dict[str, Any]], None]


def normalize_fill_message(data: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Kalshi WebSocket ``fill`` channel payload to the internal fill dict
    consumed by strategies, the circuit breaker, and the blotter hooks.
    """
    ticker = data.get("market_ticker", "")
    side   = (data.get("purchased_side") or data.get("side") or "yes").lower()

    if data.get("yes_price_dollars") is not None:
        yes_price_cents = int(round(float(data["yes_price_dollars"]) * 100))
    elif data.get("yes_price") is not None:
        yes_price_cents = int(data["yes_price"])
    else:
        yes_price_cents = 0

    price_cents = yes_price_cents if side == "yes" else max(100 - yes_price_cents, 0)

    if data.get("count_fp") is not None:
        contracts = int(float(data["count_fp"]))
    else:
        contracts = int(data.get("count") or 0)

    size_cents = contracts * price_cents

    return {
        "ticker":     ticker,
        "side":       side,
        "price":      price_cents,
        "contracts":  contracts,
        "size_cents": size_cents,
        "order_id":   data.get("order_id", ""),
        "trade_id":   data.get("trade_id", ""),
        "is_taker":   data.get("is_taker"),
        "action":     data.get("action"),
    }


class MarketIngestor:
    """
    WebSocket-based market data ingestor.

    Args:
        tickers:      List of Kalshi market tickers to subscribe to.
        on_tick:      Async callback invoked on every normalised tick event.
        credentials:  CredentialManager instance for authenticated WS handshake.
        on_fill:      Sync callback for authenticated user fill notifications
                      (subscribes to the ``fill`` channel when provided).

    Usage:
        ingestor = MarketIngestor(tickers=["PRES-2024-DEM"], on_tick=my_handler)
        await ingestor.run()   # runs until cancelled
    """

    _RECONNECT_BASE_DELAY = 1.0    # seconds
    _RECONNECT_MAX_DELAY  = 60.0   # seconds

    def __init__(
        self,
        tickers: list[str],
        on_tick: TickCallback,
        credentials=None,
        on_fill: FillCallback | None = None,
    ) -> None:
        self._tickers     = tickers
        self._on_tick     = on_tick
        self._on_fill     = on_fill
        self._credentials = credentials
        self._books: dict[str, OrderBook] = {t: OrderBook(ticker=t) for t in tickers}
        self._running     = False
        self._reconnect_delay = self._RECONNECT_BASE_DELAY

    # ── Public ───────────────────────────────────────────────────────────────

    def get_book(self, ticker: str) -> OrderBook | None:
        return self._books.get(ticker)

    def get_all_snapshots(self) -> list[dict[str, Any]]:
        return [b.snapshot() for b in self._books.values()]

    async def run(self) -> None:
        """Connect to the WebSocket and process messages until cancelled."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_stream()
                self._reconnect_delay = self._RECONNECT_BASE_DELAY  # reset on clean exit
            except (ConnectionClosedError, OSError) as exc:
                logger.warning(
                    f"WS disconnected: {exc} — reconnecting in {self._reconnect_delay:.1f}s",
                    reconnect_delay=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY
                )
            except asyncio.CancelledError:
                logger.info("MarketIngestor cancelled — stopping")
                self._running = False
                return

    async def stop(self) -> None:
        self._running = False

    # ── Private ──────────────────────────────────────────────────────────────

    async def _connect_and_stream(self) -> None:
        extra_headers = {}
        if self._credentials:
            # Authenticated WebSocket handshake
            extra_headers = self._credentials.sign_request("GET", "/trade-api/ws/v2")

        logger.info(f"Connecting to WebSocket: {config.WS_URL}", tickers=self._tickers)

        async with websockets.connect(
            config.WS_URL,
            additional_headers=extra_headers,
            ping_interval=config.WS_PING_INTERVAL_SECONDS,
        ) as ws:
            await self._subscribe(ws)
            logger.info("WebSocket connected and subscribed", tickers=self._tickers)

            async for raw_message in ws:
                if not self._running:
                    break
                await self._handle_message(raw_message)

    async def _subscribe(self, ws) -> None:
        """Send subscription commands for all tickers."""
        channels = ["orderbook_delta", "trade"]
        if self._on_fill:
            if self._credentials:
                channels.append("fill")
            else:
                logger.warning(
                    "on_fill provided but no credentials — fill channel not subscribed"
                )

        sub_msg = {
            "id":     1,
            "cmd":    "subscribe",
            "params": {
                "channels":       channels,
                "market_tickers": self._tickers,
            },
        }
        await ws.send(json.dumps(sub_msg))

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received non-JSON WebSocket message", raw=raw[:200])
            return

        msg_type = msg.get("type") or msg.get("msg")
        data      = msg.get("msg", msg)

        if msg_type == "orderbook_snapshot":
            self._apply_snapshot(data)
        elif msg_type == "orderbook_delta":
            self._apply_delta(data)
        elif msg_type == "trade":
            self._apply_trade(data)
        elif msg_type == "fill":
            self._apply_fill(data)
        # Ignore heartbeats and subscription confirmations

    def _apply_snapshot(self, data: dict) -> None:
        ticker = data.get("market_ticker", "")
        book   = self._books.get(ticker)
        if not book:
            return

        book.yes_bids.clear()
        book.yes_asks.clear()

        for level in data.get("yes", {}).get("bids", []):
            book.yes_bids[level["price"]] = level["quantity"]
        for level in data.get("yes", {}).get("asks", []):
            book.yes_asks[level["price"]] = level["quantity"]

        book.updated_at_us = int(time.time() * 1_000_000)
        self._emit_tick(book, event_type="snapshot")

    def _apply_delta(self, data: dict) -> None:
        ticker = data.get("market_ticker", "")
        book   = self._books.get(ticker)
        if not book:
            return

        for delta in data.get("deltas", []):
            book.apply_delta(
                side=delta["side"],
                price=delta["price"],
                delta=delta["delta"],
            )

        self._emit_tick(book, event_type="delta")

    def _apply_trade(self, data: dict) -> None:
        ticker = data.get("market_ticker", "")
        book   = self._books.get(ticker)
        if not book:
            return

        book.last_trade_price = data.get("yes_price")
        self._emit_tick(book, event_type="trade")

    def _apply_fill(self, data: dict) -> None:
        """User order fill from the authenticated ``fill`` channel."""
        if not self._on_fill:
            return

        fill = normalize_fill_message(data)
        if not fill.get("ticker"):
            return

        self._on_fill(fill)

    def _emit_tick(self, book: OrderBook, event_type: str) -> None:
        snap = book.snapshot()
        snap["event_type"] = event_type

        logger.market_price_update(**snap)

        # Fire the strategy callback (non-blocking — schedule as task)
        asyncio.create_task(self._on_tick(snap))
