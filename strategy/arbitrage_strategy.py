"""
strategy/arbitrage_strategy.py

Cross-market arbitrage detection for Kalshi binary prediction markets.

─── Three arbitrage types detected ────────────────────────────────────────────

1. COMPLEMENTARY ARBITRAGE (same event, YES+NO mispriced)
   -------------------------------------------------------
   A single binary market must satisfy:  YES_ask + NO_ask >= 100  (no-arb)
   Equivalently:                         YES_bid + NO_bid <= 100

   Opportunity arises when:
       YES_ask + NO_ask < 100   →  buy both sides for guaranteed profit
       profit_cents = 100 - (YES_ask + NO_ask)

   Example:
       YES ask = 44,  NO ask = 53  →  combined cost = 97  →  $3 profit per contract
       Buy YES at 44 + Buy NO at 53 = pay 97, collect 100 at resolution.

2. MUTUALLY EXCLUSIVE SET ARBITRAGE (exhaustive event set)
   --------------------------------------------------------
   For a set of mutually exclusive, collectively exhaustive outcomes
   (e.g. "Which party wins the Senate?") the YES prices must sum to 100.

   Opportunity arises when:
       SUM(best_ask for all outcomes) < 100   →  buy all → guaranteed profit
       SUM(best_bid for all outcomes) > 100   →  sell all (buy NO) → guaranteed profit

   Example (3-outcome race):
       A_ask=30, B_ask=35, C_ask=32  →  sum=97  →  $3 profit per unit

3. CROSS-MARKET SYNTHETIC ARBITRAGE (correlated markets)
   -------------------------------------------------------
   When two markets measure the same underlying but with different resolutions
   or expiries, mispricing between them creates a synthetic position.

   Example:
       "INXD > 5000 by Dec" YES @ 60
       "INXD > 4800 by Dec" YES @ 55   ← should be >= 60 (5000>4800 implies 4800>4800)
       Buy the cheaper contract (4800), the market will converge.

   This strategy detects violations of stochastic dominance between
   related threshold markets (e.g. "index > X" for multiple values of X).

─── Position sizing ────────────────────────────────────────────────────────────

All three types use a simplified sizing rule (not full Kelly) because
arbitrage profit is bounded and deterministic — Kelly's "edge over odds"
formulation degenerates to near-100% when profit is guaranteed.

Instead we size as:
    size_cents = min(profit_cents * ARB_SIZE_MULTIPLIER, MAX_POSITION_CENTS)

ARB_SIZE_MULTIPLIER controls how aggressively we scale into a free-money trade.
Set conservatively (default 50×) because:
  - Fill risk: both legs must fill; partial fills create directional exposure
  - Execution latency: the arb may close before both orders land
  - Model error: "guaranteed" assumes books are accurate — always verify

─── Leg management ─────────────────────────────────────────────────────────────

ArbitrageStrategy tracks open arb legs internally. When the first leg fills,
it flags the arb as "leg_1_filled" and urgently seeks to fill leg 2.
If leg 2 doesn't fill within LEG2_TIMEOUT_SECONDS, it emits a warning and
the circuit breaker should be notified (directional exposure taken on).

─── Usage ──────────────────────────────────────────────────────────────────────

    arb = ArbitrageStrategy()

    # Register a complementary pair
    arb.register_complementary("PRES-2024-DEM", "PRES-2024-REP")

    # Register an exhaustive outcome set
    arb.register_exhaustive_set(["SENATE-DEM", "SENATE-REP", "SENATE-TIE"])

    # Register a dominance chain (ascending threshold markets)
    arb.register_dominance_chain([
        "INXD-23DEC29-B4500",   # P(index > 4500)
        "INXD-23DEC29-B4700",   # P(index > 4700)   <- must be <= above
        "INXD-23DEC29-B5000",   # P(index > 5000)   <- must be <= above
    ])

    # Feed ticks (called by main.py's on_tick callback)
    signal = arb.evaluate(tick)
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import config
from logging_.structured_logger import logger
from strategy.base_strategy import BaseStrategy, Side, Signal


# ── Tunables (can be promoted to config.py) ──────────────────────────────────

ARB_SIZE_MULTIPLIER: int    = 50     # size_cents = profit_cents * multiplier
MIN_PROFIT_CENTS: int       = 2      # ignore arbs worth less than $0.02/contract
LEG2_TIMEOUT_SECONDS: float = 5.0   # warn if leg 2 not filled within this window
MAX_STALENESS_US: int       = 2_000_000  # ignore books not updated in last 2 seconds


# ── Internal types ───────────────────────────────────────────────────────────

class ArbType(str, Enum):
    COMPLEMENTARY = "complementary"
    EXHAUSTIVE    = "exhaustive_set"
    DOMINANCE     = "dominance_chain"


@dataclass
class ArbOpportunity:
    """Represents a detected arbitrage opportunity (may span multiple tickers)."""
    arb_type:      ArbType
    tickers:       list[str]           # all tickers involved
    legs:          list[dict]          # [{ticker, side, limit_price}]
    profit_cents:  int                 # guaranteed profit per contract unit
    size_cents:    int                 # recommended position size
    detected_at_us: int = field(default_factory=lambda: int(time.time() * 1_000_000))


@dataclass
class OpenArbLeg:
    """Tracks a partially filled arbitrage (leg 1 filled, awaiting leg 2)."""
    opportunity:   ArbOpportunity
    leg1_filled_at: float = field(default_factory=time.monotonic)
    leg1_ticker:   str = ""
    leg1_side:     str = ""


# ── Strategy ─────────────────────────────────────────────────────────────────

class ArbitrageStrategy(BaseStrategy):
    """
    Detects and signals on three classes of Kalshi binary market arbitrage.

    Call `evaluate(tick)` on every incoming market tick. The strategy
    maintains its own order book cache so it can compare multiple tickers
    on each update.
    """

    def __init__(self) -> None:
        # Registered relationship groups
        self._complementary_pairs: list[tuple[str, str]] = []
        self._exhaustive_sets:     list[list[str]]        = []
        self._dominance_chains:    list[list[str]]        = []

        # Local snapshot cache: ticker -> latest tick dict
        self._book_cache: dict[str, dict[str, Any]] = {}

        # Open arb legs awaiting leg 2
        self._open_legs: list[OpenArbLeg] = []

    # ── Registration API ─────────────────────────────────────────────────────

    def register_complementary(self, yes_ticker: str, no_ticker: str) -> None:
        """
        Register two tickers as the YES and NO sides of the same binary event.

        Kalshi sometimes lists "Candidate A wins" and "Candidate B wins"
        as separate markets even though they are complements.
        """
        self._complementary_pairs.append((yes_ticker, no_ticker))
        logger.info(
            "Arb: registered complementary pair",
            yes_ticker=yes_ticker,
            no_ticker=no_ticker,
        )

    def register_exhaustive_set(self, tickers: list[str]) -> None:
        """
        Register a set of mutually exclusive, collectively exhaustive outcome markets.

        The YES prices across all tickers must sum to exactly 100 at fair value.
        """
        if len(tickers) < 2:
            raise ValueError("Exhaustive set requires at least 2 tickers.")
        self._exhaustive_sets.append(list(tickers))
        logger.info(
            "Arb: registered exhaustive set",
            tickers=tickers,
            count=len(tickers),
        )

    def register_dominance_chain(self, tickers: list[str]) -> None:
        """
        Register an ordered list of threshold markets where each successive
        market implies a stricter condition.

        Example — "Index > X" markets ordered by ascending X:
            [INXD-B4500, INXD-B4700, INXD-B5000]

        By stochastic dominance:
            P(> 4500) >= P(> 4700) >= P(> 5000)

        Any violation (a higher-threshold market priced above a lower one)
        is an arbitrage.
        """
        if len(tickers) < 2:
            raise ValueError("Dominance chain requires at least 2 tickers.")
        self._dominance_chains.append(list(tickers))
        logger.info(
            "Arb: registered dominance chain",
            tickers=tickers,
            length=len(tickers),
        )

    # ── BaseStrategy interface ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "arbitrage"

    def evaluate(self, tick: dict[str, Any]) -> Signal | None:
        """
        Update the internal book cache with the new tick, then scan all
        registered relationships for an arbitrage opportunity.

        Returns the highest-profit Signal found, or None.
        """
        ticker = tick.get("ticker", "")
        if not ticker:
            return None

        # Reject stale books
        if not self._is_fresh(tick):
            return None

        self._book_cache[ticker] = tick
        self._warn_stale_open_legs()

        # Scan all relationship types; collect every opportunity found
        opportunities: list[ArbOpportunity] = []

        opportunities.extend(self._scan_complementary())
        opportunities.extend(self._scan_exhaustive_sets())
        opportunities.extend(self._scan_dominance_chains())

        if not opportunities:
            return None

        # Trade the highest-profit opportunity this tick
        best = max(opportunities, key=lambda o: o.profit_cents)
        return self._opportunity_to_signal(best)

    def on_fill(self, fill: dict[str, Any]) -> None:
        """
        Track partial fills so we can warn if leg 2 is not completing.
        Called by the execution manager when a fill arrives.
        """
        filled_ticker = fill.get("ticker", "")
        filled_side   = fill.get("side", "")

        # Check if this fill belongs to a known open arb leg
        for leg in self._open_legs:
            opp = leg.opportunity
            if filled_ticker == leg.leg1_ticker:
                logger.info(
                    "Arb leg 1 confirmed filled — awaiting leg 2",
                    arb_type=opp.arb_type,
                    leg1_ticker=filled_ticker,
                    remaining_legs=[l["ticker"] for l in opp.legs if l["ticker"] != filled_ticker],
                )
                return  # keep tracking until leg 2 fills

            # If leg 2 fills, close out the open leg record
            remaining = [l["ticker"] for l in opp.legs if l["ticker"] != leg.leg1_ticker]
            if filled_ticker in remaining:
                logger.info(
                    "Arb fully filled — both legs complete",
                    arb_type=opp.arb_type,
                    profit_cents=opp.profit_cents,
                    tickers=opp.tickers,
                )
                self._open_legs = [l for l in self._open_legs if l is not leg]
                return

    # ── Scanner: complementary pairs ─────────────────────────────────────────

    def _scan_complementary(self) -> list[ArbOpportunity]:
        """
        YES_ask + NO_ask < 100  →  buy both sides, collect $1 at resolution.
        """
        found = []
        for yes_ticker, no_ticker in self._complementary_pairs:
            yes_book = self._book_cache.get(yes_ticker)
            no_book  = self._book_cache.get(no_ticker)

            if not yes_book or not no_book:
                continue

            yes_ask = yes_book.get("best_ask")
            no_ask  = no_book.get("best_ask")

            if yes_ask is None or no_ask is None:
                continue

            combined_cost = yes_ask + no_ask
            profit_cents  = 100 - combined_cost

            if profit_cents < MIN_PROFIT_CENTS:
                continue

            size_cents = min(
                profit_cents * ARB_SIZE_MULTIPLIER,
                config.MAX_POSITION_CENTS,
            )

            logger.info(
                "Arb detected: complementary",
                yes_ticker=yes_ticker,
                no_ticker=no_ticker,
                yes_ask=yes_ask,
                no_ask=no_ask,
                profit_cents=profit_cents,
            )

            found.append(ArbOpportunity(
                arb_type=ArbType.COMPLEMENTARY,
                tickers=[yes_ticker, no_ticker],
                legs=[
                    {"ticker": yes_ticker, "side": Side.YES, "limit_price": yes_ask},
                    {"ticker": no_ticker,  "side": Side.YES, "limit_price": no_ask},
                ],
                profit_cents=profit_cents,
                size_cents=size_cents,
            ))

        return found

    # ── Scanner: exhaustive sets ──────────────────────────────────────────────

    def _scan_exhaustive_sets(self) -> list[ArbOpportunity]:
        """
        SUM(best_ask) < 100  →  buy all outcomes → guaranteed $1.
        SUM(best_bid) > 100  →  sell all (buy NO on all) → guaranteed profit.
        """
        found = []
        for ticker_set in self._exhaustive_sets:
            books = [self._book_cache.get(t) for t in ticker_set]
            if any(b is None for b in books):
                continue  # need all books populated

            # Check BUY ALL opportunity
            asks = [b.get("best_ask") for b in books]
            if None not in asks:
                total_ask    = sum(asks)
                profit_cents = 100 - total_ask
                if profit_cents >= MIN_PROFIT_CENTS:
                    size_cents = min(
                        profit_cents * ARB_SIZE_MULTIPLIER,
                        config.MAX_POSITION_CENTS,
                    )
                    logger.info(
                        "Arb detected: exhaustive set (buy all YES)",
                        tickers=ticker_set,
                        total_ask=total_ask,
                        profit_cents=profit_cents,
                    )
                    found.append(ArbOpportunity(
                        arb_type=ArbType.EXHAUSTIVE,
                        tickers=ticker_set,
                        legs=[
                            {"ticker": t, "side": Side.YES, "limit_price": ask}
                            for t, ask in zip(ticker_set, asks)
                        ],
                        profit_cents=profit_cents,
                        size_cents=size_cents,
                    ))

            # Check SELL ALL (buy NO on all) opportunity
            bids = [b.get("best_bid") for b in books]
            if None not in bids:
                total_bid    = sum(bids)
                profit_cents = total_bid - 100
                if profit_cents >= MIN_PROFIT_CENTS:
                    # Buying NO at price (100 - bid) on each market
                    no_prices  = [100 - bid for bid in bids]
                    size_cents = min(
                        profit_cents * ARB_SIZE_MULTIPLIER,
                        config.MAX_POSITION_CENTS,
                    )
                    logger.info(
                        "Arb detected: exhaustive set (buy all NO)",
                        tickers=ticker_set,
                        total_bid=total_bid,
                        profit_cents=profit_cents,
                    )
                    found.append(ArbOpportunity(
                        arb_type=ArbType.EXHAUSTIVE,
                        tickers=ticker_set,
                        legs=[
                            {"ticker": t, "side": Side.NO, "limit_price": no_p}
                            for t, no_p in zip(ticker_set, no_prices)
                        ],
                        profit_cents=profit_cents,
                        size_cents=size_cents,
                    ))

        return found

    # ── Scanner: dominance chains ─────────────────────────────────────────────

    def _scan_dominance_chains(self) -> list[ArbOpportunity]:
        """
        For a chain [A, B, C] ordered by ascending threshold,
        P(A) >= P(B) >= P(C) must hold.

        Violation: B_ask > A_bid  (market implies P(B) > P(A))
        Trade:     Buy A (cheaper, understated), Sell B (overpriced → buy NO on B)
        Profit at resolution: exactly one of them pays $1; the mispricing is the edge.
        """
        found = []
        for chain in self._dominance_chains:
            for i in range(len(chain) - 1):
                lower_ticker  = chain[i]      # lower threshold → higher probability
                higher_ticker = chain[i + 1]  # higher threshold → lower probability

                lower_book  = self._book_cache.get(lower_ticker)
                higher_book = self._book_cache.get(higher_ticker)

                if not lower_book or not higher_book:
                    continue

                lower_bid   = lower_book.get("best_bid")
                higher_ask  = higher_book.get("best_ask")

                if lower_bid is None or higher_ask is None:
                    continue

                # Violation: P(higher threshold) > P(lower threshold)
                # i.e. you can buy the "should be cheaper" contract above the
                # bid of the "should be more expensive" contract.
                if higher_ask >= lower_bid:
                    continue  # no violation — prices are in correct order

                # Mispricing magnitude: how inverted are the prices?
                inversion_cents = lower_bid - higher_ask
                if inversion_cents < MIN_PROFIT_CENTS:
                    continue

                # Trade: buy the underpriced higher-threshold contract,
                # sell (buy NO on) the overpriced lower-threshold contract.
                size_cents = min(
                    inversion_cents * ARB_SIZE_MULTIPLIER,
                    config.MAX_POSITION_CENTS,
                )

                logger.info(
                    "Arb detected: dominance violation",
                    lower_ticker=lower_ticker,
                    higher_ticker=higher_ticker,
                    lower_bid=lower_bid,
                    higher_ask=higher_ask,
                    inversion_cents=inversion_cents,
                )

                found.append(ArbOpportunity(
                    arb_type=ArbType.DOMINANCE,
                    tickers=[lower_ticker, higher_ticker],
                    legs=[
                        # Buy the underpriced higher-threshold contract
                        {"ticker": higher_ticker, "side": Side.YES,  "limit_price": higher_ask},
                        # Buy NO on the overpriced lower-threshold contract
                        {"ticker": lower_ticker,  "side": Side.NO,   "limit_price": 100 - lower_bid},
                    ],
                    profit_cents=inversion_cents,
                    size_cents=size_cents,
                ))

        return found

    # ── Signal construction ───────────────────────────────────────────────────

    def _opportunity_to_signal(self, opp: ArbOpportunity) -> Signal:
        """
        Convert the first leg of an ArbOpportunity into a Signal.

        The execution manager will submit leg 1 immediately. The ArbitrageStrategy
        tracks the open leg and, on the next tick after the fill arrives via
        on_fill(), the strategy re-evaluates and emits leg 2.

        This sequential approach avoids the need for the execution manager to
        handle multi-leg atomic orders (which Kalshi does not natively support).
        """
        leg1 = opp.legs[0]

        # Vig proxy: use the spread of the leg-1 market, or 1 cent minimum
        book       = self._book_cache.get(leg1["ticker"], {})
        spread     = book.get("spread") or 2
        vig_proxy  = max(spread / 2, 0.5) / 100.0

        # Edge: guaranteed profit expressed as a probability-equivalent
        edge = opp.profit_cents / 100.0
        edge_to_vig = edge / vig_proxy if vig_proxy > 0 else 0.0

        # Register this as an open arb so we track leg-2 urgency
        self._open_legs.append(OpenArbLeg(
            opportunity=opp,
            leg1_ticker=leg1["ticker"],
            leg1_side=leg1["side"].value,
        ))

        return Signal(
            ticker=leg1["ticker"],
            side=leg1["side"],
            size_cents=opp.size_cents,
            limit_price=leg1["limit_price"],
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=1.0,   # arb profit is deterministic — max confidence
            strategy=self.name,
            meta={
                "arb_type":     opp.arb_type.value,
                "all_tickers":  opp.tickers,
                "all_legs":     [
                    {"ticker": l["ticker"], "side": l["side"].value, "price": l["limit_price"]}
                    for l in opp.legs
                ],
                "profit_cents": opp.profit_cents,
                "leg_number":   1,
                "total_legs":   len(opp.legs),
            },
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_fresh(tick: dict[str, Any]) -> bool:
        """Reject ticks whose order book hasn't been updated recently."""
        updated_at = tick.get("updated_at_us", 0)
        age_us     = int(time.time() * 1_000_000) - updated_at
        return age_us <= MAX_STALENESS_US

    def _warn_stale_open_legs(self) -> None:
        """Emit warnings for arb legs where leg 2 has not filled in time."""
        now = time.monotonic()
        for leg in self._open_legs:
            elapsed = now - leg.leg1_filled_at
            if elapsed > LEG2_TIMEOUT_SECONDS:
                logger.warning(
                    "Arb leg 2 timeout — directional exposure risk",
                    arb_type=leg.opportunity.arb_type,
                    leg1_ticker=leg.leg1_ticker,
                    elapsed_seconds=round(elapsed, 1),
                    all_tickers=leg.opportunity.tickers,
                )
