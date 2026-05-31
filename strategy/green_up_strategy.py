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
        stop_loss_cents=10,         # stop when YES bid falls 10c below entry (~10c loss/contract)
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
from strategy.execution_price import (
    EntryPriceMode,
    execution_meta,
    resolve_no_buy,
    resolve_yes_buy,
    resolve_yes_sell_exit,
)
from strategy.price_targets import (
    hedge_trigger_price,
    parse_hedge_offset_cents,
    stop_loss_trigger_price,
)


# ── Module-level tunables ────────────────────────────────────────────────────

DEFAULT_ENTRY_MAX_PRICE: int | None    = 25   # None = no cap; optional underdog filter
DEFAULT_HEDGE_TRIGGER_PRICE: int      = 68    # cents; YES bid must reach this to hedge
DEFAULT_STOP_LOSS_CENTS: int           = 10    # max loss per contract before stop (cents)
DEFAULT_MAX_SPREAD_CENTS: int          = 8     # skip entry when spread exceeds this
DEFAULT_PARTIAL_HEDGE_FRACTION: float = 0.50  # PARTIAL mode: hedge 50% of full-green size
# Consecutive cancel/replace failures against a resting stop before we treat the
# order as gone (already filled or cancelled on the venue) and stop spinning.
MAX_STOP_CANCEL_FAILURES: int          = 3


def resolve_entry_max_price(
    entry_max: int | None,
    *,
    no_entry_max: bool = False,
    env_value: str | None = None,
    default: int | None = DEFAULT_ENTRY_MAX_PRICE,
) -> int | None:
    """
    Resolve optional entry max YES ask (cents).

    ``None`` disables the cap. ``no_entry_max`` or env ``0``/``none``/``off`` also
    disable it.
    """
    if no_entry_max:
        return None
    if entry_max is not None:
        return entry_max
    if env_value is not None:
        stripped = env_value.strip().lower()
        if stripped in ("", "0", "none", "off"):
            return None
        return int(env_value)
    return default


def parse_stop_loss_cents(
    value: float | int | str | None,
    *,
    default: int = DEFAULT_STOP_LOSS_CENTS,
) -> int:
    """
    Parse --stop-loss / KALSHI_GREEN_UP_STOP_LOSS as cents per contract.

    Values in (0, 1) are treated as legacy fractions (e.g. 0.40 → 10c at a
    25c reference entry) and emit a deprecation warning.
    """
    if value is None:
        return default
    v = float(value)
    if 0 < v < 1:
        cents = max(1, min(98, int(round(v * 25))))
        logger.warning(
            "GreenUp: stop-loss fraction is deprecated; use cents per contract "
            "(e.g. --stop-loss 10 instead of 0.40)",
            legacy_fraction=v,
            interpreted_cents=cents,
        )
        return cents
    cents = int(round(v))
    if cents < 1:
        raise ValueError(f"stop-loss must be at least 1 cent, got {value!r}")
    return min(98, cents)


# ── Supporting enums ─────────────────────────────────────────────────────────

class HedgeMode(str, Enum):
    FULL_GREEN = "full_green"  # equal profit on both outcomes  [formula 1]
    STAKE_BACK = "stake_back"  # break-even hedge, keep YES upside  [formula 2]
    PARTIAL    = "partial"     # fraction of full-green, residual upside on YES


class HedgeStyle(str, Enum):
    """When to submit the buy-NO hedge leg."""

    TRIGGER = "trigger"  # wait for YES bid >= hedge trigger, then buy NO (default)
    RESTING = "resting"  # GTC buy NO at trigger price immediately after entry fill


def parse_hedge_style(value: str | None, *, default: HedgeStyle = HedgeStyle.TRIGGER) -> HedgeStyle:
    if value is None:
        return default
    key = value.strip().lower()
    for style in HedgeStyle:
        if style.value == key:
            return style
    raise ValueError(
        f"Unknown hedge style {value!r}. Choose: {', '.join(s.value for s in HedgeStyle)}"
    )


class PositionState(str, Enum):
    SCANNING = "scanning"   # registered ticker, no entry order yet
    WATCHING = "watching"   # entry order sent, awaiting fill
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
    state:  PositionState = PositionState.SCANNING
    cycles_completed: int = 0   # finished entry→hedge/stop round-trips on this ticker

    # Entry leg
    entry_price_cents: int  = 0   # YES price paid (cents, 1-99)
    entry_stake_cents: int  = 0   # dollars staked on YES leg (in cents)
    entry_contracts:   int  = 0   # YES contracts held after entry fill
    entry_order_id:    str  = ""

    # Resting stop-loss order (GTC sell YES)
    stop_order_id:     str  = ""
    stop_limit_price:  int  = 0
    stop_cancel_failures: int = 0   # consecutive cancel/replace failures on the stop

    # Hedge leg
    hedge_price_cents: int  = 0   # NO price paid (cents, 1-99)
    hedge_stake_cents: int  = 0   # dollars staked on NO leg (in cents)
    hedge_order_id:    str  = ""
    hedge_limit_price: int  = 0   # resting GTC buy-NO limit (cents)

    # Risk tracking
    hedge_trigger_price:     int  = 0   # YES bid at/above this fires hedge
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

    Entry:  YES when spread and optional entry_max pass; hedge at entry + offset
            or absolute hedge_trigger_price
    Stop:   YES bid  <= stop_loss_trigger   (market moving against us)
    """

    def __init__(
        self,
        entry_max_price:        int | None = DEFAULT_ENTRY_MAX_PRICE,
        hedge_trigger_price:    int       = DEFAULT_HEDGE_TRIGGER_PRICE,
        hedge_offset_cents:     int | None = None,
        hedge_mode:             HedgeMode = HedgeMode.FULL_GREEN,
        stop_loss_cents:        int       = DEFAULT_STOP_LOSS_CENTS,
        max_spread_cents:       int       = DEFAULT_MAX_SPREAD_CENTS,
        hedge_style:            HedgeStyle = HedgeStyle.TRIGGER,
        partial_hedge_fraction: float     = DEFAULT_PARTIAL_HEDGE_FRACTION,
        entry_price_mode:       EntryPriceMode = EntryPriceMode.PASSIVE,
        exit_price_mode:        EntryPriceMode = EntryPriceMode.PASSIVE,
        limit_offset_cents:     int       = 0,
        max_cycles_per_ticker:  int       = 0,
    ) -> None:
        """
        Args:
            entry_max_price:        Optional max YES ask (cents). None = no cap; use
                                    with relative hedge for fully dynamic entries.
            hedge_trigger_price:    Green up when YES bid >= this (cents) in absolute
                                    mode. Ignored when hedge_offset_cents is set.
            hedge_offset_cents:     Relative mode: hedge when YES bid >= entry + N
                                    cents (computed per fill). None = absolute mode.
            hedge_mode:             Which formula to apply:
                                      FULL_GREEN  equal profit both outcomes [1]
                                      STAKE_BACK  free-bet, keep YES upside  [2]
                                      PARTIAL     fraction of formula-1
            stop_loss_cents:        Stop when YES bid falls this many cents below
                                    entry (max loss per contract ≈ this value).
                                    10 = stop if bid drops 10c below entry price.
            max_spread_cents:       Skip entry when YES spread exceeds this (cents).
            hedge_style:            ``trigger`` waits for YES bid >= hedge level;
                                    ``resting`` posts GTC buy-NO at trigger right
                                    after entry fill.
            partial_hedge_fraction: For PARTIAL mode only — fraction of full-green
                                    stake to place (0.0 to 1.0).
            entry_price_mode:       How to price entry (buy YES): market, limit_at_ask,
                                    limit_at_bid, etc.
            exit_price_mode:        How to price hedge/stop (buy NO).
            limit_offset_cents:     For limit_offset mode on entry/exit legs.
            max_cycles_per_ticker:  Max completed round-trips per ticker (0 = unlimited).
        """
        self._entry_max_price        = entry_max_price
        self._hedge_trigger_price    = hedge_trigger_price
        self._hedge_offset_cents     = hedge_offset_cents
        self._hedge_mode             = hedge_mode
        self._stop_loss_cents        = stop_loss_cents
        self._max_spread_cents       = max_spread_cents
        self._hedge_style            = hedge_style
        self._partial_hedge_fraction = partial_hedge_fraction
        self._entry_price_mode       = entry_price_mode
        self._exit_price_mode        = exit_price_mode
        self._limit_offset           = limit_offset_cents
        self._max_cycles_per_ticker  = max(0, int(max_cycles_per_ticker))

        # ticker -> GreenUpPosition
        self._positions: dict[str, GreenUpPosition] = {}

    @property
    def uses_relative_hedge(self) -> bool:
        return self._hedge_offset_cents is not None

    def hedge_trigger_for_entry(self, entry_price_cents: int) -> int:
        """Expected YES bid level that fires the hedge for a given entry price."""
        if self._hedge_offset_cents is not None:
            return hedge_trigger_price(entry_price_cents, self._hedge_offset_cents)
        return self._hedge_trigger_price

    # ── BaseStrategy interface ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return f"green_up_{self._hedge_mode.value}_{self._entry_price_mode.value}"

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

        if pos and pos.state in (PositionState.HEDGED, PositionState.STOPPED):
            if self._try_begin_new_cycle(pos):
                return self._check_entry(ticker, tick)
            return None

        if pos and pos.state == PositionState.HEDGING:
            if self._hedge_style == HedgeStyle.RESTING:
                stop_sig = self._check_stop_loss(pos, tick)
                if stop_sig:
                    return stop_sig
            return None

        if pos and pos.state == PositionState.STOPPING:
            return self._manage_resting_stop(pos, tick)

        if pos and pos.state == PositionState.ENTERED:
            stop_sig = self._check_stop_loss(pos, tick)
            if stop_sig:
                return stop_sig

            if self._hedge_style == HedgeStyle.RESTING:
                resting_sig = self._emit_resting_hedge(pos, tick)
                if resting_sig:
                    return resting_sig
            else:
                hedge_sig = self._check_hedge_trigger(pos, tick)
                if hedge_sig:
                    return hedge_sig

        if pos is None or pos.state == PositionState.SCANNING:
            return self._check_entry(ticker, tick)

        if pos.state == PositionState.WATCHING:
            return None

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
            contracts = int(fill.get("contracts") or 0)
            if contracts <= 0 and price > 0:
                contracts = max(1, size_c // price)
            pos.entry_price_cents        = price
            pos.entry_stake_cents        = size_c
            pos.entry_contracts          = contracts
            pos.entry_order_id           = order_id
            pos.hedge_trigger_price      = self.hedge_trigger_for_entry(price)
            pos.stop_loss_trigger_price  = stop_loss_trigger_price(
                price, self._stop_loss_cents
            )
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
                hedge_trigger_price=pos.hedge_trigger_price,
                hedge_offset_cents=self._hedge_offset_cents,
                stop_loss_cents=self._stop_loss_cents,
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
            pos.cycles_completed += 1

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

        # Stop-loss fill (GTC sell YES at bid).
        #
        # The protective sell reduces our YES position. Kalshi reports a
        # YES-sell as a fill on the contra ("no") side and frequently omits the
        # ``action`` field, so stop recognition must NOT depend on the reported
        # side — gating on side=="yes" left the position stuck in STOPPING,
        # which spun the resting-stop reprice loop indefinitely and left the
        # trade unrecorded as closed. While STOPPING the resting stop is the
        # only working order, so we match it by order id when known and
        # otherwise accept the fill by state.
        elif pos.state == PositionState.STOPPING:
            if pos.stop_order_id and order_id and order_id != pos.stop_order_id:
                return

            pos.state             = PositionState.STOPPED
            pos.cycles_completed += 1
            pos.stop_order_id     = ""
            pos.stop_limit_price  = 0
            pos.stop_cancel_failures = 0

            net_loss_cents = pos.entry_stake_cents - size_c

            logger.warning(
                "GreenUp: stop-loss filled — loss capped",
                ticker=ticker,
                entry_price_cents=pos.entry_price_cents,
                stop_fill_price=price,
                entry_stake_cents=pos.entry_stake_cents,
                stop_proceeds_cents=size_c,
                net_loss_cents=net_loss_cents,
                net_loss_usd=round(net_loss_cents / 100, 2),
                time_in_trade_s=round(pos.time_in_trade_s, 1),
                strategy=self.name,
            )

    def _try_begin_new_cycle(self, pos: GreenUpPosition) -> bool:
        """
        After HEDGED/STOPPED, allow another entry on this ticker if under the cycle cap.
        Resets position fields but keeps cycles_completed.
        """
        if (
            self._max_cycles_per_ticker > 0
            and pos.cycles_completed >= self._max_cycles_per_ticker
        ):
            if pos.state != PositionState.CLOSED:
                pos.state = PositionState.CLOSED
                logger.info(
                    "GreenUp: max cycles reached — no further entries",
                    ticker=pos.ticker,
                    cycles_completed=pos.cycles_completed,
                    max_cycles=self._max_cycles_per_ticker,
                    strategy=self.name,
                )
            return False

        completed = pos.cycles_completed
        self._positions[pos.ticker] = GreenUpPosition(
            ticker=pos.ticker,
            cycles_completed=completed,
        )
        logger.info(
            "GreenUp: starting new cycle",
            ticker=pos.ticker,
            cycles_completed=completed,
            max_cycles=self._max_cycles_per_ticker or "unlimited",
            strategy=self.name,
        )
        return True

    def add_watch_ticker(self, ticker: str) -> None:
        """Register a ticker to monitor for entry conditions."""
        if ticker not in self._positions:
            self._positions[ticker] = GreenUpPosition(ticker=ticker)
            logger.info("GreenUp: watching ticker", ticker=ticker, strategy=self.name)

    def get_position(self, ticker: str) -> GreenUpPosition | None:
        return self._positions.get(ticker)

    def register_stop_order(
        self, ticker: str, order_id: str, limit_price: int
    ) -> None:
        """Track a resting GTC stop sell after the exchange accepts it."""
        pos = self._positions.get(ticker)
        if pos and pos.state == PositionState.STOPPING:
            pos.stop_order_id        = order_id
            pos.stop_limit_price     = limit_price
            pos.stop_cancel_failures = 0   # fresh order accepted — clear failures

    def rollback_stop(self, ticker: str) -> None:
        """
        Allow stop-loss to retry after a failed submit or cancel.

        If a resting stop is already on the book (the reprice path), keep the
        position in STOPPING and retain ``stop_order_id`` so the next tick
        retries the cancel/replace against the *same* order rather than stacking
        a duplicate sell. Only revert to ENTERED when no stop order was ever
        registered (a first-fire failure), so ``_check_stop_loss`` can re-arm.

        Repeated cancel failures against the same resting stop almost always
        mean the order is already gone (filled or cancelled on the venue);
        retrying forever spins thousands of dead cancels. After
        ``MAX_STOP_CANCEL_FAILURES`` consecutive failures, drop the stale id and
        revert to ENTERED so the stop can re-arm cleanly (or REST fill
        reconciliation can finalise STOPPED).
        """
        pos = self._positions.get(ticker)
        if pos is None or pos.state != PositionState.STOPPING:
            return
        if pos.stop_order_id:
            pos.stop_cancel_failures += 1
            if pos.stop_cancel_failures < MAX_STOP_CANCEL_FAILURES:
                return
            logger.warning(
                "GreenUp: stop cancel kept failing — treating resting stop as gone",
                ticker=ticker,
                stop_order_id=pos.stop_order_id,
                consecutive_failures=pos.stop_cancel_failures,
                strategy=self.name,
            )
            pos.stop_order_id = ""
        pos.state                = PositionState.ENTERED
        pos.stop_limit_price     = 0
        pos.stop_cancel_failures = 0

    def register_hedge_order(
        self, ticker: str, order_id: str, limit_price: int
    ) -> None:
        """Track a resting GTC hedge buy after the exchange accepts it."""
        pos = self._positions.get(ticker)
        if pos and pos.state == PositionState.HEDGING:
            pos.hedge_order_id    = order_id
            pos.hedge_limit_price = limit_price

    def rollback_hedge(self, ticker: str) -> None:
        """Allow resting/trigger hedge to retry after a failed submit."""
        pos = self._positions.get(ticker)
        if pos and pos.state == PositionState.HEDGING:
            pos.state             = PositionState.ENTERED
            pos.hedge_order_id    = ""
            pos.hedge_limit_price = 0

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
                "hedge_trigger_price":  pos.hedge_trigger_price,
                "stop_trigger_price":   pos.stop_loss_trigger_price,
                "time_in_trade_s":      round(pos.time_in_trade_s, 1),
                "cycles_completed":   pos.cycles_completed,
            }
            for ticker, pos in self._positions.items()
        ]

    # ── Private: entry ────────────────────────────────────────────────────────

    def _check_entry(self, ticker: str, tick: dict[str, Any]) -> Signal | None:
        """
        Enter a YES position when book quality and optional entry cap pass.

        Sizing: fractional Kelly using the expected hedge trigger (absolute or
        entry + offset) as implied fair value.
        """
        best_bid = tick.get("best_bid")
        best_ask = tick.get("best_ask")
        spread   = tick.get("spread")
        if spread is None and best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid
        if spread is None:
            spread = 99

        if spread > self._max_spread_cents:
            return None

        if best_ask is None:
            return None

        if self._entry_max_price is not None and best_ask > self._entry_max_price:
            return None

        limit_price, order_type, tif = resolve_yes_buy(
            self._entry_price_mode,
            tick.get("best_bid", best_ask),
            best_ask,
            self._limit_offset,
        )

        trigger_price = self.hedge_trigger_for_entry(limit_price)
        if self._hedge_offset_cents is not None:
            if limit_price + self._hedge_offset_cents > 99:
                return None
        elif trigger_price <= limit_price:
            return None

        implied_fair  = trigger_price / 100.0
        market_price  = best_ask / 100.0
        edge          = implied_fair - market_price

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

        # Kalshi fills whole contracts; the execution layer rounds the stake to
        # contracts = max(size_cents // limit_price, 1). Mirror that here so the
        # logged preview (hedge size, locked profit) and the registered stake
        # match what actually fills — a sub-contract Kelly stake floors to one
        # contract. Reject if a single contract would exceed the position cap.
        contracts  = max(size_cents // limit_price, 1)
        size_cents = contracts * limit_price
        if size_cents > config.MAX_POSITION_CENTS:
            return None

        vig_proxy   = max(spread / 2.0, 0.5) / 100.0
        edge_to_vig = edge / vig_proxy

        # Preview the expected hedge at the trigger price
        preview_pos = GreenUpPosition(
            ticker=ticker,
            entry_price_cents=limit_price,
            entry_stake_cents=size_cents,
        )
        no_at_trigger = 100 - trigger_price
        if self._hedge_mode == HedgeMode.FULL_GREEN:
            p_hedge, p_profit = preview_pos.compute_full_green(no_at_trigger)
        elif self._hedge_mode == HedgeMode.STAKE_BACK:
            p_hedge, p_profit = preview_pos.compute_stake_back(no_at_trigger)
        else:
            p_hedge, p_profit = preview_pos.compute_partial(
                no_at_trigger, self._partial_hedge_fraction
            )

        if p_profit <= 0:
            return None

        # Register in WATCHING state -> advances to ENTERED on fill
        self._positions[ticker] = GreenUpPosition(
            ticker=ticker,
            entry_stake_cents=size_cents,
            entry_contracts=contracts,
            state=PositionState.WATCHING,
        )

        logger.signal_generated(
            ticker=ticker,
            side=Side.YES.value,
            size_cents=size_cents,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            phase="entry",
            entry_price_mode=self._entry_price_mode.value,
            order_type=order_type,
            entry_decimal_odds=round(100.0 / limit_price, 3),
            hedge_trigger_price=trigger_price,
            hedge_offset_cents=self._hedge_offset_cents,
            preview_hedge_cents=p_hedge,
            preview_locked_profit_cents=p_profit,
            preview_locked_profit_usd=round(p_profit / 100, 2),
            strategy=self.name,
        )

        return Signal(
            ticker=ticker,
            side=Side.YES,
            size_cents=size_cents,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=implied_fair,
            strategy=self.name,
            meta={
                **execution_meta(
                    order_type=order_type,
                    time_in_force=tif,
                    price_mode=self._entry_price_mode.value,
                    phase="entry",
                ),
                "hedge_mode":                   self._hedge_mode.value,
                "entry_price_cents":            limit_price,
                "entry_decimal_odds":           round(100.0 / limit_price, 3),
                "hedge_trigger_price":          trigger_price,
                "hedge_offset_cents":           self._hedge_offset_cents,
                "stop_loss_cents":              self._stop_loss_cents,
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

    # ── Private: hedge ────────────────────────────────────────────────────────

    def _resting_no_limit_price(self, pos: GreenUpPosition) -> int:
        """
        GTC buy-NO limit priced at the position's YES hedge trigger.

        Fills when YES bid rises to ``hedge_trigger_price`` (NO ask ≈ 100 - trigger).
        """
        trigger = pos.hedge_trigger_price or self.hedge_trigger_for_entry(
            pos.entry_price_cents
        )
        return max(1, min(99, 100 - trigger))

    def _compute_hedge_stakes(
        self, pos: GreenUpPosition, no_price: int
    ) -> tuple[int, int, str] | None:
        """Return (hedge_cents, locked_profit, mode_label) or None if invalid."""
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

        if hedge_cents <= 0:
            logger.warning(
                "GreenUp: hedge stake is zero — skipping",
                ticker=pos.ticker,
                no_price=no_price,
                mode=mode_label,
                strategy=self.name,
            )
            return None

        return min(hedge_cents, config.MAX_POSITION_CENTS), locked_profit, mode_label

    def _build_hedge_signal(
        self,
        pos: GreenUpPosition,
        tick: dict[str, Any],
        no_price: int,
        order_type: str,
        tif: str,
        *,
        mode_label: str,
        hedge_cents: int,
        locked_profit: int,
        hedge_style: HedgeStyle,
        yes_bid_at_hedge: int | None = None,
    ) -> Signal:
        spread   = tick.get("spread") or 2
        no_decimal_odds = round(100.0 / no_price, 3) if no_price > 0 else 0

        vig_proxy   = max(spread / 2.0, 0.5) / 100.0
        fair_no     = 100 - pos.entry_price_cents
        hedge_edge  = (fair_no - no_price) / 100.0
        edge_to_vig = hedge_edge / vig_proxy if vig_proxy > 0 else 0.0

        if locked_profit < 0:
            logger.info(
                "GreenUp: hedge with negative locked profit preview",
                ticker=pos.ticker,
                locked_profit_cents=locked_profit,
                hedge_stake_cents=hedge_cents,
                yes_bid_at_hedge=yes_bid_at_hedge,
                hedge_style=hedge_style.value,
                strategy=self.name,
            )

        logger.signal_generated(
            ticker=pos.ticker,
            side=Side.NO.value,
            size_cents=hedge_cents,
            limit_price=no_price,
            edge=round(hedge_edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            phase="hedge",
            mode=mode_label,
            hedge_style=hedge_style.value,
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
            yes_bid_at_hedge=yes_bid_at_hedge,
            hedge_trigger_price=pos.hedge_trigger_price,
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
            confidence=0.99,
            strategy=self.name,
            meta={
                **execution_meta(
                    order_type=order_type,
                    time_in_force=tif,
                    price_mode=self._exit_price_mode.value,
                    phase="hedge",
                ),
                "hedge_mode":             mode_label,
                "hedge_style":            hedge_style.value,
                "entry_price_cents":      pos.entry_price_cents,
                "entry_decimal_odds":     round(pos.entry_decimal_odds, 3),
                "entry_stake_cents":      pos.entry_stake_cents,
                "potential_return_cents": pos.potential_return_cents,
                "potential_return_usd":   round(pos.potential_return_cents / 100, 2),
                "no_price_cents":         no_price,
                "no_decimal_odds":        no_decimal_odds,
                "hedge_stake_cents":      hedge_cents,
                "hedge_stake_usd":        round(hedge_cents / 100, 2),
                "locked_profit_cents":    locked_profit,
                "locked_profit_usd":      round(locked_profit / 100, 2),
                "total_staked_cents":     pos.entry_stake_cents + hedge_cents,
                "total_staked_usd":       round((pos.entry_stake_cents + hedge_cents) / 100, 2),
                "yes_bid_at_hedge":       yes_bid_at_hedge,
                "hedge_trigger_price":    pos.hedge_trigger_price,
                "time_in_trade_s":        round(pos.time_in_trade_s, 1),
            },
        )

    def _emit_resting_hedge(
        self, pos: GreenUpPosition, tick: dict[str, Any]
    ) -> Signal | None:
        """
        Post a GTC buy-NO at the trigger-implied price right after entry fill.

        The order rests until YES rises to ``hedge_trigger_price`` (or stop fires).
        """
        if pos.hedge_order_id or pos.state == PositionState.HEDGING:
            return None

        no_price = self._resting_no_limit_price(pos)
        if no_price <= 0 or no_price >= 100:
            return None

        stakes = self._compute_hedge_stakes(pos, no_price)
        if stakes is None:
            return None
        hedge_cents, locked_profit, mode_label = stakes

        pos.state             = PositionState.HEDGING
        pos.hedge_limit_price = no_price

        return self._build_hedge_signal(
            pos,
            tick,
            no_price,
            "limit",
            "gtc",
            mode_label=mode_label,
            hedge_cents=hedge_cents,
            locked_profit=locked_profit,
            hedge_style=HedgeStyle.RESTING,
            yes_bid_at_hedge=tick.get("best_bid"),
        )

    def _check_hedge_trigger(
        self, pos: GreenUpPosition, tick: dict[str, Any]
    ) -> Signal | None:
        """
        Fire the hedge when YES bid >= the position's hedge trigger.

        Trigger is set on entry fill (absolute or entry + offset). NO price at
        hedge = 100 - YES_bid (Kalshi complement). Applies formula 1 or 2.
        """
        best_bid = tick.get("best_bid")

        trigger = pos.hedge_trigger_price or self.hedge_trigger_for_entry(
            pos.entry_price_cents
        )
        if best_bid is None or best_bid < trigger:
            return None

        best_ask = tick.get("best_ask")
        if best_ask is None:
            return None

        no_price, order_type, tif = resolve_no_buy(
            self._exit_price_mode,
            best_bid,
            best_ask,
            self._limit_offset,
        )
        if no_price <= 0 or no_price >= 100:
            return None

        stakes = self._compute_hedge_stakes(pos, no_price)
        if stakes is None:
            return None
        hedge_cents, locked_profit, mode_label = stakes

        pos.state = PositionState.HEDGING

        return self._build_hedge_signal(
            pos,
            tick,
            no_price,
            order_type,
            tif,
            mode_label=mode_label,
            hedge_cents=hedge_cents,
            locked_profit=locked_profit,
            hedge_style=HedgeStyle.TRIGGER,
            yes_bid_at_hedge=best_bid,
        )

    # ── Private: stop-loss ────────────────────────────────────────────────────

    def _entry_contracts(self, pos: GreenUpPosition) -> int:
        if pos.entry_contracts > 0:
            return pos.entry_contracts
        if pos.entry_price_cents > 0 and pos.entry_stake_cents > 0:
            return max(1, pos.entry_stake_cents // pos.entry_price_cents)
        return 1

    def _stop_sell_mode(self) -> EntryPriceMode:
        """
        Pricing mode for the protective stop sell.

        A stop-loss must actually exit the position. In PASSIVE mode the sell
        rests at the ask, so in a fast-falling book no buyer ever lifts it and
        the order chases the market down without filling — the loss is never
        capped. Escalate PASSIVE to CROSS_SPREAD so the stop sells at the bid
        (still a GTC limit) and crosses to get filled. Explicit aggressive or
        maker modes chosen by the operator are left untouched.
        """
        if self._exit_price_mode == EntryPriceMode.PASSIVE:
            return EntryPriceMode.CROSS_SPREAD
        return self._exit_price_mode

    def _manage_resting_stop(
        self, pos: GreenUpPosition, tick: dict[str, Any]
    ) -> Signal | None:
        """Reprice the resting GTC stop sell when the bid moves."""
        if not pos.stop_order_id:
            return None

        best_bid = tick.get("best_bid")
        best_ask = tick.get("best_ask")
        if best_bid is None or best_ask is None:
            return None

        sell_price, order_type, tif = resolve_yes_sell_exit(
            self._stop_sell_mode(),
            best_bid,
            best_ask,
            self._limit_offset,
        )
        if sell_price <= 0 or sell_price >= 100:
            return None

        if pos.stop_order_id and pos.stop_limit_price == sell_price:
            return None

        cancel_id = pos.stop_order_id or None
        return self._build_stop_sell_signal(
            pos,
            tick,
            sell_price,
            order_type,
            tif,
            cancel_order_id=cancel_id,
            is_reprice=True,
        )

    def _check_stop_loss(
        self, pos: GreenUpPosition, tick: dict[str, Any]
    ) -> Signal | None:
        """
        Trigger a defensive YES sell when bid falls to the stop level.

        Stop level = entry_price - stop_loss_cents (YES bid at or below fires stop).

        Exits with a GTC limit sell at the bid (cross-spread price, resting order)
        so the stop remains on book until filled — not a fire-and-forget IOC.
        """
        best_bid = tick.get("best_bid")
        if best_bid is None or best_bid > pos.stop_loss_trigger_price:
            return None

        best_ask = tick.get("best_ask")
        if best_ask is None:
            return None

        sell_price, order_type, tif = resolve_yes_sell_exit(
            self._stop_sell_mode(),
            best_bid,
            best_ask,
            self._limit_offset,
        )
        if sell_price <= 0 or sell_price >= 100:
            return None

        cancel_id = pos.hedge_order_id or None
        if cancel_id:
            pos.hedge_order_id    = ""
            pos.hedge_limit_price = 0

        pos.state = PositionState.STOPPING

        return self._build_stop_sell_signal(
            pos, tick, sell_price, order_type, tif, cancel_order_id=cancel_id
        )

    def _build_stop_sell_signal(
        self,
        pos: GreenUpPosition,
        tick: dict[str, Any],
        sell_price: int,
        order_type: str,
        tif: str,
        *,
        cancel_order_id: str | None = None,
        is_reprice: bool = False,
    ) -> Signal:
        best_bid = tick.get("best_bid")
        spread   = tick.get("spread") or 2
        contracts = self._entry_contracts(pos)
        size_cents = contracts * sell_price
        proceeds_cents = size_cents
        net_loss_cents = pos.entry_stake_cents - proceeds_cents

        vig_proxy   = max(spread / 2.0, 0.5) / 100.0
        stop_edge   = (pos.entry_price_cents - sell_price) / 100.0
        edge_to_vig = stop_edge / vig_proxy if vig_proxy > 0 else 0.0

        # The first stop fire must always log the WARNING, even when it also
        # cancels a resting hedge. Only an actual reprice (bid moved while the
        # stop is already on book) logs the quieter info line.
        if is_reprice:
            logger.info(
                "GreenUp: repricing resting stop sell",
                ticker=pos.ticker,
                old_price_cents=pos.stop_limit_price,
                new_price_cents=sell_price,
                cancel_order_id=cancel_order_id,
                strategy=self.name,
            )
        else:
            logger.warning(
                "GreenUp: stop-loss triggered",
                ticker=pos.ticker,
                entry_price_cents=pos.entry_price_cents,
                stop_loss_cents=self._stop_loss_cents,
                stop_trigger_price=pos.stop_loss_trigger_price,
                current_bid=best_bid,
                sell_price_cents=sell_price,
                entry_contracts=contracts,
                est_proceeds_cents=proceeds_cents,
                est_net_loss_cents=net_loss_cents,
                est_net_loss_usd=round(net_loss_cents / 100, 2),
                cancel_order_id=cancel_order_id,
                time_in_trade_s=round(pos.time_in_trade_s, 1),
                strategy=self.name,
            )

        meta: dict[str, Any] = {
            **execution_meta(
                order_type=order_type,
                time_in_force=tif,
                price_mode=self._exit_price_mode.value,
                phase="stop_loss",
                action="sell",
            ),
            "entry_price_cents":    pos.entry_price_cents,
            "entry_stake_cents":    pos.entry_stake_cents,
            "entry_contracts":      contracts,
            "stop_loss_cents":      self._stop_loss_cents,
            "stop_trigger_price":   pos.stop_loss_trigger_price,
            "current_bid":          best_bid,
            "sell_price_cents":     sell_price,
            "est_proceeds_cents":   proceeds_cents,
            "est_net_loss_cents":   net_loss_cents,
            "est_net_loss_usd":     round(net_loss_cents / 100, 2),
            "time_in_trade_s":      round(pos.time_in_trade_s, 1),
        }
        if cancel_order_id:
            meta["cancel_order_id"] = cancel_order_id

        return Signal(
            ticker=pos.ticker,
            side=Side.YES,
            size_cents=size_cents,
            limit_price=sell_price,
            edge=round(stop_edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=0.0,
            strategy=self.name,
            meta=meta,
        )
