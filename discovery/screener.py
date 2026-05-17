"""
discovery/screener.py

Market screener — scores every open market against each strategy's
ideal conditions and returns a ranked shortlist of the best opportunities.

────────────────────────────────────────────────────────────────────────────────
HOW SCORING WORKS
────────────────────────────────────────────────────────────────────────────────

Each strategy has a dedicated scorer that returns a float in [0.0, 1.0].
Higher = better fit. Zero means "do not trade this market with this strategy."

  KellyScorer      — rewards wide edge, low spread, high liquidity,
                     and a price that implies the model has a real edge.

  GreenUpScorer    — rewards low current YES price (underdog), high 24h
                     volume (market is active enough to move), and time
                     remaining (need the event to actually develop).

  ArbitrageScorer  — rewards markets within the same event group where
                     YES prices sum to less than 100 (complementary arb)
                     or where a dominance chain is violated.

────────────────────────────────────────────────────────────────────────────────
OUTPUT
────────────────────────────────────────────────────────────────────────────────

  ScreenerResult — one per (market, strategy) pair that scores above threshold.
  The screener also detects and flags arbitrage groups directly.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

    async with MarketClient(creds, limiter) as client:
        screener = MarketScreener(client)

        # Score every open Politics market
        results = await screener.screen_category("Politics")

        # Print ranked table
        screener.print_report(results)

        # Get just the tickers ready to trade
        tickers = [r.market.ticker for r in results if r.score > 0.5]
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import config
from discovery.market_client import MarketClient, MarketSummary, OrderBookSnapshot
from discovery.market_math import (
    fee_adjusted_roi_if_yes_wins_pct,
    gross_roi_if_yes_wins_pct,
    passes_roi_gate,
)
from logging_.structured_logger import logger


# ── Screener tunables ─────────────────────────────────────────────────────────

MIN_SCORE_THRESHOLD: float    = 0.30   # drop results below this score
SCREENER_MIN_VOLUME_24H: int  = 200    # hard floor — all strategy scorers

# Strategy names accepted by score_for_strategy() / discovery rank_by=screener
SCREENER_STRATEGY_ALIASES: dict[str, str] = {
    "green_up":  "green_up",
    "greenup":   "green_up",
    "kelly":     "kelly",
    "high_prob": "high_prob",
    "highprob":  "high_prob",
    "arb":       "arb",
    "arbitrage": "arb",
}
MAX_SPREAD_CENTS: int         = 10     # reject illiquid markets
MIN_VOLUME_24H: int           = 50     # minimum daily contract volume
MIN_MINUTES_TO_CLOSE: float   = 30.0  # skip markets closing very soon

# Green-up entry window (cents)
GREEN_UP_MAX_ENTRY_PRICE: int   = 35   # back YES below this (high decimal odds)
GREEN_UP_MIN_ENTRY_PRICE: int   = 10   # skip if too cheap (near-zero liquidity)

# High-probability entry window (cents)
HP_MIN_YES_ASK: int = 85
HP_MAX_YES_ASK: int = 97
HP_MIN_ROI_PCT: float = getattr(config, "HP_MIN_ROI_PCT", 2.0)

# Arbitrage detection
ARB_SUM_THRESHOLD: int = 98    # flag if sum of YES asks in event < this


# ── Result types ──────────────────────────────────────────────────────────────

class StrategyFit(str, Enum):
    KELLY      = "kelly"
    GREEN_UP   = "green_up"
    HIGH_PROB  = "high_prob"
    ARB_COMP   = "arb_complementary"
    ARB_SET    = "arb_exhaustive_set"
    ARB_DOM    = "arb_dominance"


@dataclass
class ScreenerResult:
    """One ranked result for a single (market, strategy) pair."""
    market:       MarketSummary
    strategy_fit: StrategyFit
    score:        float             # 0.0 – 1.0; higher = better
    reasons:      list[str]         # human-readable scoring factors
    order_book:   OrderBookSnapshot | None = None
    arb_group:    list[str] | None  = None  # peer tickers if arb opportunity
    arb_profit_cents: int           = 0     # estimated arb profit per contract

    def summary_line(self) -> str:
        stars = "★" * round(self.score * 5)
        return (
            f"[{self.strategy_fit.value:<18}]  "
            f"{self.score:.2f} {stars:<5}  "
            f"{self.market.ticker:<35}  "
            f"bid={self.market.yes_bid or '?':>3}  "
            f"ask={self.market.yes_ask or '?':>3}  "
            f"spread={self.market.spread:>3}c  "
            f"vol24h={self.market.volume_24h:>6}  "
            f"{self.market.title[:50]}"
        )


@dataclass
class ArbitrageGroup:
    """A detected multi-market arbitrage opportunity."""
    arb_type:        StrategyFit
    tickers:         list[str]
    profit_cents:    int            # guaranteed profit per contract unit
    details:         str            # human-readable description


# ── Screener ──────────────────────────────────────────────────────────────────

class MarketScreener:
    """
    Fetches, filters, and scores open markets to find the best candidates
    for each trading strategy.
    """

    def __init__(self, client: MarketClient) -> None:
        self._client = client

    # ── Public API ────────────────────────────────────────────────────────────

    async def screen_category(
        self,
        category: str,
        fetch_order_books: bool = True,
        limit: int = 100,
        tag: str | None = None,
        sport: str | None = None,
        competition: str | None = None,
        scope: str | None = None,
    ) -> list[ScreenerResult]:
        """
        Screen all open markets in one category.

        Args:
            category:          e.g. "Politics", "Economics", "Sports"
            fetch_order_books: If True, enriches results with live book data.
            limit:             Max markets to consider.

        Returns:
            Ranked list of ScreenerResult, sorted by score descending.
        """
        markets = await self._client.get_markets_by_category(
            category=category,
            status="open",
            limit=limit,
            min_volume_24h=MIN_VOLUME_24H,
            tag=tag,
            sport=sport,
            competition=competition,
            scope=scope,
        )
        return await self._score_markets(markets, fetch_order_books)

    async def screen_all(
        self,
        fetch_order_books: bool = True,
        min_volume_24h: int = MIN_VOLUME_24H,
    ) -> list[ScreenerResult]:
        """
        Screen all open markets across all categories.

        This is the broadest scan — useful for finding arb opportunities
        across unrelated event groups.
        """
        markets = await self._client.get_all_open_markets(
            min_volume_24h=min_volume_24h,
        )
        return await self._score_markets(markets, fetch_order_books)

    async def screen_event(self, event_ticker: str) -> list[ScreenerResult]:
        """
        Screen all markets within a single event (e.g. one election).

        Also runs the exhaustive-set arbitrage detector across all markets
        in the event, since they share a common underlying.
        """
        markets = await self._client.get_event_markets(event_ticker)
        results = await self._score_markets(markets, fetch_order_books=True)

        # Add exhaustive-set arb check across all markets in the event
        arb = self._detect_exhaustive_arb(markets)
        if arb:
            for m in markets:
                results.append(ScreenerResult(
                    market=m,
                    strategy_fit=StrategyFit.ARB_SET,
                    score=0.90,
                    reasons=[
                        f"Exhaustive set arb: YES asks sum to {arb.profit_cents + 100}c",
                        f"Guaranteed profit: {arb.profit_cents}c per contract",
                    ],
                    arb_group=arb.tickers,
                    arb_profit_cents=arb.profit_cents,
                ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def get_market_detail(self, ticker: str) -> dict[str, Any]:
        """
        Full detail view for a single ticker: market summary + live order book
        + price history. Use before manually selecting a market to trade.
        """
        market = await self._client.get_market(ticker)
        book   = await self._client.get_order_book(ticker)
        history = await self._client.get_price_history(ticker, period_seconds=3600)

        if not market:
            return {"error": f"Market {ticker} not found"}

        return {
            "market":        market.to_dict(),
            "order_book": {
                "best_bid":  book.best_bid if book else None,
                "best_ask":  book.best_ask if book else None,
                "spread":    book.spread   if book else None,
                "mid_price": book.mid_price if book else None,
                "yes_bids":  (book.yes_bids[:5] if book else []),
                "yes_asks":  (book.yes_asks[:5] if book else []),
            } if book else {},
            "price_history_1h": history[-20:],   # last 20 candles
            "strategy_hints":   self._strategy_hints(market, book),
        }

    def print_report(self, results: list[ScreenerResult], top_n: int = 20) -> None:
        """Print a formatted screener report to stdout."""
        header = (
            f"\n{'─' * 130}\n"
            f"  KALSHI MARKET SCREENER  —  {len(results)} results  "
            f"(showing top {min(top_n, len(results))})\n"
            f"{'─' * 130}"
        )
        print(header)
        print(
            f"  {'STRATEGY':<20}  {'SCORE':>5}  "
            f"{'TICKER':<35}  {'BID':>3}  {'ASK':>3}  "
            f"{'SPREAD':>7}  {'VOL24H':>8}  TITLE"
        )
        print(f"{'─' * 130}")
        for r in results[:top_n]:
            print(f"  {r.summary_line()}")
        print(f"{'─' * 130}\n")

    # ── Scoring pipeline ──────────────────────────────────────────────────────

    async def _score_markets(
        self,
        markets: list[MarketSummary],
        fetch_order_books: bool,
    ) -> list[ScreenerResult]:
        """
        Run all scorers over the market list, optionally enriching with books.
        """
        # Filter out non-tradeable markets upfront
        tradeable = [m for m in markets if m.is_tradeable()]
        logger.info(
            "Screening markets",
            total=len(markets),
            tradeable=len(tradeable),
        )

        # Optionally bulk-fetch order books (5 concurrent requests)
        books: dict[str, OrderBookSnapshot] = {}
        if fetch_order_books and tradeable:
            books = await self._client.get_order_books_bulk(
                tickers=[m.ticker for m in tradeable],
                concurrency=5,
            )

        results: list[ScreenerResult] = []

        for market in tradeable:
            book = books.get(market.ticker)

            # Run all strategy scorers
            for scorer_fn, strategy_fit in [
                (self._score_kelly,     StrategyFit.KELLY),
                (self._score_green_up,  StrategyFit.GREEN_UP),
                (self._score_high_prob, StrategyFit.HIGH_PROB),
                (self._score_arb_comp,  StrategyFit.ARB_COMP),
            ]:
                score, reasons = scorer_fn(market, book)
                if score >= MIN_SCORE_THRESHOLD:
                    results.append(ScreenerResult(
                        market=market,
                        strategy_fit=strategy_fit,
                        score=score,
                        reasons=reasons,
                        order_book=book,
                    ))

        # Detect dominance-chain arb across all markets
        dom_arbs = self._detect_dominance_arb(tradeable)
        for arb in dom_arbs:
            for ticker in arb.tickers:
                m = next((x for x in tradeable if x.ticker == ticker), None)
                if m:
                    results.append(ScreenerResult(
                        market=m,
                        strategy_fit=StrategyFit.ARB_DOM,
                        score=min(0.95, 0.50 + arb.profit_cents / 20),
                        reasons=[arb.details],
                        arb_group=arb.tickers,
                        arb_profit_cents=arb.profit_cents,
                    ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # ── Individual scorers ────────────────────────────────────────────────────

    @staticmethod
    def _volume_gate(
        market: MarketSummary,
        min_volume: int = SCREENER_MIN_VOLUME_24H,
    ) -> tuple[float, list[str]] | None:
        """Return (0, reasons) when 24h volume is below the liquidity floor."""
        if market.volume_24h < min_volume:
            return 0.0, [
                f"Volume 24h {market.volume_24h:,} below screener minimum "
                f"{min_volume:,}"
            ]
        return None

    def _score_kelly(
        self, market: MarketSummary, book: OrderBookSnapshot | None
    ) -> tuple[float, list[str]]:
        """
        Score a market for Kelly strategy fit.

        Rewards:
          - Tight spread (low vig, easier to capture edge)
          - High 24h volume (liquidity, easier fills)
          - Price near 50 (maximum Kelly leverage zone)
          - High open interest (market is liquid and active)
        """
        reasons: list[str] = []
        score = 0.0

        vol_fail = self._volume_gate(market)
        if vol_fail:
            return vol_fail

        if market.spread is None:
            return 0.0, ["No spread data"]

        # Factor 1: spread tightness (0–0.35)
        if market.spread <= 2:
            spread_score = 0.35
            reasons.append(f"Tight spread: {market.spread}c (excellent)")
        elif market.spread <= 5:
            spread_score = 0.25
            reasons.append(f"Spread: {market.spread}c (good)")
        elif market.spread <= MAX_SPREAD_CENTS:
            spread_score = 0.10
            reasons.append(f"Spread: {market.spread}c (acceptable)")
        else:
            return 0.0, [f"Spread too wide: {market.spread}c"]
        score += spread_score

        # Factor 2: 24h volume (0–0.30)
        if market.volume_24h >= 10_000:
            vol_score = 0.30
            reasons.append(f"Volume 24h: {market.volume_24h:,} (very high)")
        elif market.volume_24h >= 1_000:
            vol_score = 0.20
            reasons.append(f"Volume 24h: {market.volume_24h:,} (high)")
        elif market.volume_24h >= SCREENER_MIN_VOLUME_24H:
            vol_score = 0.10
            reasons.append(f"Volume 24h: {market.volume_24h:,} (moderate)")
        else:
            vol_score = 0.0
        score += vol_score

        # Factor 3: price near 50 = maximum Kelly zone (0–0.20)
        dist_from_50 = abs(market.mid_price - 50.0)
        if dist_from_50 <= 10:
            price_score = 0.20
            reasons.append(f"Mid price {market.mid_price:.1f}c — near 50, high Kelly leverage")
        elif dist_from_50 <= 25:
            price_score = 0.12
            reasons.append(f"Mid price {market.mid_price:.1f}c — moderate Kelly zone")
        else:
            price_score = 0.04
            reasons.append(f"Mid price {market.mid_price:.1f}c — low Kelly leverage (extreme)")
        score += price_score

        # Factor 4: open interest (0–0.15)
        if market.open_interest >= 5_000:
            score += 0.15
            reasons.append(f"Open interest: {market.open_interest:,} (deep)")
        elif market.open_interest >= 500:
            score += 0.08
        score = min(score, 1.0)

        return round(score, 3), reasons

    def _score_green_up(
        self, market: MarketSummary, book: OrderBookSnapshot | None
    ) -> tuple[float, list[str]]:
        """
        Score a market for Green Up strategy fit.

        Rewards:
          - Low YES price (underdog; high decimal odds on entry)
          - High 24h volume (market must move for the strategy to work)
          - Long time remaining (need the event to develop)
          - Not too cheap (< 10c is near-zero probability, no movement expected)
        """
        reasons: list[str] = []
        score = 0.0

        vol_fail = self._volume_gate(market)
        if vol_fail:
            return vol_fail

        yes_ask = market.yes_ask
        if yes_ask is None:
            return 0.0, ["No ask price"]

        # Factor 1: price in the underdog entry window (0–0.40)
        if GREEN_UP_MIN_ENTRY_PRICE <= yes_ask <= GREEN_UP_MAX_ENTRY_PRICE:
            # Lower = higher odds = bigger potential green-up profit
            price_score = 0.40 * (1.0 - (yes_ask - GREEN_UP_MIN_ENTRY_PRICE) /
                                  (GREEN_UP_MAX_ENTRY_PRICE - GREEN_UP_MIN_ENTRY_PRICE))
            odds = round(100.0 / yes_ask, 2)
            reasons.append(
                f"YES ask {yes_ask}c ({odds}x odds) — in green-up entry window"
            )
        elif yes_ask < GREEN_UP_MIN_ENTRY_PRICE:
            return 0.0, [f"YES ask {yes_ask}c too cheap — near-zero probability"]
        else:
            return 0.0, [f"YES ask {yes_ask}c — not an underdog (above entry window)"]
        score += price_score

        # Factor 2: 24h volume — market must be active to move (0–0.35)
        if market.volume_24h >= 5_000:
            score += 0.35
            reasons.append(f"Volume 24h: {market.volume_24h:,} — active market")
        elif market.volume_24h >= 1_000:
            score += 0.25
            reasons.append(f"Volume 24h: {market.volume_24h:,} — reasonable activity")
        elif market.volume_24h >= SCREENER_MIN_VOLUME_24H:
            score += 0.10
            reasons.append(f"Volume 24h: {market.volume_24h:,} — meets floor")
        else:
            return 0.0, [f"Volume 24h {market.volume_24h} below green-up minimum"]

        # Factor 3: time to close — need time for the market to move (0–0.25)
        mins = market.minutes_to_close
        if mins is None:
            score += 0.15
            reasons.append("No expiry — long-dated market")
        elif mins >= 60 * 24:   # > 1 day
            score += 0.25
            reasons.append(f"Closes in {mins/60:.0f}h — ample time to green up")
        elif mins >= 60:        # > 1 hour
            score += 0.15
            reasons.append(f"Closes in {mins:.0f}m — enough time")
        elif mins >= MIN_MINUTES_TO_CLOSE:
            score += 0.05
            reasons.append(f"Closes in {mins:.0f}m — short window, higher risk")
        else:
            return 0.0, [f"Closing in {mins:.0f}m — too soon for green-up"]

        return round(min(score, 1.0), 3), reasons

    def _score_high_prob(
        self, market: MarketSummary, book: OrderBookSnapshot | None
    ) -> tuple[float, list[str]]:
        """
        Score a market for the high-probability strategy.

        Rewards:
          - YES ask in the 85–97c window (high implied P(YES), modest payout)
          - Tight spread and healthy volume
          - ROI if YES wins above HP_MIN_ROI_PCT
        """
        reasons: list[str] = []
        score = 0.0

        vol_fail = self._volume_gate(market)
        if vol_fail:
            return vol_fail

        yes_ask = market.yes_ask
        if yes_ask is None:
            return 0.0, ["No ask price"]

        if yes_ask < HP_MIN_YES_ASK:
            return 0.0, [f"YES ask {yes_ask}c below high-prob floor ({HP_MIN_YES_ASK}c)"]
        if yes_ask > HP_MAX_YES_ASK:
            return 0.0, [f"YES ask {yes_ask}c above high-prob cap ({HP_MAX_YES_ASK}c)"]

        gross = gross_roi_if_yes_wins_pct(yes_ask)
        net = fee_adjusted_roi_if_yes_wins_pct(yes_ask, round_trip_fees=False)
        passed, _, applied = passes_roi_gate(yes_ask, HP_MIN_ROI_PCT)
        if not passed:
            return 0.0, [
                f"Fee-adj ROI {applied:.1f}% below minimum {HP_MIN_ROI_PCT}% "
                f"(gross {gross:.1f}%)"
            ]
        roi = applied

        # Price band: prefer mid-high prob (88–94c) — balance of edge vs payout
        if 88 <= yes_ask <= 94:
            score += 0.35
            reasons.append(f"YES ask {yes_ask}c — sweet spot for high-prob entries")
        else:
            score += 0.20
            reasons.append(f"YES ask {yes_ask}c — in high-prob window")

        reasons.append(
            f"Implied P(YES) {yes_ask}%  |  net ROI if win {roi:.1f}% "
            f"(gross {gross:.1f}%)"
        )

        if market.spread <= 4:
            score += 0.25
            reasons.append(f"Spread {market.spread}c — tight")
        elif market.spread <= MAX_SPREAD_CENTS:
            score += 0.12
            reasons.append(f"Spread {market.spread}c — acceptable")
        else:
            return 0.0, [f"Spread {market.spread}c too wide"]

        if market.volume_24h >= 1_000:
            score += 0.25
            reasons.append(f"Volume 24h: {market.volume_24h:,}")
        elif market.volume_24h >= SCREENER_MIN_VOLUME_24H:
            score += 0.10
            reasons.append(f"Volume 24h: {market.volume_24h:,} — meets floor")
        else:
            return 0.0, [f"Volume 24h {market.volume_24h} below high-prob minimum"]

        mins = market.minutes_to_close
        if mins is not None and mins < MIN_MINUTES_TO_CLOSE:
            return 0.0, [f"Closing in {mins:.0f}m — too soon"]

        if mins is not None and mins >= 60:
            score += 0.15
            reasons.append(f"Closes in {mins/60:.0f}h")

        return round(min(score, 1.0), 3), reasons

    def _score_arb_comp(
        self, market: MarketSummary, book: OrderBookSnapshot | None
    ) -> tuple[float, list[str]]:
        """
        Score a market for COMPLEMENTARY arbitrage.

        A complementary arb exists when YES_ask + NO_ask < 100 on the
        same market (single market, both sides mispriced simultaneously).

        Kalshi sometimes has this when the book is thin on one side.
        """
        reasons: list[str] = []

        vol_fail = self._volume_gate(market)
        if vol_fail:
            return vol_fail

        yes_ask = market.yes_ask
        no_ask  = market.no_ask

        if yes_ask is None or no_ask is None:
            return 0.0, ["Missing bid/ask data"]

        combined    = yes_ask + no_ask
        profit_cents = 100 - combined

        if profit_cents < 1:
            return 0.0, [f"No comp arb: YES_ask + NO_ask = {combined}c (>= 100)"]

        # Score proportional to profit, capped at $5/contract = perfect score
        score   = min(profit_cents / 5.0, 1.0)
        reasons = [
            f"Comp arb detected: YES_ask({yes_ask}) + NO_ask({no_ask}) = {combined}c",
            f"Guaranteed profit: {profit_cents}c per contract (${profit_cents/100:.2f})",
        ]
        return round(score, 3), reasons

    # ── Arbitrage group detectors ─────────────────────────────────────────────

    def _detect_exhaustive_arb(
        self, markets: list[MarketSummary]
    ) -> ArbitrageGroup | None:
        """
        Detect buy-all-YES arb across markets in the same event.

        Sum of YES asks < 100 → buy all → guaranteed $1 at resolution.
        """
        open_markets = [m for m in markets if m.yes_ask is not None and m.status == "open"]
        if len(open_markets) < 2:
            return None

        total_ask    = sum(m.yes_ask for m in open_markets)
        profit_cents = 100 - total_ask

        if profit_cents < 1:
            return None

        tickers = [m.ticker for m in open_markets]
        return ArbitrageGroup(
            arb_type=StrategyFit.ARB_SET,
            tickers=tickers,
            profit_cents=profit_cents,
            details=(
                f"Exhaustive set arb: {len(tickers)} markets, "
                f"YES asks sum to {total_ask}c, profit {profit_cents}c/contract"
            ),
        )

    def _detect_dominance_arb(
        self, markets: list[MarketSummary]
    ) -> list[ArbitrageGroup]:
        """
        Detect dominance-chain violations within the same series.

        Groups markets by series_ticker, sorts by implied probability,
        and checks for any price inversion (higher-threshold market priced
        above lower-threshold market within the same series).
        """
        # Group by series
        series_groups: dict[str, list[MarketSummary]] = {}
        for m in markets:
            if m.series_ticker:
                series_groups.setdefault(m.series_ticker, []).append(m)

        arbs: list[ArbitrageGroup] = []

        for series_ticker, group in series_groups.items():
            if len(group) < 2:
                continue

            # Sort by implied probability ascending (lowest prob = highest threshold)
            group.sort(key=lambda m: m.implied_prob)

            for i in range(len(group) - 1):
                lower = group[i]    # lower probability = stricter threshold
                upper = group[i+1]  # higher probability = weaker threshold

                if lower.yes_ask is None or upper.yes_bid is None:
                    continue

                # Violation: P(stricter) > P(weaker) implied by prices
                if lower.yes_ask >= upper.yes_bid:
                    continue

                inversion = upper.yes_bid - lower.yes_ask
                if inversion < 1:
                    continue

                arbs.append(ArbitrageGroup(
                    arb_type=StrategyFit.ARB_DOM,
                    tickers=[lower.ticker, upper.ticker],
                    profit_cents=inversion,
                    details=(
                        f"Dominance violation in series {series_ticker}: "
                        f"{lower.ticker} ask={lower.yes_ask}c < "
                        f"{upper.ticker} bid={upper.yes_bid}c  "
                        f"(inversion: {inversion}c)"
                    ),
                ))

        return arbs

    # ── Strategy hints ────────────────────────────────────────────────────────

    def _strategy_hints(
        self,
        market: MarketSummary,
        book: OrderBookSnapshot | None,
    ) -> dict[str, Any]:
        """
        Return per-strategy guidance for one market's detail view.
        Used by get_market_detail().
        """
        hints: dict[str, Any] = {}

        yes_ask = market.yes_ask
        yes_bid = market.yes_bid

        # Kelly hints
        if yes_ask and yes_bid:
            hints["kelly"] = {
                "suggested_kelly_fraction": round(1.0 / config.KELLY_DIVISOR, 3),
                "max_position_usd": config.MAX_POSITION_CENTS / 100,
                "min_edge_required_pct": config.MIN_EDGE_TO_VIG * 100,
                "note": "Set model probability in KellyStrategy to generate signals",
            }

        # Green-up hints
        if yes_ask and GREEN_UP_MIN_ENTRY_PRICE <= yes_ask <= GREEN_UP_MAX_ENTRY_PRICE:
            entry_odds    = round(100.0 / yes_ask, 2)
            # Preview at trigger price 68
            trigger       = 68
            no_at_trigger = 100 - trigger
            pot_return    = config.MAX_POSITION_CENTS * entry_odds
            hedge_stake   = pot_return / (100.0 / no_at_trigger)
            locked_profit = pot_return - config.MAX_POSITION_CENTS - hedge_stake
            hints["green_up"] = {
                "entry_price_cents": yes_ask,
                "entry_decimal_odds": entry_odds,
                "example_entry_stake_usd": config.MAX_POSITION_CENTS / 100,
                "example_potential_return_usd": round(pot_return / 100, 2),
                "example_hedge_at_trigger_68": {
                    "no_price_cents":        no_at_trigger,
                    "no_decimal_odds":       round(100.0 / no_at_trigger, 2),
                    "hedge_stake_usd":       round(hedge_stake / 100, 2),
                    "locked_profit_usd":     round(locked_profit / 100, 2),
                },
            }

        # High-probability hints
        if yes_ask and HP_MIN_YES_ASK <= yes_ask <= HP_MAX_YES_ASK:
            gross = gross_roi_if_yes_wins_pct(yes_ask)
            net = fee_adjusted_roi_if_yes_wins_pct(yes_ask, round_trip_fees=False)
            hints["high_prob"] = {
                "yes_ask_cents":           yes_ask,
                "implied_prob_pct":        yes_ask,
                "payout_per_contract_usd": round((100 - yes_ask) / 100, 2),
                "gross_roi_if_yes_wins_pct": round(gross, 2),
                "fee_adjusted_roi_if_yes_wins_pct": round(net, 2),
                "roi_if_yes_wins_pct":     round(net, 2),
                "fee_per_contract_cents":  config.FEE_PER_CONTRACT_CENTS,
                "example_stake_usd":       getattr(config, "HP_STAKE_CENTS", 5000) / 100,
                "entry_modes": [
                    "passive", "cross_spread", "market",
                    "limit_at_ask", "limit_at_bid", "limit_at_mid", "limit_offset",
                ],
                "post_fill_modes": [
                    "hold", "resting_take_profit", "resting_stop", "tp_and_stop",
                ],
            }

        # Arb hints
        no_ask = market.no_ask
        if yes_ask and no_ask:
            combined     = yes_ask + no_ask
            profit_cents = 100 - combined
            hints["arbitrage"] = {
                "yes_ask":       yes_ask,
                "no_ask":        no_ask,
                "combined_cost": combined,
                "comp_arb_profit_cents": max(0, profit_cents),
                "comp_arb_viable": profit_cents > 0,
            }

        return hints


def score_for_strategy(
    market: MarketSummary,
    strategy: str,
    book: OrderBookSnapshot | None = None,
) -> float:
    """
    Synchronous screener score for one market and strategy name.

    Used by discovery ``rank_by=screener`` to pick the top N tickers for a bot
    strategy (e.g. green_up in Sports).
    """
    key = SCREENER_STRATEGY_ALIASES.get(strategy.strip().lower(), strategy.strip().lower())
    scorer = MarketScreener.__new__(MarketScreener)

    if key == "green_up":
        score, _ = scorer._score_green_up(market, book)
    elif key == "kelly":
        score, _ = scorer._score_kelly(market, book)
    elif key == "high_prob":
        score, _ = scorer._score_high_prob(market, book)
    elif key == "arb":
        score, _ = scorer._score_arb_comp(market, book)
    else:
        return 0.0
    return score
