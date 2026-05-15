"""
discovery/market_client.py

REST client for Kalshi's market discovery endpoints.

Provides structured access to:
  - Event categories (series)
  - All markets within a category
  - Individual market detail (odds, payout, volume, close time)
  - Order book snapshot for a specific ticker
  - Candlestick / price history

This is a READ-ONLY client — it never places orders.
It uses the shared RateLimiter and CredentialManager so it fits
cleanly into the existing auth and rate-limit infrastructure.

Kalshi REST endpoints used
──────────────────────────
  GET /events                        → list event series (categories)
  GET /events/{event_ticker}         → single event detail + child markets
  GET /markets                       → paginated market list (filterable)
  GET /markets/{ticker}              → single market detail
  GET /markets/{ticker}/orderbook    → current order book snapshot
  GET /markets/{ticker}/history      → candlestick price history
  GET /series                        → all series (broader than events)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

import config
from credentials.credential_manager import CredentialManager
from execution.rate_limiter import BucketType, RateLimiter
from logging_.structured_logger import logger


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class MarketSummary:
    """
    Lightweight summary of one Kalshi market, enriched with derived fields
    useful for strategy screening.
    """
    # Identity
    ticker:          str
    event_ticker:    str
    title:           str
    category:        str        # e.g. "Politics", "Economics", "Sports"
    series_ticker:   str

    # Pricing (all in cents, 1–99)
    yes_bid:         int | None   # best YES bid
    yes_ask:         int | None   # best YES ask
    no_bid:          int | None   # best NO bid  (= 100 - yes_ask)
    no_ask:          int | None   # best NO ask  (= 100 - yes_bid)
    last_price:      int | None   # last trade price (YES)

    # Liquidity
    volume:          int          # total contracts traded (all time)
    volume_24h:      int          # contracts traded in last 24 hours
    open_interest:   int          # open contracts outstanding
    liquidity:       int          # total resting order liquidity (cents)

    # Market lifecycle
    status:          str          # "open", "closed", "settled"
    close_time:      datetime | None
    result:          str | None   # "yes" / "no" / None if unresolved

    # Derived screening fields (computed on construction)
    spread:          int          = field(init=False)   # yes_ask - yes_bid
    mid_price:       float        = field(init=False)   # (yes_bid + yes_ask) / 2
    implied_prob:    float        = field(init=False)   # mid / 100
    yes_decimal_odds: float       = field(init=False)   # 100 / yes_ask
    no_decimal_odds:  float       = field(init=False)   # 100 / no_ask
    minutes_to_close: float | None = field(init=False)

    def __post_init__(self) -> None:
        bid = self.yes_bid or 0
        ask = self.yes_ask or 100

        self.spread       = ask - bid if self.yes_ask and self.yes_bid else 100
        self.mid_price    = (bid + ask) / 2.0 if self.yes_bid and self.yes_ask else 50.0
        self.implied_prob = self.mid_price / 100.0

        self.yes_decimal_odds = round(100.0 / ask, 3) if ask > 0 else 0.0
        no_ask_val            = self.no_ask or (100 - bid)
        self.no_decimal_odds  = round(100.0 / no_ask_val, 3) if no_ask_val > 0 else 0.0

        if self.close_time:
            delta = self.close_time - datetime.now(timezone.utc)
            self.minutes_to_close = delta.total_seconds() / 60.0
        else:
            self.minutes_to_close = None

    def is_tradeable(self) -> bool:
        """Basic sanity check before handing to the strategy engine."""
        return (
            self.status == "open"
            and self.yes_bid is not None
            and self.yes_ask is not None
            and self.spread < 20          # reject illiquid markets (>20c spread)
            and self.volume_24h > 0
            and (self.minutes_to_close is None or self.minutes_to_close > 5)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker":           self.ticker,
            "event_ticker":     self.event_ticker,
            "title":            self.title,
            "category":         self.category,
            "yes_bid":          self.yes_bid,
            "yes_ask":          self.yes_ask,
            "spread":           self.spread,
            "mid_price":        self.mid_price,
            "implied_prob":     round(self.implied_prob, 4),
            "yes_decimal_odds": self.yes_decimal_odds,
            "no_decimal_odds":  self.no_decimal_odds,
            "volume_24h":       self.volume_24h,
            "open_interest":    self.open_interest,
            "liquidity":        self.liquidity,
            "status":           self.status,
            "close_time":       self.close_time.isoformat() if self.close_time else None,
            "minutes_to_close": round(self.minutes_to_close, 1) if self.minutes_to_close else None,
            "is_tradeable":     self.is_tradeable(),
        }


@dataclass
class OrderBookSnapshot:
    """Full order book for one ticker at a point in time."""
    ticker:      str
    fetched_at:  datetime
    yes_bids:    list[tuple[int, int]]   # [(price, qty), ...] descending
    yes_asks:    list[tuple[int, int]]   # [(price, qty), ...] ascending
    best_bid:    int | None
    best_ask:    int | None
    spread:      int | None
    mid_price:   float | None

    def to_tick(self) -> dict[str, Any]:
        """Convert to the normalised tick format consumed by strategy engines."""
        return {
            "ticker":        self.ticker,
            "best_bid":      self.best_bid,
            "best_ask":      self.best_ask,
            "spread":        self.spread,
            "mid_price":     self.mid_price,
            "event_type":    "snapshot",
            "updated_at_us": int(self.fetched_at.timestamp() * 1_000_000),
        }


# ── Client ────────────────────────────────────────────────────────────────────

class MarketClient:
    """
    Async REST client for Kalshi market discovery.

    Usage:
        async with MarketClient(credentials, rate_limiter) as client:
            categories = await client.get_categories()
            markets    = await client.get_markets_by_category("Politics", limit=50)
            book       = await client.get_order_book("PRES-2024-DEM")
    """

    def __init__(
        self,
        credentials: CredentialManager,
        rate_limiter: RateLimiter,
    ) -> None:
        self._creds   = credentials
        self._limiter = rate_limiter
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "MarketClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS)
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    # ── Categories / Series ───────────────────────────────────────────────────

    async def get_categories(self) -> list[str]:
        """
        Return a sorted list of all unique market categories available.

        These map to Kalshi's 'category' field on markets, e.g.:
            Politics, Economics, Sports, Climate, Finance, Tech, Culture, Health
        """
        data = await self._get("/series")
        series_list = data.get("series", [])
        categories = sorted({s.get("category", "Unknown") for s in series_list if s.get("category")})
        logger.info("Fetched categories", count=len(categories), categories=categories)
        return categories

    async def get_series(self) -> list[dict[str, Any]]:
        """
        Return all series (event groups) with their metadata.

        Each series is a collection of related markets, e.g.
        "US Presidential Election 2024" is a series containing markets for
        each candidate.
        """
        data = await self._get("/series")
        return data.get("series", [])

    # ── Market listing ────────────────────────────────────────────────────────

    async def get_markets_by_category(
        self,
        category: str,
        status: str = "open",
        limit: int = 100,
        min_volume_24h: int = 0,
    ) -> list[MarketSummary]:
        """
        Fetch all open markets in a given category.

        Args:
            category:       Category string as returned by get_categories().
            status:         "open" | "closed" | "settled" (default "open").
            limit:          Max markets to return (Kalshi max per page = 200).
            min_volume_24h: Pre-filter — skip markets with less 24h volume.

        Returns:
            List of MarketSummary, sorted by 24h volume descending.
        """
        params = {
            "status":   status,
            "category": category,
            "limit":    min(limit, 200),
        }
        data    = await self._get("/markets", params=params)
        raw     = data.get("markets", [])
        markets = [self._parse_market(m) for m in raw]
        markets = [m for m in markets if m.volume_24h >= min_volume_24h]
        markets.sort(key=lambda m: m.volume_24h, reverse=True)

        logger.info(
            "Fetched markets by category",
            category=category,
            total=len(markets),
            tradeable=sum(1 for m in markets if m.is_tradeable()),
        )
        return markets

    async def get_all_open_markets(
        self,
        limit: int = 200,
        min_volume_24h: int = 100,
    ) -> list[MarketSummary]:
        """
        Fetch all open markets across all categories.

        Paginates automatically until `limit` is reached or no more pages.
        Pre-filters by min_volume_24h to avoid illiquid noise.
        """
        markets: list[MarketSummary] = []
        cursor: str | None = None

        while len(markets) < limit:
            params: dict[str, Any] = {
                "status": "open",
                "limit":  min(200, limit - len(markets)),
            }
            if cursor:
                params["cursor"] = cursor

            data   = await self._get("/markets", params=params)
            batch  = data.get("markets", [])
            cursor = data.get("cursor")

            for raw in batch:
                m = self._parse_market(raw)
                if m.volume_24h >= min_volume_24h:
                    markets.append(m)

            if not cursor or not batch:
                break

        markets.sort(key=lambda m: m.volume_24h, reverse=True)
        logger.info("Fetched all open markets", count=len(markets))
        return markets

    async def get_market(self, ticker: str) -> MarketSummary | None:
        """Fetch a single market by ticker. Returns None if not found."""
        try:
            data = await self._get(f"/markets/{ticker}")
            raw  = data.get("market", data)
            return self._parse_market(raw)
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                logger.warning("Market not found", ticker=ticker)
                return None
            raise

    async def get_event_markets(self, event_ticker: str) -> list[MarketSummary]:
        """
        Fetch all markets belonging to one event (e.g. one election event
        with a market per candidate).

        Useful for registering exhaustive sets in the ArbitrageStrategy.
        """
        data   = await self._get(f"/events/{event_ticker}")
        event  = data.get("event", {})
        raw    = event.get("markets", [])
        return [self._parse_market(m) for m in raw]

    # ── Order book ────────────────────────────────────────────────────────────

    async def get_order_book(self, ticker: str, depth: int = 10) -> OrderBookSnapshot | None:
        """
        Fetch the current order book snapshot for one ticker.

        Args:
            ticker: Market ticker.
            depth:  Number of price levels to return on each side (max 200).

        Returns:
            OrderBookSnapshot, or None if the market is not found.
        """
        try:
            data = await self._get(
                f"/markets/{ticker}/orderbook",
                params={"depth": depth},
            )
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return None
            raise

        book = data.get("orderbook", data)
        now  = datetime.now(timezone.utc)

        yes_bids_raw = book.get("yes", [])   # [[price, qty], ...]
        yes_asks_raw = book.get("no", [])    # Kalshi: "no" side = YES asks

        yes_bids = sorted(
            [(int(lvl[0]), int(lvl[1])) for lvl in yes_bids_raw],
            key=lambda x: x[0], reverse=True,
        )
        yes_asks = sorted(
            [(int(lvl[0]), int(lvl[1])) for lvl in yes_asks_raw],
            key=lambda x: x[0],
        )

        best_bid  = yes_bids[0][0] if yes_bids else None
        best_ask  = yes_asks[0][0] if yes_asks else None
        spread    = (best_ask - best_bid) if best_bid and best_ask else None
        mid       = (best_bid + best_ask) / 2.0 if best_bid and best_ask else None

        return OrderBookSnapshot(
            ticker=ticker,
            fetched_at=now,
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            mid_price=mid,
        )

    # ── Price history ─────────────────────────────────────────────────────────

    async def get_price_history(
        self,
        ticker: str,
        period_seconds: int = 3600,
    ) -> list[dict[str, Any]]:
        """
        Fetch candlestick price history for a ticker.

        Args:
            ticker:         Market ticker.
            period_seconds: Candle size in seconds (60, 300, 3600, 86400).

        Returns:
            List of candle dicts: {ts, open, high, low, close, volume}
        """
        data = await self._get(
            f"/markets/{ticker}/history",
            params={"period_seconds": period_seconds},
        )
        return data.get("history", [])

    # ── Bulk order book fetch ─────────────────────────────────────────────────

    async def get_order_books_bulk(
        self,
        tickers: list[str],
        concurrency: int = 5,
    ) -> dict[str, OrderBookSnapshot]:
        """
        Fetch order books for multiple tickers concurrently.

        Respects rate limiting via a semaphore capped at `concurrency`.
        Returns a dict of ticker -> OrderBookSnapshot for successful fetches.
        """
        sem = asyncio.Semaphore(concurrency)
        results: dict[str, OrderBookSnapshot] = {}

        async def _fetch_one(ticker: str) -> None:
            async with sem:
                book = await self.get_order_book(ticker)
                if book:
                    results[ticker] = book

        await asyncio.gather(*[_fetch_one(t) for t in tickers])
        logger.info(
            "Bulk order book fetch complete",
            requested=len(tickers),
            returned=len(results),
        )
        return results

    # ── Internal HTTP ─────────────────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Signed GET request with rate limiting."""
        full_path = f"/trade-api/v2{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            signing_path = f"{full_path}?{qs}"
        else:
            signing_path = full_path

        headers = self._creds.sign_request("GET", signing_path)

        async with self._limiter.throttle(BucketType.READ):
            assert self._session is not None, "MarketClient must be used as async context manager"
            resp = await self._session.get(
                f"{config.BASE_URL}{path}",
                params=params,
                headers=headers,
            )

            if resp.status == 429:
                await self._limiter.on_429(BucketType.READ)
                # Retry once after backoff
                resp = await self._session.get(
                    f"{config.BASE_URL}{path}",
                    params=params,
                    headers=self._creds.sign_request("GET", signing_path),
                )

            resp.raise_for_status()
            self._limiter.reset_backoff(BucketType.READ)
            return await resp.json()

    # ── Parsing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_market(raw: dict[str, Any]) -> MarketSummary:
        """Normalise a raw Kalshi market dict into a MarketSummary."""

        def _cents(val: Any) -> int | None:
            return int(val) if val is not None else None

        def _dt(val: str | None) -> datetime | None:
            if not val:
                return None
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None

        yes_bid = _cents(raw.get("yes_bid"))
        yes_ask = _cents(raw.get("yes_ask"))
        no_bid  = _cents(raw.get("no_bid"))
        no_ask  = _cents(raw.get("no_ask"))

        # Kalshi sometimes omits no_bid/no_ask — derive from YES complement
        if yes_ask is not None and no_bid is None:
            no_bid = 100 - yes_ask
        if yes_bid is not None and no_ask is None:
            no_ask = 100 - yes_bid

        return MarketSummary(
            ticker=raw.get("ticker", ""),
            event_ticker=raw.get("event_ticker", ""),
            title=raw.get("title", raw.get("subtitle", "")),
            category=raw.get("category", "Unknown"),
            series_ticker=raw.get("series_ticker", ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            last_price=_cents(raw.get("last_price")),
            volume=int(raw.get("volume", 0)),
            volume_24h=int(raw.get("volume_24h", 0)),
            open_interest=int(raw.get("open_interest", 0)),
            liquidity=int(raw.get("liquidity", 0)),
            status=raw.get("status", "unknown"),
            close_time=_dt(raw.get("close_time") or raw.get("expiration_time")),
            result=raw.get("result"),
        )
