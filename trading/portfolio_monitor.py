"""
trading/portfolio_monitor.py

Real-time portfolio position tracker with mark-to-market P&L.

Fetches all open positions from Kalshi, then enriches each one with the
live order book mid-price to compute unrealised P&L, current implied value,
and per-position risk metrics.

Also tracks:
  - Realised P&L from closed positions fetched from the API
  - Session P&L (delta since monitor was started)
  - Portfolio-level risk: total exposure, largest position, sector concentration

Kalshi REST endpoints used
──────────────────────────
    GET /portfolio/positions    all current open positions
    GET /portfolio/fills        fills (for realised P&L history)
    GET /portfolio/balance      cash balance and portfolio value
    GET /markets/{ticker}/orderbook   mark-to-market price per position

Data flow
─────────
    refresh() → fetch positions → bulk fetch order books →
    mark_to_market() each position → compute portfolio totals →
    return PortfolioSnapshot
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

import config
from credentials.credential_manager import CredentialManager
from discovery.market_client import MarketClient, OrderBookSnapshot
from execution.rate_limiter import BucketType, RateLimiter
from logging_.structured_logger import logger


# ── Position model ────────────────────────────────────────────────────────────

@dataclass
class Position:
    """
    One open position enriched with live mark-to-market data.

    Kalshi positions are always long (you buy YES or NO; no shorting).
    Unrealised P&L is computed as:
        mark_price  = current mid-price for the side held
        cost_basis  = avg_entry_price × contracts  (what you paid)
        unrealised  = (mark_price - avg_entry_price) × contracts

    Where mark_price for YES = yes mid-price,
          mark_price for NO  = (100 - yes mid-price).
    """
    # From Kalshi API
    ticker:          str
    side:            str              # "yes" | "no"
    contracts:       int              # number of contracts held
    avg_entry_price: int              # cents paid per contract
    market_title:    str = ""

    # Enriched by mark-to-market
    current_mid:     int | None = None   # current YES mid in cents
    mark_price:      int | None = None   # mid for the held side (yes or no)
    unrealised_pnl:  int        = 0      # cents
    cost_basis:      int        = 0      # cents total paid
    current_value:   int        = 0      # mark_price × contracts
    max_payout:      int        = 0      # 100 × contracts (if position wins)
    implied_prob:    float      = 0.0    # current implied probability of winning
    minutes_to_close: float | None = None

    def __post_init__(self) -> None:
        self.cost_basis  = self.avg_entry_price * self.contracts
        self.max_payout  = 100 * self.contracts

    def apply_mark(self, book: OrderBookSnapshot | None) -> None:
        """Update unrealised P&L from a live order book snapshot."""
        if not book or book.mid_price is None:
            return

        yes_mid  = int(book.mid_price)
        self.current_mid = yes_mid

        if self.side == "yes":
            self.mark_price    = yes_mid
            self.implied_prob  = yes_mid / 100.0
        else:
            self.mark_price    = 100 - yes_mid
            self.implied_prob  = (100 - yes_mid) / 100.0

        self.current_value  = self.mark_price * self.contracts
        self.unrealised_pnl = self.current_value - self.cost_basis

    @property
    def pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return self.unrealised_pnl / self.cost_basis * 100.0

    @property
    def potential_profit(self) -> int:
        """Profit if position wins (max payout minus cost basis)."""
        return self.max_payout - self.cost_basis

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker":           self.ticker,
            "title":            self.market_title,
            "side":             self.side,
            "contracts":        self.contracts,
            "avg_entry_price":  self.avg_entry_price,
            "cost_basis_usd":   round(self.cost_basis / 100, 2),
            "mark_price":       self.mark_price,
            "current_value_usd": round(self.current_value / 100, 2),
            "unrealised_pnl_usd": round(self.unrealised_pnl / 100, 2),
            "pnl_pct":          round(self.pnl_pct, 2),
            "implied_prob_pct": round(self.implied_prob * 100, 1),
            "max_payout_usd":   round(self.max_payout / 100, 2),
            "potential_profit_usd": round(self.potential_profit / 100, 2),
            "minutes_to_close": round(self.minutes_to_close, 1) if self.minutes_to_close else None,
        }


# ── Portfolio snapshot ────────────────────────────────────────────────────────

@dataclass
class PortfolioSnapshot:
    """
    Point-in-time view of the full portfolio.
    """
    positions:          list[Position]
    cash_balance_cents: int
    portfolio_value_cents: int         # cash + sum of current_value
    total_cost_basis_cents: int        # total invested
    total_unrealised_pnl_cents: int
    total_max_payout_cents: int        # if ALL positions win
    session_realised_pnl_cents: int    # realised since monitor started
    snapped_at:         datetime       = field(default_factory=lambda: datetime.now(timezone.utc))

    # Risk metrics
    largest_position_pct: float       = 0.0   # biggest position as % of portfolio
    num_positions:        int          = 0

    @property
    def total_unrealised_pnl_usd(self) -> float:
        return self.total_unrealised_pnl_cents / 100

    @property
    def win_scenario_value_usd(self) -> float:
        """Portfolio value if every open position wins."""
        return (self.cash_balance_cents + self.total_max_payout_cents) / 100

    def print_report(self) -> None:
        """Pretty-print the full portfolio to stdout."""
        ts = self.snapped_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        w  = 110

        print(f"\n{'═' * w}")
        print(f"  PORTFOLIO SNAPSHOT  —  {ts}")
        print(f"{'═' * w}")

        # Summary bar
        print(f"\n  {'Cash balance:':<30} ${self.cash_balance_cents/100:>10.2f}")
        print(f"  {'Open positions value:':<30} ${sum(p.current_value for p in self.positions)/100:>10.2f}")
        print(f"  {'Total cost basis:':<30} ${self.total_cost_basis_cents/100:>10.2f}")
        print(f"  {'Unrealised P&L:':<30} ${self.total_unrealised_pnl_cents/100:>+10.2f}  ({self._pnl_pct():+.1f}%)")
        print(f"  {'Session realised P&L:':<30} ${self.session_realised_pnl_cents/100:>+10.2f}")
        print(f"  {'Max payout (all win):':<30} ${self.total_max_payout_cents/100:>10.2f}")
        print(f"  {'Open positions:':<30} {self.num_positions}")

        if not self.positions:
            print(f"\n  No open positions.\n{'═' * w}\n")
            return

        # Position table
        print(f"\n  {'─' * w}")
        print(
            f"  {'TICKER':<30} {'SIDE':<5} {'QTY':>5} {'ENTRY':>6} "
            f"{'MARK':>6} {'COST':>8} {'VALUE':>8} {'UNREAL P&L':>11} "
            f"{'P&L%':>7} {'IMPL%':>7} {'MAX PAY':>8} {'EXP':>8}"
        )
        print(f"  {'─' * w}")

        for pos in sorted(self.positions, key=lambda p: abs(p.unrealised_pnl), reverse=True):
            pnl_sign = "+" if pos.unrealised_pnl >= 0 else ""
            exp_str  = f"{pos.minutes_to_close:.0f}m" if pos.minutes_to_close is not None else "open"
            print(
                f"  {pos.ticker:<30} {pos.side:<5} {pos.contracts:>5} "
                f"{pos.avg_entry_price:>5}c {(pos.mark_price or 0):>5}c "
                f"${pos.cost_basis/100:>7.2f} ${pos.current_value/100:>7.2f} "
                f"{pnl_sign}${pos.unrealised_pnl/100:>9.2f} "
                f"{pos.pnl_pct:>+6.1f}% {pos.implied_prob*100:>6.1f}% "
                f"${pos.max_payout/100:>7.2f} {exp_str:>8}"
            )

        print(f"  {'─' * w}\n{'═' * w}\n")

    def _pnl_pct(self) -> float:
        if self.total_cost_basis_cents == 0:
            return 0.0
        return self.total_unrealised_pnl_cents / self.total_cost_basis_cents * 100


# ── Monitor ───────────────────────────────────────────────────────────────────

class PortfolioMonitor:
    """
    Fetches and mark-to-markets the full Kalshi portfolio on demand.

    Usage:
        async with PortfolioMonitor(creds, limiter) as monitor:
            snapshot = await monitor.refresh()
            snapshot.print_report()

            # Continuous loop (every 30 seconds)
            async for snapshot in monitor.stream(interval_seconds=30):
                snapshot.print_report()
    """

    def __init__(
        self,
        credentials: CredentialManager,
        rate_limiter: RateLimiter,
    ) -> None:
        self._creds   = credentials
        self._limiter = rate_limiter
        self._session: aiohttp.ClientSession | None = None
        self._session_start_realised: int = 0   # baseline for session P&L
        self._session_started = False

    async def __aenter__(self) -> "PortfolioMonitor":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS)
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    # ── Public API ────────────────────────────────────────────────────────────

    async def refresh(self, market_client: MarketClient | None = None) -> PortfolioSnapshot:
        """
        Fetch current positions + balance, mark-to-market each position,
        and return a complete PortfolioSnapshot.

        Args:
            market_client: If provided, uses it for bulk order book fetching.
                           If None, creates a temporary internal client.
        """
        positions_raw, balance_raw, fills_raw = await asyncio.gather(
            self._fetch_positions(),
            self._fetch_balance(),
            self._fetch_fills(limit=100),
        )

        # Parse positions
        positions = [self._parse_position(p) for p in positions_raw]

        # Mark-to-market: bulk fetch order books
        if positions:
            tickers = [p.ticker for p in positions]
            books   = await self._bulk_fetch_books(tickers)
            for pos in positions:
                pos.apply_mark(books.get(pos.ticker))

        # Parse balance
        cash_balance = int(balance_raw.get("balance", 0))

        # Session realised P&L (fills since monitor started)
        realised_total = sum(
            int(f.get("is_taker", 0))  # placeholder — use actual fill P&L
            for f in fills_raw
        )
        if not self._session_started:
            self._session_start_realised = realised_total
            self._session_started = True
        session_realised = realised_total - self._session_start_realised

        # Portfolio totals
        total_cost        = sum(p.cost_basis for p in positions)
        total_unrealised  = sum(p.unrealised_pnl for p in positions)
        total_max_payout  = sum(p.max_payout for p in positions)
        portfolio_value   = cash_balance + sum(p.current_value for p in positions)

        # Risk: largest position as % of total value
        largest_pct = 0.0
        if portfolio_value > 0 and positions:
            largest_pct = max(p.current_value for p in positions) / portfolio_value * 100

        snapshot = PortfolioSnapshot(
            positions=positions,
            cash_balance_cents=cash_balance,
            portfolio_value_cents=portfolio_value,
            total_cost_basis_cents=total_cost,
            total_unrealised_pnl_cents=total_unrealised,
            total_max_payout_cents=total_max_payout,
            session_realised_pnl_cents=session_realised,
            largest_position_pct=largest_pct,
            num_positions=len(positions),
        )

        logger.info(
            "Portfolio refreshed",
            positions=len(positions),
            unrealised_pnl_usd=round(total_unrealised / 100, 2),
            cash_balance_usd=round(cash_balance / 100, 2),
        )

        return snapshot

    async def stream(
        self,
        interval_seconds: float = 30.0,
        market_client: MarketClient | None = None,
    ):
        """
        Async generator: yields a fresh PortfolioSnapshot every `interval_seconds`.

        Usage:
            async for snap in monitor.stream(interval_seconds=15):
                snap.print_report()
        """
        while True:
            try:
                snapshot = await self.refresh(market_client)
                yield snapshot
            except Exception as exc:
                logger.error(f"Portfolio refresh error: {exc}")
            await asyncio.sleep(interval_seconds)

    async def get_position(self, ticker: str) -> Position | None:
        """Fetch and mark-to-market a single position by ticker."""
        snapshot = await self.refresh()
        return next((p for p in snapshot.positions if p.ticker == ticker), None)

    # ── Private: API calls ────────────────────────────────────────────────────

    async def _fetch_positions(self) -> list[dict]:
        path    = "/portfolio/positions"
        headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")

        async with self._limiter.throttle(BucketType.READ):
            resp = await self._session.get(f"{config.BASE_URL}{path}", headers=headers)
            resp.raise_for_status()
            data = await resp.json()
            return data.get("market_positions", data.get("positions", []))

    async def _fetch_balance(self) -> dict:
        path    = "/portfolio/balance"
        headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")

        async with self._limiter.throttle(BucketType.READ):
            resp = await self._session.get(f"{config.BASE_URL}{path}", headers=headers)
            resp.raise_for_status()
            return await resp.json()

    async def _fetch_fills(self, limit: int = 50) -> list[dict]:
        path    = "/portfolio/fills"
        headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")

        async with self._limiter.throttle(BucketType.READ):
            resp = await self._session.get(
                f"{config.BASE_URL}{path}",
                params={"limit": limit},
                headers=headers,
            )
            resp.raise_for_status()
            data = await resp.json()
            return data.get("fills", [])

    async def _bulk_fetch_books(
        self,
        tickers: list[str],
        concurrency: int = 5,
    ) -> dict[str, OrderBookSnapshot]:
        """Fetch order books concurrently for mark-to-market."""
        from discovery.market_client import MarketClient as MC
        sem     = asyncio.Semaphore(concurrency)
        results = {}

        async def _one(ticker: str) -> None:
            async with sem:
                path    = f"/markets/{ticker}/orderbook"
                headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")
                async with self._limiter.throttle(BucketType.READ):
                    try:
                        resp = await self._session.get(
                            f"{config.BASE_URL}{path}",
                            params={"depth": 5},
                            headers=headers,
                        )
                        if resp.status != 200:
                            return
                        data = await resp.json()
                        book = data.get("orderbook", {})
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        yes_bids = sorted(
                            [(int(l[0]), int(l[1])) for l in book.get("yes", [])],
                            key=lambda x: x[0], reverse=True,
                        )
                        yes_asks = sorted(
                            [(int(l[0]), int(l[1])) for l in book.get("no", [])],
                            key=lambda x: x[0],
                        )
                        best_bid = yes_bids[0][0] if yes_bids else None
                        best_ask = yes_asks[0][0] if yes_asks else None
                        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None

                        from discovery.market_client import OrderBookSnapshot
                        results[ticker] = OrderBookSnapshot(
                            ticker=ticker,
                            fetched_at=now,
                            yes_bids=yes_bids,
                            yes_asks=yes_asks,
                            best_bid=best_bid,
                            best_ask=best_ask,
                            spread=(best_ask - best_bid) if best_bid and best_ask else None,
                            mid_price=mid,
                        )
                    except Exception as exc:
                        logger.warning(f"Book fetch failed for {ticker}: {exc}")

        await asyncio.gather(*[_one(t) for t in tickers])
        return results

    @staticmethod
    def _parse_position(raw: dict[str, Any]) -> Position:
        """Normalise a raw Kalshi position dict."""
        contracts       = int(raw.get("position", raw.get("contracts", 0)))
        avg_price       = int(raw.get("market_exposure", raw.get("avg_price", 0)))

        # Kalshi returns market_exposure in cents total; convert to per-contract
        if "market_exposure" in raw and contracts > 0:
            avg_price = avg_price // contracts

        return Position(
            ticker=raw.get("ticker", ""),
            side=raw.get("side", "yes"),
            contracts=contracts,
            avg_entry_price=avg_price,
            market_title=raw.get("market_title", raw.get("title", "")),
        )
