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
  GET /search/tags_by_categories     → tags (subcategories) per category
  GET /search/filters_by_sport       → sports, leagues, and scopes
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
    updated_at:      datetime | None   # last metadata/trade update (API: updated_time)
    result:          str | None   # "yes" / "no" / None if unresolved

    # Derived screening fields (computed on construction)
    spread:          int          = field(init=False)   # yes_ask - yes_bid
    mid_price:       float        = field(init=False)   # (yes_bid + yes_ask) / 2
    implied_prob:    float        = field(init=False)   # mid / 100
    yes_decimal_odds: float       = field(init=False)   # 100 / yes_ask
    no_decimal_odds:  float       = field(init=False)   # 100 / no_ask
    minutes_to_close: float | None = field(init=False)
    minutes_since_update: float | None = field(init=False)

    def __post_init__(self) -> None:
        bid = self.yes_bid or 0
        ask = self.yes_ask or 100

        self.spread       = ask - bid if self.yes_ask and self.yes_bid else 100
        self.mid_price    = (bid + ask) / 2.0 if self.yes_bid and self.yes_ask else 50.0
        self.implied_prob = self.mid_price / 100.0

        self.yes_decimal_odds = round(100.0 / ask, 3) if ask > 0 else 0.0
        no_ask_val            = self.no_ask or (100 - bid)
        self.no_decimal_odds  = round(100.0 / no_ask_val, 3) if no_ask_val > 0 else 0.0

        now = datetime.now(timezone.utc)
        if self.close_time:
            self.minutes_to_close = (self.close_time - now).total_seconds() / 60.0
        else:
            self.minutes_to_close = None
        if self.updated_at:
            self.minutes_since_update = (now - self.updated_at).total_seconds() / 60.0
        else:
            self.minutes_since_update = None

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
        # Payout is always $1.00 per contract on Kalshi binary markets.
        # Cost to enter = ask price (cents / 100 dollars per contract).
        yes_cost_usd = round(self.yes_ask / 100, 2) if self.yes_ask else None
        no_cost_usd  = round(self.no_ask  / 100, 2) if self.no_ask  else None
        yes_roi_pct  = round((1.0 - self.yes_ask / 100) / (self.yes_ask / 100) * 100, 1) if self.yes_ask else None
        no_roi_pct   = round((1.0 - self.no_ask  / 100) / (self.no_ask  / 100) * 100, 1) if self.no_ask  else None

        return {
            # ── Identity ─────────────────────────────────────────────────────
            "ticker":              self.ticker,
            "event_ticker":        self.event_ticker,
            "title":               self.title,
            "category":            self.category,
            # ── Pricing ──────────────────────────────────────────────────────
            "yes_bid":             self.yes_bid,
            "yes_ask":             self.yes_ask,
            "no_bid":              self.no_bid,
            "no_ask":              self.no_ask,
            "spread":              self.spread,
            "last_price":          self.last_price,
            # ── Probability ───────────────────────────────────────────────────
            "mid_price":           self.mid_price,
            "implied_prob_pct":    round(self.implied_prob * 100, 1),  # e.g. 62.5
            "yes_decimal_odds":    self.yes_decimal_odds,              # e.g. 1.61
            "no_decimal_odds":     self.no_decimal_odds,               # e.g. 2.50
            # ── Payout (per 1 contract = $1.00 at resolution) ────────────────
            "payout_per_contract_usd": 1.00,                           # always $1
            "yes_cost_usd":        yes_cost_usd,   # what you pay to buy 1 YES
            "no_cost_usd":         no_cost_usd,    # what you pay to buy 1 NO
            "yes_profit_if_win_usd": round(1.0 - yes_cost_usd, 2) if yes_cost_usd else None,
            "no_profit_if_win_usd":  round(1.0 - no_cost_usd,  2) if no_cost_usd  else None,
            "yes_roi_pct":         yes_roi_pct,    # return on investment if YES wins
            "no_roi_pct":          no_roi_pct,     # return on investment if NO  wins
            # ── Liquidity ─────────────────────────────────────────────────────
            "volume_24h":          self.volume_24h,
            "open_interest":       self.open_interest,
            "liquidity":           self.liquidity,
            # ── Lifecycle ─────────────────────────────────────────────────────
            "status":              self.status,
            "close_time":          self.close_time.isoformat() if self.close_time else None,
            "minutes_to_close":    round(self.minutes_to_close, 1) if self.minutes_to_close else None,
            "updated_at":          self.updated_at.isoformat() if self.updated_at else None,
            "minutes_since_update": round(self.minutes_since_update, 1) if self.minutes_since_update is not None else None,
            "is_tradeable":        self.is_tradeable(),
        }


from discovery.orderbook_parse import OrderBookSnapshot, parse_orderbook_response

# Re-export for callers that import from market_client
__all__ = ["MarketClient", "MarketSummary", "OrderBookSnapshot"]


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
        # Discovery responses (events with nested markets) can be large and slow.
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=60.0,
                connect=10.0,
                sock_read=60.0,
            )
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

    async def get_tags_by_categories(self) -> dict[str, list[str]]:
        """
        Return Kalshi's category → tag (subcategory) mapping.

        Tags are used to filter series and events within a category, e.g.
        Sports → Soccer, Tennis, Hockey.
        """
        data = await self._get("/search/tags_by_categories")
        raw = data.get("tags_by_categories", {})
        return {
            cat: [t for t in (tags or []) if t]
            for cat, tags in raw.items()
            if tags
        }

    async def get_tags_for_category(self, category: str) -> list[str]:
        """Sorted tags (subcategories) for one category, case-insensitive."""
        mapping = await self.get_tags_by_categories()
        cat_lc = category.lower()
        for name, tags in mapping.items():
            if name.lower() == cat_lc:
                return sorted(tags)
        return []

    async def get_sports_filters(self) -> dict[str, Any]:
        """
        Sports-specific filters: sport → {competitions, scopes}.

        Competitions are leagues (e.g. EPL, Champions League); scopes are
        market types (e.g. Games, Futures).
        """
        data = await self._get("/search/filters_by_sport")
        return {
            "filters_by_sports": data.get("filters_by_sports", {}),
            "sport_ordering": data.get("sport_ordering", []),
        }

    async def get_series_for_category(
        self,
        category: str,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """Series in a category, optionally filtered by tag (subcategory)."""
        params: dict[str, Any] = {"category": category}
        if tag:
            params["tags"] = tag
        data = await self._get("/series", params=params)
        return data.get("series", [])

    # ── Market listing ────────────────────────────────────────────────────────

    async def get_markets_by_category(
        self,
        category: str,
        status: str = "open",
        limit: int = 100,
        min_volume_24h: int = 0,
        activity_hours: float | None = None,
        full_scan: bool = False,
        tag: str | None = None,
        sport: str | None = None,
        competition: str | None = None,
        scope: str | None = None,
    ) -> list[MarketSummary]:
        """
        Fetch open markets in a given category.

        Uses GET /events with nested markets: each event carries a
        category label and its child markets in one response. This avoids
        thousands of per-series /markets calls (which trip demo rate limits)
        and full-catalog /markets scans.

        Args:
            min_volume_24h: Minimum contracts traded in the last 24 hours.
            activity_hours: If set, only markets whose updated_time is within
                this many hours (proxy for recent activity; Kalshi does not
                expose a native 2h volume field on list endpoints).
            full_scan: Paginate through all open events before ranking, so
                high-volume markets are not missed due to early stopping.
            tag: Subcategory tag (e.g. Soccer, Tennis). Filters by series tags.
            sport: Alias for tag on Sports markets (same values as tags).
            competition: League/competition id on event product_metadata
                (e.g. EPL, Champions League). Use with Sports + sport/tag.
            scope: Competition scope (e.g. Games, Futures) from product_metadata.
        """
        category_lc = category.lower()
        tag_filter = (tag or sport or "").strip() or None
        competition_lc = competition.strip().lower() if competition else None
        scope_lc = self._normalize_scope(scope) if scope else None

        if tag_filter or competition_lc or scope_lc:
            full_scan = True

        series_tickers: set[str] | None = None
        if tag_filter:
            series_list = await self.get_series_for_category(category, tag=tag_filter)
            series_tickers = {s["ticker"] for s in series_list if s.get("ticker")}
            if not series_tickers:
                logger.warning(
                    "No series found for category tag",
                    category=category,
                    tag=tag_filter,
                )
                return []

        collected: list[MarketSummary] = []
        cursor: str | None = None
        pages = 0
        events_matched = 0
        target_pool = 0 if full_scan else (max(limit * 5, 400) if limit else 0)
        max_pages = 500 if full_scan else 50

        while pages < max_pages:
            params: dict[str, Any] = {
                "status":              status,
                "limit":               200,
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor

            data   = await self._get("/events", params=params)
            batch  = data.get("events", [])
            cursor = data.get("cursor")
            pages += 1

            for event in batch:
                if (event.get("category") or "").lower() != category_lc:
                    continue
                if series_tickers is not None:
                    if event.get("series_ticker", "") not in series_tickers:
                        continue
                if not self._event_matches_competition_filters(
                    event, competition_lc, scope_lc
                ):
                    continue
                events_matched += 1
                series_tk = event.get("series_ticker", "")
                for raw in event.get("markets", []):
                    if status and not self._market_matches_status(raw.get("status"), status):
                        continue
                    if not raw.get("series_ticker"):
                        raw["series_ticker"] = series_tk
                    raw["_category_override"] = category
                    m = self._parse_market(raw)
                    if m.volume_24h < min_volume_24h:
                        continue
                    if activity_hours is not None and not self._matches_activity_hours(m, activity_hours):
                        continue
                    collected.append(m)

            if target_pool and limit and len(collected) >= target_pool:
                break
            if not cursor or not batch:
                break

        if not collected and events_matched == 0:
            logger.warning("No events found for category", category=category)
            return []

        collected.sort(key=lambda m: m.volume_24h, reverse=True)
        result = collected[:limit] if limit else collected

        logger.info(
            "Fetched markets by category",
            category=category,
            tag=tag_filter,
            competition=competition,
            scope=scope,
            pages_scanned=pages,
            events_matched=events_matched,
            matched=len(collected),
            total=len(result),
            tradeable=sum(1 for m in result if m.is_tradeable()),
        )
        return result
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
        # Build series_ticker -> category lookup first (category removed from /markets)
        series_data    = await self._get("/series")
        series_cat_map = {
            s["ticker"]: s.get("category", "Unknown")
            for s in series_data.get("series", [])
            if s.get("ticker")
        }

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
                # Inject category from series lookup
                series_tk = raw.get("series_ticker", "")
                raw["_category_override"] = series_cat_map.get(series_tk, "Unknown")
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

        return parse_orderbook_response(data, ticker)

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
        *,
        max_429_retries: int = 8,
    ) -> dict[str, Any]:
        """Signed GET request with rate limiting and 429 retries."""
        full_path = f"/trade-api/v2{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            signing_path = f"{full_path}?{qs}"
        else:
            signing_path = full_path

        assert self._session is not None, "MarketClient must be used as async context manager"

        for attempt in range(max_429_retries + 1):
            async with self._limiter.throttle(BucketType.READ):
                resp = await self._session.get(
                    f"{config.BASE_URL}{path}",
                    params=params,
                    headers=self._creds.sign_request("GET", signing_path),
                )

            if resp.status == 429:
                if attempt >= max_429_retries:
                    resp.raise_for_status()
                await self._limiter.on_429(BucketType.READ)
                continue

            resp.raise_for_status()
            self._limiter.reset_backoff(BucketType.READ)
            return await resp.json()

        raise RuntimeError("unreachable")  # pragma: no cover

    # ── Parsing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _event_matches_competition_filters(
        event: dict[str, Any],
        competition_lc: str | None,
        scope_lc: str | None,
    ) -> bool:
        """Filter Sports events by league (competition) and scope in product_metadata."""
        if not competition_lc and not scope_lc:
            return True
        meta = event.get("product_metadata") or {}
        if competition_lc:
            comp = (meta.get("competition") or "").lower()
            if comp != competition_lc:
                return False
        if scope_lc:
            want = MarketClient._normalize_scope(scope_lc)
            ev_scope = MarketClient._normalize_scope(meta.get("competition_scope") or "")
            if ev_scope != want:
                return False
        return True

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        """Normalize scope for comparison (Games ↔ Game, case-insensitive)."""
        s = scope.strip().lower()
        if s.endswith("s") and len(s) > 3:
            return s[:-1]
        return s

    @staticmethod
    def _matches_activity_hours(market: MarketSummary, activity_hours: float) -> bool:
        """True if the market was updated within the last activity_hours."""
        if market.updated_at is None:
            return False
        age_hours = (datetime.now(timezone.utc) - market.updated_at).total_seconds() / 3600.0
        return age_hours <= activity_hours

    @staticmethod
    def _market_matches_status(market_status: str | None, requested: str) -> bool:
        """Kalshi uses 'active' for tradeable markets; callers pass status='open'."""
        m = (market_status or "").lower()
        r = requested.lower()
        if r == "open":
            return m in ("open", "active")
        return m == r

    @staticmethod
    def _parse_market(raw: dict[str, Any]) -> MarketSummary:
        """Normalise a raw Kalshi market dict into a MarketSummary."""

        def _cents(val: Any) -> int | None:
            if val is None:
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        def _dollars_to_cents(val: Any) -> int | None:
            if val is None or val == "":
                return None
            try:
                return int(round(float(val) * 100))
            except (TypeError, ValueError):
                return None

        def _price_cents(cent_key: str, dollar_key: str) -> int | None:
            return _cents(raw.get(cent_key)) or _dollars_to_cents(raw.get(dollar_key))

        def _metric_int(*keys: str) -> int:
            for key in keys:
                val = raw.get(key)
                if val is None or val == "":
                    continue
                try:
                    return int(float(val))
                except (TypeError, ValueError):
                    continue
            return 0

        def _dt(val: str | None) -> datetime | None:
            if not val:
                return None
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None

        yes_bid = _price_cents("yes_bid", "yes_bid_dollars")
        yes_ask = _price_cents("yes_ask", "yes_ask_dollars")
        no_bid  = _price_cents("no_bid", "no_bid_dollars")
        no_ask  = _price_cents("no_ask", "no_ask_dollars")

        # Kalshi sometimes omits no_bid/no_ask — derive from YES complement
        if yes_ask is not None and no_bid is None:
            no_bid = 100 - yes_ask
        if yes_bid is not None and no_ask is None:
            no_ask = 100 - yes_bid

        market_status = raw.get("status", "unknown")
        if market_status == "active":
            market_status = "open"

        return MarketSummary(
            ticker=raw.get("ticker", ""),
            event_ticker=raw.get("event_ticker", ""),
            title=raw.get("title", raw.get("subtitle", "")),
            category=raw.get("_category_override") or raw.get("category", "Unknown"),
            series_ticker=raw.get("series_ticker", ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            last_price=_price_cents("last_price", "last_price_dollars"),
            volume=_metric_int("volume", "volume_fp"),
            volume_24h=_metric_int("volume_24h", "volume_24h_fp"),
            open_interest=_metric_int("open_interest", "open_interest_fp"),
            liquidity=_metric_int("liquidity", "liquidity_dollars"),
            status=market_status,
            close_time=_dt(raw.get("close_time") or raw.get("expiration_time")),
            updated_at=_dt(raw.get("updated_time")),
            result=raw.get("result"),
        )
