"""
strategy/green_up_strategy.py

"Green Up" — Back High, Lay Low in-play hedging strategy.

────────────────────────────────────────────────────────────────────────────────
CORE FORMULA REFERENCE
────────────────────────────────────────────────────────────────────────────────

All hedge math is derived from two canonical formulas:

  ① FULL GREEN (equal profit regardless of outcome)
  ─────────────────────────────────────────────────
      Hedge Stake = Potential Return / New Odds

      Where:
          Potential Return = Initial Stake × Entry Odds
          New Odds         = decimal odds of the opposing side at hedge time

      Example (from spec):
          Initial Stake    = $100  at  5.00 odds
          Potential Return = $100 × 5.00 = $500
          New (fav) Odds   = 2.50
          Hedge Stake      = $500 / 2.50 = $200

          If underdog wins:  $500 - $100 - $200 = $200 profit
          If favorite wins:  $200 × 2.50 - $100 - $200 = $200 profit  ✓ equal

  ② FREE BET / STAKE BACK (no-lose, leave upside on entry leg)
  ──────────────────────────────────────────────────────────────
      Hedge Stake = Initial Stake / (New Odds - 1)

      Example (from spec):
          Initial Stake = $100,  New Odds = 2.50
          Hedge Stake   = $100 / (2.50 - 1) = $100 / 1.50 = $66.67

          If favorite wins:  $66.67 × 2.50 - $100 - $66.67 ≈ $0  (break even)
          If underdog wins:  $500   - $100 - $66.67         = $333.33 profit  ✓

────────────────────────────────────────────────────────────────────────────────
KALSHI TRANSLATION
────────────────────────────────────────────────────────────────────────────────

Kalshi uses CENTS (1–99) as prices rather than decimal odds.
Conversion:  decimal_odds = 100 / price_cents

  Entry:  Buy YES at price P_entry cents   →  odds_entry = 100 / P_entry
  Hedge:  Buy NO  at price P_hedge cents   →  odds_hedge = 100 / P_hedge
          (NO price = 100 - YES_bid at hedge time)

  ① Full green in cents:
      potential_return_cents = entry_cents × (100 / P_entry)
      hedge_cents            = potential_return_cents / (100 / P_hedge)
                             = potential_return_cents × P_hedge / 100
      locked_profit_cents    = potential_return_cents - entry_cents - hedge_cents

  ② Free bet / stake-back in cents:
      odds_hedge_net         = (100 / P_hedge) - 1   i.e. (100 - P_hedge) / P_hedge
      hedge_cents            = entry_cents / odds_hedge_net
                             = entry_cents × P_hedge / (100 - P_hedge)
      profit_if_entry_wins   = potential_return_cents - entry_cents - hedge_cents

────────────────────────────────────────────────────────────────────────────────
STATE MACHINE
────────────────────────────────────────────────────────────────────────────────

    WATCHING ──► ENTERED ──► HEDGING ──► HEDGED   (profit locked)
                    │
                    └──────► STOPPING ──► STOPPED  (loss capped)

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

    from strategy.green_up_strategy import GreenUpStrategy, HedgeMode

    strat = GreenUpStrategy(
        entry_max_price=25,         # back YES only when price <= 25c (4.00 odds)
        hedge_trigger_price=68,     # green up when YES bid reaches 68c
        hedge_mode=HedgeMode.FULL_GREEN,
        stop_loss_threshold=0.40,   # stop out if YES falls 40% below entry
    )

    strat.add_watch_ticker("PRES-2024-DEM")

    # In the main on_tick callback:
    signal = strat.evaluate(tick)

    # In the main on_fill callback:
    strat.on_fill(fill_dict)
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import config
from logging_.structured_logger import logger
from strategy.base_strategy import BaseStrategy, Side, Signal


# ── Module-level tunables ────────────────────────────────────────────────────

DEFAULT_ENTRY_MAX_PRICE: int          = 25    # cents; 25c = 4.00 decimal odds
DEFAULT_HEDGE_TRIGGER_PRICE: int      = 68    # cents; YES bid must reach this to hedge
DEFAULT_STOP_LOSS_THRESHOLD: float    = 0.40  # exit if price falls 40% below entry
DEFAULT_PARTIAL_HEDGE_FRACTION: float = 0.50  # PARTIAL mode: hedge 50% of full-green size
MIN_HEDGE_PROFIT_CENTS: int           = 50    # skip hedge if locked profit < $0.50


# ── Supporting enums ─────────────────────────────────────────────────────────

class HedgeMode(str, Enum):
    FULL_GREEN = "full_green"  # equal profit on both outcomes  [formula 1]
    STAKE_BACK = "stake_back"  # break-even hedge, keep YES upside  [formula 2]
    PARTIAL    = "partial"     # fraction of full-green, residual upside on YES


class PositionState(str, Enum):
    WATCHING = "watching"   # no position yet, monitoring for entry
    ENTERED  = "entered"    # YES bought, waiting for hedge trigger or stop
    HEDGING  = "hedging"    # hedge order sent, awaiting fill confirmation
    HEDGED   = "hedged"     # both legs filled — profit locked
    STOPPING = "stopping"   # stop-loss order sent
    STOPPED  = "stopped"    # stop-loss filled — loss capped
    CLOSED   = "closed"     # market resolved


# ── Position dataclass ────────────────────────────────────────────────────────

@dataclass
class GreenUpPosition:
    """
    Full lifecycle record for one green-up trade.

    Created when an entry signal is generated; updated on every fill.
    """

    ticker: str
    state:  PositionState = PositionState.WATCHING

    # Entry leg
    entry_price_cents: int  = 0   # YES price paid (cents, 1-99)
    entry_stake_cents: int  = 0   # dollars staked on YES leg (in cents)
    entry_order_id:    str  = ""

    # Hedge leg
    hedge_price_cents: int  = 0   # NO price paid (cents, 1-99)
    hedge_stake_cents: int  = 0   # dollars staked on NO leg (in cents)
    hedge_order_id:    str  = ""

    # Risk tracking
    stop_loss_trigger_price: int  = 0   # YES bid below this fires stop
    locked_profit_cents:     int  = 0   # guaranteed P&L after hedge fills

    # Timestamps
    entered_at: float = field(default_factory=time.monotonic)
    hedged_at:  float = 0.0

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def entry_decimal_odds(self) -> float:
        """e.g. 25c entry -> 100/25 = 4.00 decimal odds."""
        return 100.0 / self.entry_price_cents if self.entry_price_cents > 0 else 0.0

    @property
    def potential_return_cents(self) -> int:
        """
        What the YES leg pays if it wins.
        = entry_stake x entry_decimal_odds  (in cents)
        """
        if self.entry_price_cents <= 0:
            return 0
        return int(self.entry_stake_cents * (100.0 / self.entry_price_cents))

    @property
    def time_in_trade_s(self) -> float:
        return time.monotonic() - self.entered_at

    # ── Formula 1: FULL GREEN ─────────────────────────────────────────────────

    def compute_full_green(self, no_price_cents: int) -> tuple[int, int]:
        """
        Equal-profit hedge using:

            Hedge Stake = Potential Return / New Odds

        where:
            Potential Return = entry_stake x entry_odds  (already in potential_return_cents)
            New Odds         = 100 / no_price_cents

        Returns:
            (hedge_stake_cents, locked_profit_cents)

        Spec verification (mapped to cents):
            entry_stake  = 10_000c ($100),  entry_price = 20c  -> odds 5.00
            potential    = 10_000 x 5.00 = 50_000c ($500)
            no_price     = 40c            -> odds 2.50
            hedge_stake  = 50_000 / 2.50 = 20_000c ($200)         [formula 1]
            locked_profit = 50_000 - 10_000 - 20_000 = 20_000c ($200) OK
        """
        if no_price_cents <= 0 or self.entry_price_cents <= 0:
            return 0, 0

        new_decimal_odds  = 100.0 / no_price_cents
        potential         = self.potential_return_cents              # cents
        hedge_stake_cents = int(potential / new_decimal_odds)        # formula 1
        locked_profit     = potential - self.entry_stake_cents - hedge_stake_cents

        return hedge_stake_cents, locked_profit

    # ── Formula 2: STAKE BACK / FREE BET ─────────────────────────────────────

    def compute_stake_back(self, no_price_cents: int) -> tuple[int, int]:
        """
        Break-even hedge using:

            Hedge Stake = Initial Stake / (New Odds - 1)

        Outcome:
            If NO wins  -> hedge pays back exactly the initial stake -> net $0
            If YES wins -> full YES payout minus initial stake minus tiny hedge

        Returns:
            (hedge_stake_cents, profit_if_yes_wins_cents)

        Spec verification (mapped to cents):
            entry_stake = 10_000c ($100),  no_price = 40c -> odds 2.50
            odds_net    = 2.50 - 1 = 1.50
            hedge_stake = 10_000 / 1.50 = 6_667c ($66.67)          [formula 2]
            if YES wins: 50_000 - 10_000 - 6_667 = 33_333c ($333.33) OK
            if NO  wins: 6_667 x 2.50 - 10_000 - 6_667 ~ 0          OK
        """
        if no_price_cents <= 0 or self.entry_price_cents <= 0:
            return 0, 0

        new_decimal_odds  = 100.0 / no_price_cents
        odds_net          = new_decimal_odds - 1.0                   # (New Odds - 1)
        if odds_net <= 0:
            return 0, 0

        hedge_stake_cents  = int(self.entry_stake_cents / odds_net)  # formula 2
        profit_if_yes_wins = (
            self.potential_return_cents
            - self.entry_stake_cents
            - hedge_stake_cents
        )

        return hedge_stake_cents, profit_if_yes_wins

    # ── PARTIAL ───────────────────────────────────────────────────────────────

    def compute_partial(self, no_price_cents: int, fraction: float) -> tuple[int, int]:
        """
        Hedge a fraction of the full-green stake.

        fraction=0.5 -> half formula-1 stake, leaving half the YES upside exposed.

        Returns:
            (hedge_stake_cents, guaranteed_floor_cents)
        """
        full_hedge, full_profit = self.compute_full_green(no_price_cents)
        partial_hedge = int(full_hedge * fraction)
        floor_cents   = (
            int(full_profit * fraction)
            - int(self.entry_stake_cents * (1.0 - fraction))
        )
        return partial_hedge, floor_cents


# ── Strategy class ────────────────────────────────────────────────────────────

class GreenUpStrategy(BaseStrategy):
    """
    In-play green-up strategy: back an underdog cheaply, hedge when odds shift.

    Entry:  YES price <= entry_max_price  (underdog, high decimal odds)
    Hedge:  YES bid  >= hedge_trigger_price  (market moved in our favour)
    Stop:   YES bid  <= stop_loss_trigger   (market moving against us)
    """

    def __init__(
        self,
        entry_max_price:        int       = DEFAULT_ENTRY_MAX_PRICE,
        hedge_trigger_price:    int       = DEFAULT_HEDGE_TRIGGER_PRICE,
        hedge_mode:             HedgeMode = HedgeMode.FULL_GREEN,
        stop_loss_threshold:    float     = DEFAULT_STOP_LOSS_THRESHOLD,
        partial_hedge_fraction: float     = DEFAULT_PARTIAL_HEDGE_FRACTION,
    ) -> None:
        """
        Args:
            entry_max_price:        Only back YES when best_ask <= this (cents).
                                    25 = 4.00 decimal odds, 20 = 5.00 odds.
            hedge_trigger_price:    Green up when YES bid >= this (cents).
                                    68c YES => 32c NO => 3.13 decimal odds on NO.
            hedge_mode:             Which formula to apply:
                                      FULL_GREEN  equal profit both outcomes [1]
                                      STAKE_BACK  free-bet, keep YES upside  [2]
                                      PARTIAL     fraction of formula-1
            stop_loss_threshold:    Stop out if YES falls this fraction below entry.
                                    0.40 = stop if price drops 40% from entry.
            partial_hedge_fraction: For PARTIAL mode only — fraction of full-green
                                    stake to place (0.0 to 1.0).
        """
        self._entry_max_price        = entry_max_price
        self._hedge_trigger_price    = hedge_trigger_price
        self._hedge_mode             = hedge_mode
        self._stop_loss_threshold    = stop_loss_threshold
        self._partial_hedge_fraction = partial_hedge_fraction

        # ticker -> GreenUpPosition
        self._positions: dict[str, GreenUpPosition] = {}

    # ── BaseStrategy interface ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return f"green_up_{self._hedge_mode.value}"

    def evaluate(self, tick: dict[str, Any]) -> Signal | None:
        """
        Called on every market tick.

        Priority order (highest first):
            1. Stop-loss check on active (ENTERED) positions
            2. Hedge trigger on active (ENTERED) positions
            3. New entry opportunity (WATCHING / no position)
        """
        ticker   = tick.get("ticker", "")
        best_bid = tick.get("best_bid")
        best_ask = tick.get("best_ask")
        if not ticker or best_bid is None or best_ask is None:
            return None

        pos = self._positions.get(ticker)

        if pos and pos.state == PositionState.ENTERED:
            stop_sig = self._check_stop_loss(pos, tick)
            if stop_sig:
                return stop_sig

            hedge_sig = self._check_hedge_trigger(pos, tick)
            if hedge_sig:
                return hedge_sig

        if pos is None or pos.state == PositionState.WATCHING:
            return self._check_entry(ticker, tick)

        return None

    def on_fill(self, fill: dict[str, Any]) -> None:
        """
        Advance the position state machine when a fill is confirmed.
        Call this from main.py's FILL_RECEIVED handler.
        """
        ticker   = fill.get("ticker", "")
        side     = fill.get("side", "")
        price    = fill.get("price", 0)
        size_c   = fill.get("size_cents", 0)
        order_id = fill.get("order_id", "")

        pos = self._positions.get(ticker)
        if pos is None:
            return

        # Entry fill
        if pos.state == PositionState.WATCHING and side == Side.YES.value:
            pos.entry_price_cents        = price
            pos.entry_stake_cents        = size_c
            pos.entry_order_id           = order_id
            pos.stop_loss_trigger_price  = int(price * (1.0 - self._stop_loss_threshold))
            pos.entered_at               = time.monotonic()
            pos.state                    = PositionState.ENTERED

            logger.info(
                "GreenUp: entry filled",
                ticker=ticker,
                entry_price_cents=price,
                entry_decimal_odds=round(pos.entry_decimal_odds, 3),
                entry_stake_cents=size_c,
                potential_return_cents=pos.potential_return_cents,
                potential_return_usd=round(pos.potential_return_cents / 100, 2),
                stop_loss_trigger_price=pos.stop_loss_trigger_price,
                strategy=self.name,
            )

        # Hedge fill (FULL_GREEN / STAKE_BACK / PARTIAL)
        elif pos.state in (PositionState.HEDGING, PositionState.ENTERED) \
                and side == Side.NO.value:
            pos.hedge_price_cents = price
            pos.hedge_stake_cents = size_c
            pos.hedge_order_id    = order_id
            pos.hedged_at         = time.monotonic()
            pos.state             = PositionState.HEDGED

            # Actual locked profit from the real fill prices
            locked = pos.potential_return_cents - pos.entry_stake_cents - size_c
            pos.locked_profit_cents = locked

            logger.info(
                "GreenUp: hedge filled — position greened up",
                ticker=ticker,
                entry_price_cents=pos.entry_price_cents,
                entry_decimal_odds=round(pos.entry_decimal_odds, 3),
                hedge_price_cents=price,
                hedge_decimal_odds=round(100.0 / price, 3) if price > 0 else 0,
                entry_stake_cents=pos.entry_stake_cents,
                hedge_stake_cents=size_c,
                total_staked_cents=pos.entry_stake_cents + size_c,
                locked_profit_cents=locked,
                locked_profit_usd=round(locked / 100, 2),
                time_in_trade_s=round(pos.time_in_trade_s, 1),
                strategy=self.name,
            )

        # Stop-loss fill
        elif pos.state in (PositionState.STOPPING, PositionState.ENTERED) \
                and side == Side.NO.value:
            pos.hedge_price_cents = price
            pos.hedge_stake_cents = size_c
            pos.hedge_order_id    = order_id
            pos.state             = PositionState.STOPPED

            stop_recovery  = int(size_c * (100.0 / price)) if price > 0 else 0
            net_loss_cents = pos.entry_stake_cents - stop_recovery

            logger.warning(
                "GreenUp: stop-loss filled — loss capped",
                ticker=ticker,
                entry_price_cents=pos.entry_price_cents,
                stop_fill_price=price,
                entry_stake_cents=pos.entry_stake_cents,
                stop_hedge_cents=size_c,
                stop_recovery_cents=stop_recovery,
                net_loss_cents=net_loss_cents,
                net_loss_usd=round(net_loss_cents / 100, 2),
                time_in_trade_s=round(pos.time_in_trade_s, 1),
                strategy=self.name,
            )

    def add_watch_ticker(self, ticker: str) -> None:
        """Register a ticker to monitor for entry conditions."""
        if ticker not in self._positions:
            self._positions[ticker] = GreenUpPosition(ticker=ticker)
            logger.info("GreenUp: watching ticker", ticker=ticker, strategy=self.name)

    def get_position(self, ticker: str) -> GreenUpPosition | None:
        return self._positions.get(ticker)

    def summary(self) -> list[dict]:
        """Serialisable snapshot of all tracked positions."""
        return [
            {
                "ticker":               ticker,
                "state":                pos.state.value,
                "entry_price_cents":    pos.entry_price_cents,
                "entry_decimal_odds":   round(pos.entry_decimal_odds, 3),
                "entry_stake_cents":    pos.entry_stake_cents,
                "potential_return_c":   pos.potential_return_cents,
                "hedge_price_cents":    pos.hedge_price_cents,
                "hedge_stake_cents":    pos.hedge_stake_cents,
                "locked_profit_cents":  pos.locked_profit_cents,
                "locked_profit_usd":    round(pos.locked_profit_cents / 100, 2),
                "stop_trigger_price":   pos.stop_loss_trigger_price,
                "time_in_trade_s":      round(pos.time_in_trade_s, 1),
            }
            for ticker, pos in self._positions.items()
        ]

    # ── Private: entry ────────────────────────────────────────────────────────

    def _check_entry(self, ticker: str, tick: dict[str, Any]) -> Signal | None:
        """
        Enter a YES position when best_ask <= entry_max_price.

        Sizing: fractional Kelly, treating hedge_trigger_price as the
        implied fair value (conservative; it's where we expect YES to rise).
        """
        best_ask = tick.get("best_ask")
        spread   = tick.get("spread") or 2

        if best_ask is None or best_ask > self._entry_max_price:
            return None

        implied_fair = self._hedge_trigger_price / 100.0
        market_price = best_ask / 100.0
        edge         = implied_fair - market_price

        if edge <= 0:
            return None

        # Fractional Kelly
        b          = (1.0 - market_price) / market_price
        q          = 1.0 - implied_fair
        kelly_full = max((implied_fair * b - q) / b, 0.0)
        kelly_frac = kelly_full / config.KELLY_DIVISOR
        size_cents = min(
            int(kelly_frac * config.MAX_POSITION_CENTS),
            config.MAX_POSITION_CENTS,
        )
        if size_cents < 1:
            return None

        vig_proxy   = max(spread / 2.0, 0.5) / 100.0
        edge_to_vig = edge / vig_proxy

        # Preview the expected hedge at the trigger price
        preview_pos = GreenUpPosition(
            ticker=ticker,
            entry_price_cents=best_ask,
            entry_stake_cents=size_cents,
        )
        no_at_trigger = 100 - self._hedge_trigger_price
        if self._hedge_mode == HedgeMode.FULL_GREEN:
            p_hedge, p_profit = preview_pos.compute_full_green(no_at_trigger)
        elif self._hedge_mode == HedgeMode.STAKE_BACK:
            p_hedge, p_profit = preview_pos.compute_stake_back(no_at_trigger)
        else:
            p_hedge, p_profit = preview_pos.compute_partial(
                no_at_trigger, self._partial_hedge_fraction
            )

        # Register in WATCHING state -> advances to ENTERED on fill
        self._positions[ticker] = GreenUpPosition(
            ticker=ticker,
            entry_stake_cents=size_cents,
            state=PositionState.WATCHING,
        )

        logger.signal_generated(
            ticker=ticker,
            side=Side.YES.value,
            size_cents=size_cents,
            limit_price=best_ask,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            phase="entry",
            entry_decimal_odds=round(100.0 / best_ask, 3),
            hedge_trigger_price=self._hedge_trigger_price,
            preview_hedge_cents=p_hedge,
            preview_locked_profit_cents=p_profit,
            preview_locked_profit_usd=round(p_profit / 100, 2),
            strategy=self.name,
        )

        return Signal(
            ticker=ticker,
            side=Side.YES,
            size_cents=size_cents,
            limit_price=best_ask,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=implied_fair,
            strategy=self.name,
            meta={
                "phase":                        "entry",
                "hedge_mode":                   self._hedge_mode.value,
                "entry_price_cents":            best_ask,
                "entry_decimal_odds":           round(100.0 / best_ask, 3),
                "hedge_trigger_price":          self._hedge_trigger_price,
                "stop_loss_threshold":          self._stop_loss_threshold,
                "kelly_full":                   round(kelly_full, 4),
                "kelly_divisor":                config.KELLY_DIVISOR,
                # Preview of what the hedge will look like
                "preview_no_price":             no_at_trigger,
                "preview_no_decimal_odds":      round(100.0 / no_at_trigger, 3) if no_at_trigger > 0 else 0,
                "preview_hedge_cents":          p_hedge,
                "preview_hedge_usd":            round(p_hedge / 100, 2),
                "preview_locked_profit_cents":  p_profit,
                "preview_locked_profit_usd":    round(p_profit / 100, 2),
            },
        )

    # ── Private: hedge trigger ────────────────────────────────────────────────

    def _check_hedge_trigger(
        self, pos: GreenUpPosition, tick: dict[str, Any]
    ) -> Signal | None:
        """
        Fire the hedge when YES bid >= hedge_trigger_price.

        NO price at hedge = 100 - YES_bid  (Kalshi complement).
        Applies formula 1 or 2 based on hedge_mode.
        """
        best_bid = tick.get("best_bid")
        spread   = tick.get("spread") or 2

        if best_bid is None or best_bid < self._hedge_trigger_price:
            return None

        no_price = 100 - best_bid
        if no_price <= 0 or no_price >= 100:
            return None

        # Apply the selected hedge formula
        if self._hedge_mode == HedgeMode.FULL_GREEN:
            hedge_cents, locked_profit = pos.compute_full_green(no_price)
            mode_label = "full_green [formula 1]"
        elif self._hedge_mode == HedgeMode.STAKE_BACK:
            hedge_cents, locked_profit = pos.compute_stake_back(no_price)
            mode_label = "stake_back [formula 2]"
        else:
            hedge_cents, locked_profit = pos.compute_partial(
                no_price, self._partial_hedge_fraction
            )
            mode_label = f"partial_{self._partial_hedge_fraction}"

        # Safety gates
        if hedge_cents <= 0:
            logger.warning(
                "GreenUp: hedge stake is zero — skipping",
                ticker=pos.ticker,
                no_price=no_price,
                mode=mode_label,
                strategy=self.name,
            )
            return None

        # For FULL_GREEN and PARTIAL, require minimum locked profit
        if self._hedge_mode != HedgeMode.STAKE_BACK \
                and locked_profit < MIN_HEDGE_PROFIT_CENTS:
            logger.info(
                "GreenUp: locked profit below minimum — waiting for better price",
                ticker=pos.ticker,
                locked_profit_cents=locked_profit,
                minimum_cents=MIN_HEDGE_PROFIT_CENTS,
                strategy=self.name,
            )
            return None

        hedge_cents     = min(hedge_cents, config.MAX_POSITION_CENTS)
        no_decimal_odds = round(100.0 / no_price, 3)

        # Edge on NO leg relative to entry (our "fair" NO = 100 - entry_price)
        vig_proxy   = max(spread / 2.0, 0.5) / 100.0
        fair_no     = 100 - pos.entry_price_cents
        hedge_edge  = (fair_no - no_price) / 100.0
        edge_to_vig = hedge_edge / vig_proxy if vig_proxy > 0 else 0.0

        pos.state = PositionState.HEDGING

        logger.signal_generated(
            ticker=pos.ticker,
            side=Side.NO.value,
            size_cents=hedge_cents,
            limit_price=no_price,
            edge=round(hedge_edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            phase="hedge",
            mode=mode_label,
            entry_price_cents=pos.entry_price_cents,
            entry_decimal_odds=round(pos.entry_decimal_odds, 3),
            entry_stake_cents=pos.entry_stake_cents,
            potential_return_cents=pos.potential_return_cents,
            potential_return_usd=round(pos.potential_return_cents / 100, 2),
            no_price_cents=no_price,
            no_decimal_odds=no_decimal_odds,
            hedge_stake_cents=hedge_cents,
            hedge_stake_usd=round(hedge_cents / 100, 2),
            locked_profit_cents=locked_profit,
            locked_profit_usd=round(locked_profit / 100, 2),
            yes_bid_at_hedge=best_bid,
            time_in_trade_s=round(pos.time_in_trade_s, 1),
            strategy=self.name,
        )

        return Signal(
            ticker=pos.ticker,
            side=Side.NO,
            size_cents=hedge_cents,
            limit_price=no_price,
            edge=round(hedge_edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=0.99,   # near-certain locked profit
            strategy=self.name,
            meta={
                "phase":                  "hedge",
                "hedge_mode":             mode_label,
                # Entry leg
                "entry_price_cents":      pos.entry_price_cents,
                "entry_decimal_odds":     round(pos.entry_decimal_odds, 3),
                "entry_stake_cents":      pos.entry_stake_cents,
                "potential_return_cents": pos.potential_return_cents,
                "potential_return_usd":   round(pos.potential_return_cents / 100, 2),
                # Hedge leg
                "no_price_cents":         no_price,
                "no_decimal_odds":        no_decimal_odds,
                "hedge_stake_cents":      hedge_cents,
                "hedge_stake_usd":        round(hedge_cents / 100, 2),
                # P&L summary
                "locked_profit_cents":    locked_profit,
                "locked_profit_usd":      round(locked_profit / 100, 2),
                "total_staked_cents":     pos.entry_stake_cents + hedge_cents,
                "total_staked_usd":       round((pos.entry_stake_cents + hedge_cents) / 100, 2),
                # Context
                "yes_bid_at_hedge":       best_bid,
                "time_in_trade_s":        round(pos.time_in_trade_s, 1),
            },
        )

    # ── Private: stop-loss ────────────────────────────────────────────────────

    def _check_stop_loss(
        self, pos: GreenUpPosition, tick: dict[str, Any]
    ) -> Signal | None:
        """
        Trigger a defensive NO hedge if YES bid falls to the stop level.

        Stop level = entry_price x (1 - stop_loss_threshold)

        Uses formula 2 (stake-back) for the stop — we're trying to recover
        as much of the initial stake as current prices allow. This is a
        defensive exit, not a profit trade.
        """
        best_bid = tick.get("best_bid")
        spread   = tick.get("spread") or 2

        if best_bid is None or best_bid > pos.stop_loss_trigger_price:
            return None

        no_price = 100 - best_bid
        if no_price <= 0 or no_price >= 100:
            return None

        # Formula 2: stake-back for maximum recovery at current prices
        stop_hedge_cents, _ = pos.compute_stake_back(no_price)
        stop_hedge_cents    = max(stop_hedge_cents, 1)
        stop_hedge_cents    = min(stop_hedge_cents, config.MAX_POSITION_CENTS)

        stop_recovery  = int(stop_hedge_cents * (100.0 / no_price)) if no_price > 0 else 0
        net_loss_cents = pos.entry_stake_cents - stop_recovery

        vig_proxy   = max(spread / 2.0, 0.5) / 100.0
        stop_edge   = (pos.entry_price_cents - no_price) / 100.0
        edge_to_vig = stop_edge / vig_proxy if vig_proxy > 0 else 0.0

        pos.state = PositionState.STOPPING

        logger.warning(
            "GreenUp: stop-loss triggered",
            ticker=pos.ticker,
            entry_price_cents=pos.entry_price_cents,
            stop_trigger_price=pos.stop_loss_trigger_price,
            current_bid=best_bid,
            no_price_cents=no_price,
            stop_hedge_cents=stop_hedge_cents,
            est_net_loss_cents=net_loss_cents,
            est_net_loss_usd=round(net_loss_cents / 100, 2),
            time_in_trade_s=round(pos.time_in_trade_s, 1),
            strategy=self.name,
        )

        return Signal(
            ticker=pos.ticker,
            side=Side.NO,
            size_cents=stop_hedge_cents,
            limit_price=no_price,
            edge=round(stop_edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=0.0,   # defensive exit, not an alpha trade
            strategy=self.name,
            meta={
                "phase":                "stop_loss",
                "entry_price_cents":    pos.entry_price_cents,
                "entry_stake_cents":    pos.entry_stake_cents,
                "stop_trigger_price":   pos.stop_loss_trigger_price,
                "current_bid":          best_bid,
                "no_price_cents":       no_price,
                "stop_hedge_cents":     stop_hedge_cents,
                "est_net_loss_cents":   net_loss_cents,
                "est_net_loss_usd":     round(net_loss_cents / 100, 2),
                "stop_loss_threshold":  self._stop_loss_threshold,
                "time_in_trade_s":      round(pos.time_in_trade_s, 1),
            },
        )
