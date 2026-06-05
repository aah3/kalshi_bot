"""
strategy/mean_reversion_strategy.py

Mean-reversion strategy — fade short-term price swings around a rolling mean.

Typical profile:
  - Track rolling mid-price (YES bid/ask midpoint) per ticker.
  - LONG:  buy YES when mid drops below mean − deviation (dip), sell on revert.
  - SHORT: buy NO when mid spikes above mean + deviation (fade rally), sell NO on revert.
  - Requires minimum rolling volatility so entries target active, oscillating markets.

Entry filters mirror green_up (spread cap, optional entry_max for longs) plus a
volatility floor. Exits use resting take-profit toward the mean (or fixed offset)
with an optional stop if price continues away from the entry.

Signal meta consumed by ExecutionManager:
  - order_type:     "limit" | "market"
  - action:         "buy" | "sell"
  - time_in_force:  "gtc" | "ioc" | "fok"
  - phase:          "entry" | "exit" | "stop_loss"
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import config
from discovery.market_math import vig_proxy_cents
from logging_.structured_logger import logger
from strategy.base_strategy import BaseStrategy, Side, Signal
from strategy.execution_price import (
    EntryPriceMode,
    execution_meta,
    resolve_no_buy,
    resolve_no_sell_exit,
    resolve_yes_buy,
    resolve_yes_sell_exit,
)
from strategy.price_targets import stop_loss_trigger_price

__all__ = [
    "EntryPriceMode",
    "ExitTarget",
    "MeanReversionStrategy",
    "PostFillMode",
    "PositionState",
    "TradeDirection",
    "parse_mr_stop_loss_cents",
]


# ── Defaults (overridable via constructor or env in factory) ─────────────────

DEFAULT_LOOKBACK_TICKS: int        = 20
DEFAULT_MIN_SAMPLES: int            = 10
DEFAULT_ENTRY_DEVIATION_CENTS: int  = 5
DEFAULT_TAKE_PROFIT_OFFSET: int     = 5
DEFAULT_TAKE_PROFIT_CAP: int        = 99
DEFAULT_STOP_LOSS_CENTS: int        = 10
DEFAULT_MIN_VOLATILITY_CENTS: float = 4.0
DEFAULT_ENTRY_MAX_PRICE: int        = 45
DEFAULT_ENTRY_MIN_PRICE: int        = 10
DEFAULT_SHORT_MIN_YES_ASK: int        = 55
DEFAULT_SHORT_MAX_YES_ASK: int        = 90
DEFAULT_MAX_SPREAD_CENTS: int         = 8
DEFAULT_STAKE_CENTS: int              = 5_000
DEFAULT_LIMIT_OFFSET: int             = 0
DEFAULT_MAX_CYCLES_PER_TICKER: int    = 0


class TradeDirection(str, Enum):
    """Which mean-reversion legs to run when flat."""

    LONG  = "long"   # buy dips, sell on revert
    SHORT = "short"  # fade spikes via buy-NO, sell NO on revert
    BOTH  = "both"   # take whichever deviation signal is stronger


class ExitTarget(str, Enum):
    """How resting take-profit limit is chosen."""

    MEAN   = "mean"    # revert toward rolling mean
    OFFSET = "offset"  # fixed offset from entry
    MAX    = "max"     # max(mean target, offset target) — default


class PostFillMode(str, Enum):
    HOLD_TO_SETTLEMENT   = "hold"
    RESTING_TAKE_PROFIT  = "resting_take_profit"
    RESTING_STOP_LOSS    = "resting_stop"
    TAKE_PROFIT_AND_STOP = "tp_and_stop"


class PositionState(str, Enum):
    SCANNING     = "scanning"
    WATCHING     = "watching"
    ENTERED      = "entered"
    EXIT_PENDING = "exit_pending"
    CLOSED       = "closed"


@dataclass
class MeanReversionPosition:
    ticker: str
    state: PositionState = PositionState.SCANNING
    direction: TradeDirection = TradeDirection.LONG
    cycles_completed: int = 0

    entry_price_cents: int = 0
    entry_stake_cents: int = 0
    entry_order_id: str = ""
    entry_side: Side = Side.YES
    rolling_mean_at_entry: float = 0.0

    take_profit_price: int = 0
    stop_loss_trigger: int = 0
    tp_order_sent: bool = False
    stop_order_sent: bool = False
    tp_order_id: str = ""
    tp_limit_price: int = 0
    stop_order_id: str = ""

    entered_at: float = field(default_factory=time.monotonic)


def parse_mr_stop_loss_cents(value: int | str | None) -> int | None:
    """Parse --mr-stop-loss-cents / KALSHI_MR_STOP_LOSS_CENTS."""
    if value is None:
        return None
    cents = int(value)
    if cents < 1:
        raise ValueError(f"mr-stop-loss-cents must be at least 1, got {value!r}")
    return min(98, cents)


def parse_trade_direction(value: str | None, *, default: TradeDirection = TradeDirection.BOTH) -> TradeDirection:
    if value is None:
        return default
    key = value.strip().lower()
    for direction in TradeDirection:
        if direction.value == key:
            return direction
    raise ValueError(
        f"Unknown trade direction {value!r}. "
        f"Choose: {', '.join(d.value for d in TradeDirection)}"
    )


def parse_exit_target(value: str | None, *, default: ExitTarget = ExitTarget.MAX) -> ExitTarget:
    if value is None:
        return default
    key = value.strip().lower()
    for target in ExitTarget:
        if target.value == key:
            return target
    raise ValueError(
        f"Unknown exit target {value!r}. "
        f"Choose: {', '.join(t.value for t in ExitTarget)}"
    )


def _rolling_stats(prices: deque[int]) -> tuple[float, float]:
    if len(prices) < 2:
        return 0.0, 0.0
    mean = sum(prices) / len(prices)
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    return mean, math.sqrt(variance)


class MeanReversionStrategy(BaseStrategy):
    """
    Fade short-term YES price swings around a rolling mean.

    Complements green_up (cheap underdog + hedge) and high_prob (high implied
    probability hold) by capturing round-trip volatility in oscillating markets.
    """

    def __init__(
        self,
        lookback_ticks: int = DEFAULT_LOOKBACK_TICKS,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        entry_deviation_cents: int = DEFAULT_ENTRY_DEVIATION_CENTS,
        take_profit_offset_cents: int = DEFAULT_TAKE_PROFIT_OFFSET,
        take_profit_cap: int = DEFAULT_TAKE_PROFIT_CAP,
        exit_target: ExitTarget = ExitTarget.MAX,
        stop_loss_cents: int | None = DEFAULT_STOP_LOSS_CENTS,
        min_volatility_cents: float = DEFAULT_MIN_VOLATILITY_CENTS,
        entry_max_price: int = DEFAULT_ENTRY_MAX_PRICE,
        entry_min_price: int = DEFAULT_ENTRY_MIN_PRICE,
        short_min_yes_ask: int = DEFAULT_SHORT_MIN_YES_ASK,
        short_max_yes_ask: int = DEFAULT_SHORT_MAX_YES_ASK,
        max_spread_cents: int = DEFAULT_MAX_SPREAD_CENTS,
        stake_cents: int = DEFAULT_STAKE_CENTS,
        trade_direction: TradeDirection = TradeDirection.BOTH,
        entry_price_mode: EntryPriceMode = EntryPriceMode.PASSIVE,
        exit_price_mode: EntryPriceMode = EntryPriceMode.PASSIVE,
        limit_offset_cents: int = DEFAULT_LIMIT_OFFSET,
        post_fill_mode: PostFillMode = PostFillMode.TAKE_PROFIT_AND_STOP,
        max_cycles_per_ticker: int = DEFAULT_MAX_CYCLES_PER_TICKER,
    ) -> None:
        self._lookback = max(2, int(lookback_ticks))
        self._min_samples = max(2, min(int(min_samples), self._lookback))
        self._entry_deviation = max(1, int(entry_deviation_cents))
        self._tp_offset = max(1, int(take_profit_offset_cents))
        self._tp_cap = min(99, max(1, int(take_profit_cap)))
        self._exit_target = exit_target
        self._stop_loss_cents = stop_loss_cents
        self._min_volatility = max(0.0, float(min_volatility_cents))
        self._entry_max = entry_max_price
        self._entry_min = entry_min_price
        self._short_min = short_min_yes_ask
        self._short_max = short_max_yes_ask
        self._max_spread = max_spread_cents
        self._stake_cents = stake_cents
        self._trade_direction = trade_direction
        self._entry_mode = entry_price_mode
        self._exit_mode = exit_price_mode
        self._limit_offset = limit_offset_cents
        self._post_fill_mode = post_fill_mode
        self._max_cycles_per_ticker = max(0, int(max_cycles_per_ticker))

        self._price_history: dict[str, deque[int]] = {}
        self._positions: dict[str, MeanReversionPosition] = {}
        self._watch_tickers: set[str] = set()

    @property
    def name(self) -> str:
        return f"mean_reversion_{self._trade_direction.value}_{self._entry_mode.value}"

    def add_watch_ticker(self, ticker: str) -> None:
        self._watch_tickers.add(ticker)
        if ticker not in self._positions:
            self._positions[ticker] = MeanReversionPosition(ticker=ticker)
        if ticker not in self._price_history:
            self._price_history[ticker] = deque(maxlen=self._lookback)

    def get_position(self, ticker: str) -> MeanReversionPosition | None:
        return self._positions.get(ticker)

    def register_tp_order(
        self, ticker: str, order_id: str, limit_price: int
    ) -> None:
        pos = self._positions.get(ticker)
        if pos and pos.state == PositionState.EXIT_PENDING:
            pos.tp_order_id = order_id
            pos.tp_limit_price = limit_price

    def register_stop_order(self, ticker: str, order_id: str) -> None:
        pos = self._positions.get(ticker)
        if pos and pos.state == PositionState.EXIT_PENDING:
            pos.stop_order_id = order_id

    def rollback_exit(self, ticker: str, phase: str) -> None:
        pos = self._positions.get(ticker)
        if pos is None or pos.state != PositionState.EXIT_PENDING:
            return
        if phase == "exit":
            pos.state = PositionState.ENTERED
            pos.tp_order_sent = False
            pos.tp_order_id = ""
            pos.tp_limit_price = 0
        elif phase == "stop_loss":
            pos.stop_order_sent = False
            pos.stop_order_id = ""
            if pos.tp_order_id:
                pos.state = PositionState.EXIT_PENDING
            else:
                pos.state = PositionState.ENTERED

    def summary(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ticker, pos in self._positions.items():
            hist = self._price_history.get(ticker, deque())
            mean, std = _rolling_stats(hist)
            out.append({
                "ticker":              ticker,
                "state":               pos.state.value,
                "direction":           pos.direction.value,
                "entry_price_cents":   pos.entry_price_cents,
                "entry_stake_cents":   pos.entry_stake_cents,
                "take_profit_price":   pos.take_profit_price,
                "stop_trigger_price":  pos.stop_loss_trigger,
                "rolling_mean":        round(mean, 2),
                "rolling_volatility":  round(std, 2),
                "samples":             len(hist),
                "cycles_completed":    pos.cycles_completed,
                "time_in_trade_s":     round(time.monotonic() - pos.entered_at, 1),
            })
        return out

    def evaluate(self, tick: dict[str, Any]) -> Signal | None:
        ticker = tick.get("ticker", "")
        best_bid = tick.get("best_bid")
        best_ask = tick.get("best_ask")
        spread = tick.get("spread")

        if not ticker or best_bid is None or best_ask is None:
            return None

        if self._watch_tickers and ticker not in self._watch_tickers:
            return None

        self._record_mid(ticker, best_bid, best_ask)

        pos = self._positions.get(ticker)

        if pos and pos.state == PositionState.CLOSED:
            if self._try_begin_new_cycle(pos):
                return self._check_entry(ticker, tick, best_bid, best_ask, spread)
            return None

        if pos and pos.state in (PositionState.ENTERED, PositionState.EXIT_PENDING):
            return self._check_exit(pos, tick)

        if pos and pos.state == PositionState.WATCHING:
            return None

        if pos is None or pos.state == PositionState.SCANNING:
            return self._check_entry(ticker, tick, best_bid, best_ask, spread)

        return None

    def _record_mid(self, ticker: str, best_bid: int, best_ask: int) -> None:
        if ticker not in self._price_history:
            self._price_history[ticker] = deque(maxlen=self._lookback)
        self._price_history[ticker].append((best_bid + best_ask) // 2)

    def _market_stats(self, ticker: str) -> tuple[float, float] | None:
        hist = self._price_history.get(ticker)
        if not hist or len(hist) < self._min_samples:
            return None
        mean, std = _rolling_stats(hist)
        if std < self._min_volatility:
            return None
        return mean, std

    def _try_begin_new_cycle(self, pos: MeanReversionPosition) -> bool:
        if (
            self._max_cycles_per_ticker > 0
            and pos.cycles_completed >= self._max_cycles_per_ticker
        ):
            logger.info(
                "MeanRev: max cycles reached — no further entries",
                ticker=pos.ticker,
                cycles_completed=pos.cycles_completed,
                max_cycles=self._max_cycles_per_ticker,
                strategy=self.name,
            )
            return False

        completed = pos.cycles_completed
        self._positions[pos.ticker] = MeanReversionPosition(
            ticker=pos.ticker,
            cycles_completed=completed,
        )
        logger.info(
            "MeanRev: starting new cycle",
            ticker=pos.ticker,
            cycles_completed=completed,
            max_cycles=self._max_cycles_per_ticker or "unlimited",
            strategy=self.name,
        )
        return True

    def on_fill(self, fill: dict[str, Any]) -> None:
        ticker = fill.get("ticker", "")
        side = fill.get("side", "")
        price = int(fill.get("price", 0) or fill.get("yes_price", 0))
        size_c = int(fill.get("size_cents", 0) or fill.get("contracts", 0) * price)
        order_id = fill.get("order_id", "")

        pos = self._positions.get(ticker)
        if pos is None:
            return

        if pos.state == PositionState.WATCHING:
            expected = pos.entry_side.value
            if side != expected:
                return
            pos.entry_price_cents = price
            pos.entry_stake_cents = size_c or self._stake_cents
            pos.entry_order_id = order_id
            stats = self._market_stats(ticker)
            pos.rolling_mean_at_entry = stats[0] if stats else float(price)
            pos.take_profit_price = self._resolve_take_profit_price(pos)
            pos.stop_loss_trigger = self._resolve_stop_loss_trigger(pos)
            pos.state = PositionState.ENTERED
            pos.tp_order_sent = False
            pos.stop_order_sent = False

            logger.info(
                "MeanRev: entry filled",
                ticker=ticker,
                direction=pos.direction.value,
                side=side,
                entry_price_cents=price,
                rolling_mean=round(pos.rolling_mean_at_entry, 2),
                take_profit_price=pos.take_profit_price,
                stop_loss_trigger=pos.stop_loss_trigger,
                strategy=self.name,
            )

        elif pos.state == PositionState.EXIT_PENDING:
            known_ids = [oid for oid in (pos.tp_order_id, pos.stop_order_id) if oid]
            if known_ids and order_id and order_id not in known_ids:
                return
            self._apply_exit_fill(pos, price)

        elif pos.state == PositionState.ENTERED:
            action = fill.get("action")
            is_sell = fill.get("is_sell") or action == "sell"
            if is_sell and side == pos.entry_side.value:
                self._apply_exit_fill(pos, price)

    def _apply_exit_fill(self, pos: MeanReversionPosition, exit_price: int) -> None:
        pos.cycles_completed += 1
        pos.state = PositionState.CLOSED
        pos.tp_order_id = ""
        pos.tp_limit_price = 0
        pos.stop_order_id = ""
        pos.tp_order_sent = False
        pos.stop_order_sent = False
        logger.info(
            "MeanRev: exit filled",
            ticker=pos.ticker,
            direction=pos.direction.value,
            exit_price_cents=exit_price,
            entry_price_cents=pos.entry_price_cents,
            cycles_completed=pos.cycles_completed,
            strategy=self.name,
        )

    def _resolve_take_profit_price(self, pos: MeanReversionPosition) -> int:
        mean = pos.rolling_mean_at_entry
        entry = pos.entry_price_cents

        if pos.direction == TradeDirection.LONG:
            mean_target = int(round(mean))
            offset_target = entry + self._tp_offset
            if self._exit_target == ExitTarget.MEAN:
                target = mean_target
            elif self._exit_target == ExitTarget.OFFSET:
                target = offset_target
            else:
                target = max(mean_target, offset_target)
            return min(self._tp_cap, max(entry + 1, target))

        mean_no = int(round(100.0 - mean))
        offset_target = entry + self._tp_offset
        if self._exit_target == ExitTarget.MEAN:
            target = mean_no
        elif self._exit_target == ExitTarget.OFFSET:
            target = offset_target
        else:
            target = max(mean_no, offset_target)
        return min(self._tp_cap, max(entry + 1, target))

    def _resolve_stop_loss_trigger(self, pos: MeanReversionPosition) -> int:
        entry = pos.entry_price_cents
        if self._stop_loss_cents is None:
            return 1
        if pos.direction == TradeDirection.LONG:
            return stop_loss_trigger_price(entry, self._stop_loss_cents)
        return max(1, entry - self._stop_loss_cents)

    def _check_entry(
        self,
        ticker: str,
        tick: dict[str, Any],
        best_bid: int,
        best_ask: int,
        spread: int | None,
    ) -> Signal | None:
        if spread is not None and spread > self._max_spread:
            return None

        stats = self._market_stats(ticker)
        if stats is None:
            return None
        mean, std = stats
        mid = (best_bid + best_ask) // 2

        long_signal = self._long_entry_ok(mid, mean, best_ask)
        short_signal = self._short_entry_ok(mid, mean, best_ask)

        direction: TradeDirection | None = None
        if self._trade_direction == TradeDirection.LONG:
            direction = TradeDirection.LONG if long_signal else None
        elif self._trade_direction == TradeDirection.SHORT:
            direction = TradeDirection.SHORT if short_signal else None
        else:
            if long_signal and short_signal:
                long_dev = mean - mid
                short_dev = mid - mean
                direction = (
                    TradeDirection.LONG if long_dev >= short_dev else TradeDirection.SHORT
                )
            elif long_signal:
                direction = TradeDirection.LONG
            elif short_signal:
                direction = TradeDirection.SHORT

        if direction is None:
            return None

        if direction == TradeDirection.LONG:
            limit_price, order_type, tif = resolve_yes_buy(
                self._entry_mode, best_bid, best_ask, self._limit_offset,
            )
            side = Side.YES
        else:
            limit_price, order_type, tif = resolve_no_buy(
                self._entry_mode, best_bid, best_ask, self._limit_offset,
            )
            side = Side.NO

        size_cents = min(self._stake_cents, config.MAX_POSITION_CENTS)
        if size_cents < 1:
            return None

        vig_proxy = max((spread or 2) / 2.0, 0.5) / 100.0
        edge = abs(mean - mid) / 100.0
        edge_to_vig = edge / vig_proxy if vig_proxy > 0 else 0.0

        self._positions[ticker] = MeanReversionPosition(
            ticker=ticker,
            state=PositionState.WATCHING,
            direction=direction,
            entry_side=side,
        )

        logger.signal_generated(
            ticker=ticker,
            side=side.value,
            size_cents=size_cents,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            phase="entry",
            direction=direction.value,
            rolling_mean=round(mean, 2),
            rolling_volatility=round(std, 2),
            mid_price=mid,
            entry_mode=self._entry_mode.value,
            strategy=self.name,
        )

        return Signal(
            ticker=ticker,
            side=side,
            size_cents=size_cents,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=mid / 100.0,
            strategy=self.name,
            meta={
                **execution_meta(
                    order_type=order_type,
                    time_in_force=tif,
                    price_mode=self._entry_mode.value,
                    phase="entry",
                ),
                "direction":          direction.value,
                "rolling_mean":       round(mean, 2),
                "rolling_volatility": round(std, 2),
                "entry_deviation":    self._entry_deviation,
                "post_fill_mode":     self._post_fill_mode.value,
                "best_bid":           best_bid,
                "best_ask":           best_ask,
            },
        )

    def _long_entry_ok(self, mid: float, mean: float, yes_ask: int) -> bool:
        if mid > mean - self._entry_deviation:
            return False
        if yes_ask > self._entry_max:
            return False
        if yes_ask < self._entry_min:
            return False
        return True

    def _short_entry_ok(self, mid: float, mean: float, yes_ask: int) -> bool:
        if mid < mean + self._entry_deviation:
            return False
        if yes_ask < self._short_min:
            return False
        if yes_ask > self._short_max:
            return False
        return True

    def _check_exit(
        self, pos: MeanReversionPosition, tick: dict[str, Any]
    ) -> Signal | None:
        if self._post_fill_mode == PostFillMode.HOLD_TO_SETTLEMENT:
            return None

        best_bid = tick.get("best_bid")
        best_ask = tick.get("best_ask")
        spread = tick.get("spread") or 2
        if best_bid is None or best_ask is None:
            return None

        mode = self._post_fill_mode
        emit_tp = (
            pos.state == PositionState.ENTERED
            and mode in (
                PostFillMode.RESTING_TAKE_PROFIT,
                PostFillMode.TAKE_PROFIT_AND_STOP,
            )
        )
        emit_stop = mode in (
            PostFillMode.RESTING_STOP_LOSS,
            PostFillMode.TAKE_PROFIT_AND_STOP,
        )

        if pos.direction == TradeDirection.LONG:
            if emit_tp and not pos.tp_order_sent:
                _, order_type, tif = resolve_yes_sell_exit(
                    self._exit_mode, best_bid, best_ask, self._limit_offset,
                )
                return self._exit_signal(
                    pos,
                    limit_price=pos.take_profit_price,
                    order_type=order_type,
                    time_in_force=tif,
                    phase="exit",
                    reason="resting_take_profit",
                    spread=spread,
                    mark_tp_sent=True,
                )

            if (
                emit_stop
                and not pos.stop_order_sent
                and best_bid <= pos.stop_loss_trigger
            ):
                cancel_id = pos.tp_order_id or None
                if cancel_id:
                    pos.tp_order_id = ""
                    pos.tp_limit_price = 0
                    pos.tp_order_sent = False

                stop_mode = (
                    EntryPriceMode.CROSS_SPREAD
                    if self._exit_mode == EntryPriceMode.PASSIVE
                    else self._exit_mode
                )
                limit_price, order_type, tif = resolve_yes_sell_exit(
                    stop_mode, best_bid, best_ask, self._limit_offset,
                )
                return self._exit_signal(
                    pos,
                    limit_price=limit_price,
                    order_type=order_type,
                    time_in_force=tif,
                    phase="stop_loss",
                    reason="stop_loss",
                    spread=spread,
                    mark_stop_sent=True,
                    cancel_order_id=cancel_id,
                )
            return None

        no_bid = 100 - best_ask
        if emit_tp and not pos.tp_order_sent:
            _, order_type, tif = resolve_no_sell_exit(
                self._exit_mode, best_bid, best_ask, self._limit_offset,
            )
            return self._exit_signal(
                pos,
                limit_price=pos.take_profit_price,
                order_type=order_type,
                time_in_force=tif,
                phase="exit",
                reason="resting_take_profit",
                spread=spread,
                mark_tp_sent=True,
            )

        if emit_stop and not pos.stop_order_sent and no_bid <= pos.stop_loss_trigger:
            cancel_id = pos.tp_order_id or None
            if cancel_id:
                pos.tp_order_id = ""
                pos.tp_limit_price = 0
                pos.tp_order_sent = False

            stop_mode = (
                EntryPriceMode.CROSS_SPREAD
                if self._exit_mode == EntryPriceMode.PASSIVE
                else self._exit_mode
            )
            limit_price, order_type, tif = resolve_no_sell_exit(
                stop_mode, best_bid, best_ask, self._limit_offset,
            )
            return self._exit_signal(
                pos,
                limit_price=limit_price,
                order_type=order_type,
                time_in_force=tif,
                phase="stop_loss",
                reason="stop_loss",
                spread=spread,
                mark_stop_sent=True,
                cancel_order_id=cancel_id,
            )

        return None

    def _exit_signal(
        self,
        pos: MeanReversionPosition,
        limit_price: int,
        order_type: str,
        time_in_force: str,
        phase: str,
        reason: str,
        spread: int,
        *,
        mark_tp_sent: bool = False,
        mark_stop_sent: bool = False,
        cancel_order_id: str | None = None,
    ) -> Signal:
        if mark_tp_sent:
            pos.tp_order_sent = True
        if mark_stop_sent:
            pos.stop_order_sent = True
        pos.state = PositionState.EXIT_PENDING

        contracts_value = pos.entry_stake_cents
        vig_proxy = max(spread / 2.0, 0.5) / 100.0
        exit_prob = limit_price / 100.0
        entry_prob = pos.entry_price_cents / 100.0
        edge = exit_prob - entry_prob if pos.direction == TradeDirection.LONG else entry_prob - exit_prob

        log_kw: dict[str, Any] = {
            "ticker": pos.ticker,
            "side": pos.entry_side.value,
            "size_cents": contracts_value,
            "limit_price": limit_price,
            "phase": phase,
            "exit_reason": reason,
            "direction": pos.direction.value,
            "strategy": self.name,
        }
        if cancel_order_id:
            log_kw["cancel_order_id"] = cancel_order_id
        logger.signal_generated(**log_kw)

        meta: dict[str, Any] = {
            "phase":             phase,
            "order_type":        order_type,
            "action":            "sell",
            "time_in_force":     time_in_force,
            "exit_reason":       reason,
            "entry_price_cents": pos.entry_price_cents,
            "direction":         pos.direction.value,
            "post_fill_mode":    self._post_fill_mode.value,
        }
        if cancel_order_id:
            meta["cancel_order_id"] = cancel_order_id

        return Signal(
            ticker=pos.ticker,
            side=pos.entry_side,
            size_cents=contracts_value,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge / vig_proxy, 4) if vig_proxy else 0.0,
            confidence=exit_prob,
            strategy=self.name,
            meta=meta,
        )
