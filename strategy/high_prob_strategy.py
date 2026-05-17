"""
strategy/high_prob_strategy.py

High-probability / low-payout strategy — buy YES when the market already prices
a high chance of resolution, accepting a smaller return per contract in exchange
for a higher win rate.

Typical profile:
  - YES ask 85–96c  →  implied probability 85–96%
  - Max payout if YES wins: (100 - ask) cents per contract
  - ROI if YES wins: (100 - ask) / ask

Entry price modes (EntryPriceMode):
  - passive         — buy at bid, sell at ask (default, maker-friendly)
  - cross_spread    — buy at ask, sell at bid (aggressive limit)
  - market          — IOC market order
  - limit_at_ask / limit_at_bid / limit_at_mid / limit_offset — explicit overrides

Post-fill behaviour (PostFillMode):
  - HOLD_TO_SETTLEMENT     — keep position until market resolves
  - RESTING_TAKE_PROFIT    — after entry fill, place resting sell YES at target
  - RESTING_STOP_LOSS      — after entry fill, place resting sell if bid breaches stop
  - TAKE_PROFIT_AND_STOP   — resting TP plus aggressive stop on breach

Take-profit target (when not holding to settlement):
  - take_profit_pct set  → sell at entry + pct × (entry + vig_proxy), capped at 99¢
  - else                 → sell at entry + take_profit_offset_cents (legacy)
  vig_proxy = max(spread/2, 0.5) cents, frozen at entry signal time.

Signal meta consumed by ExecutionManager:
  - order_type:     "limit" | "market"
  - action:         "buy" | "sell"
  - time_in_force:  "gtc" | "ioc" | "fok"
  - phase:          "entry" | "exit" | "stop_loss"
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import config
from discovery.market_math import (
    gross_roi_if_yes_wins_pct,
    passes_roi_gate,
    round_trip_fees_for_post_fill,
    take_profit_price_cents,
    vig_proxy_cents,
)
from logging_.structured_logger import logger
from strategy.base_strategy import BaseStrategy, Side, Signal
from strategy.execution_price import (
    EntryPriceMode,
    execution_meta,
    resolve_yes_buy,
    resolve_yes_sell,
)

# Re-export for tests and callers that imported from this module.
__all__ = ["EntryPriceMode", "HighProbStrategy", "PostFillMode"]


# ── Defaults (overridable via constructor or env in factory) ─────────────────

DEFAULT_MIN_YES_ASK: int           = 85    # minimum implied P(YES) to enter
DEFAULT_MAX_YES_ASK: int           = 97    # cap entry — avoid 1–3c upside
DEFAULT_MIN_ROI_PCT: float         = 2.0   # minimum (100-ask)/ask * 100
DEFAULT_MAX_SPREAD_CENTS: int      = 8
DEFAULT_STAKE_CENTS: int           = 5_000  # $50 per entry
DEFAULT_TAKE_PROFIT_OFFSET: int    = 3     # sell YES at entry + N cents
DEFAULT_TAKE_PROFIT_PRICE: int     = 99    # hard cap for resting TP
DEFAULT_STOP_LOSS_PCT: float       = 0.12  # exit if bid falls 12% below entry
DEFAULT_LIMIT_OFFSET: int          = 0     # for LIMIT_OFFSET mode


class PostFillMode(str, Enum):
    HOLD_TO_SETTLEMENT  = "hold"
    RESTING_TAKE_PROFIT = "resting_take_profit"
    RESTING_STOP_LOSS   = "resting_stop"
    TAKE_PROFIT_AND_STOP = "tp_and_stop"


class PositionState(str, Enum):
    WATCHING     = "watching"      # entry signal emitted, awaiting fill
    ENTERED      = "entered"       # long YES, managing exit
    EXIT_PENDING = "exit_pending"  # exit order sent
    CLOSED       = "closed"


@dataclass
class HighProbPosition:
    ticker: str
    state: PositionState = PositionState.WATCHING

    entry_price_cents: int = 0
    entry_stake_cents: int = 0
    entry_order_id: str = ""
    entry_vig_cents: int = 0

    take_profit_price: int = 0
    stop_loss_trigger: int = 0
    tp_order_sent: bool = False
    stop_order_sent: bool = False

    entered_at: float = field(default_factory=time.monotonic)


class HighProbStrategy(BaseStrategy):
    """
    Buy YES on contracts where implied probability is high and payout is modest.

    Optional model probabilities (via set_model_probability) tighten entries:
    the model P(YES) must exceed the market ask by min_edge_to_vig × vig.
    """

    def __init__(
        self,
        min_yes_ask: int = DEFAULT_MIN_YES_ASK,
        max_yes_ask: int = DEFAULT_MAX_YES_ASK,
        min_roi_pct: float = DEFAULT_MIN_ROI_PCT,
        max_spread_cents: int = DEFAULT_MAX_SPREAD_CENTS,
        stake_cents: int = DEFAULT_STAKE_CENTS,
        entry_price_mode: EntryPriceMode = EntryPriceMode.PASSIVE,
        exit_price_mode: EntryPriceMode = EntryPriceMode.PASSIVE,
        limit_offset_cents: int = DEFAULT_LIMIT_OFFSET,
        post_fill_mode: PostFillMode = PostFillMode.HOLD_TO_SETTLEMENT,
        take_profit_offset_cents: int = DEFAULT_TAKE_PROFIT_OFFSET,
        take_profit_pct: float | None = None,
        take_profit_price_cap: int = DEFAULT_TAKE_PROFIT_PRICE,
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        require_model_edge: bool = False,
    ) -> None:
        self._min_yes_ask = min_yes_ask
        self._max_yes_ask = max_yes_ask
        self._min_roi_pct = min_roi_pct
        self._max_spread = max_spread_cents
        self._stake_cents = stake_cents
        self._entry_mode = entry_price_mode
        self._exit_mode = exit_price_mode
        self._limit_offset = limit_offset_cents
        self._post_fill_mode = post_fill_mode
        self._tp_offset = take_profit_offset_cents
        self._tp_pct = take_profit_pct
        self._tp_cap = take_profit_price_cap
        self._stop_loss_pct = stop_loss_pct
        self._require_model_edge = require_model_edge
        assume_rt = getattr(config, "HP_ASSUME_ROUND_TRIP_FEES", False)
        self._round_trip_fees = (
            assume_rt
            if assume_rt
            else round_trip_fees_for_post_fill(post_fill_mode.value)
        )

        self._model_probs: dict[str, float] = {}
        self._positions: dict[str, HighProbPosition] = {}
        self._watch_tickers: set[str] = set()

    def _resolve_take_profit_price(
        self, entry_cents: int, vig_cents: int,
    ) -> int:
        if self._tp_pct is not None:
            return take_profit_price_cents(
                entry_cents, vig_cents, self._tp_pct, price_cap=self._tp_cap,
            )
        return min(self._tp_cap, entry_cents + self._tp_offset)

    @property
    def name(self) -> str:
        return f"high_prob_{self._entry_mode.value}"

    def add_watch_ticker(self, ticker: str) -> None:
        self._watch_tickers.add(ticker)

    def set_model_probability(self, ticker: str, prob: float) -> None:
        if not 0.01 <= prob <= 0.99:
            raise ValueError(f"Probability must be in [0.01, 0.99], got {prob}")
        self._model_probs[ticker] = prob

    def evaluate(self, tick: dict[str, Any]) -> Signal | None:
        ticker = tick.get("ticker", "")
        best_bid = tick.get("best_bid")
        best_ask = tick.get("best_ask")
        spread = tick.get("spread")

        if not ticker or best_bid is None or best_ask is None:
            return None

        if self._watch_tickers and ticker not in self._watch_tickers:
            return None

        pos = self._positions.get(ticker)

        if pos and pos.state == PositionState.ENTERED:
            return self._check_exit(pos, tick)

        if pos is None or pos.state == PositionState.WATCHING:
            if pos and pos.state == PositionState.WATCHING:
                return None
            return self._check_entry(ticker, tick, best_bid, best_ask, spread)

        return None

    def on_fill(self, fill: dict[str, Any]) -> None:
        ticker = fill.get("ticker", "")
        side = fill.get("side", "")
        price = int(fill.get("price", 0) or fill.get("yes_price", 0))
        size_c = int(fill.get("size_cents", 0) or fill.get("contracts", 0) * price)
        order_id = fill.get("order_id", "")

        pos = self._positions.get(ticker)
        if pos is None:
            return

        if pos.state == PositionState.WATCHING and side == Side.YES.value:
            pos.entry_price_cents = price
            pos.entry_stake_cents = size_c or self._stake_cents
            pos.entry_order_id = order_id
            pos.take_profit_price = self._resolve_take_profit_price(
                price, pos.entry_vig_cents,
            )
            pos.stop_loss_trigger = max(
                1,
                int(price * (1.0 - self._stop_loss_pct)),
            )
            pos.state = PositionState.ENTERED
            pos.tp_order_sent = False
            pos.stop_order_sent = False

            logger.info(
                "HighProb: entry filled",
                ticker=ticker,
                entry_price_cents=price,
                roi_if_win_pct=round(
                    gross_roi_if_yes_wins_pct(price), 2
                ),
                take_profit_price=pos.take_profit_price,
                take_profit_pct=self._tp_pct,
                entry_vig_cents=pos.entry_vig_cents,
                stop_loss_trigger=pos.stop_loss_trigger,
                strategy=self.name,
            )

        elif pos.state in (PositionState.ENTERED, PositionState.EXIT_PENDING) \
                and side == Side.YES.value \
                and (fill.get("action") == "sell" or fill.get("is_sell")):
            pos.state = PositionState.CLOSED
            logger.info(
                "HighProb: exit filled",
                ticker=ticker,
                exit_price_cents=price,
                entry_price_cents=pos.entry_price_cents,
                strategy=self.name,
            )

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _check_entry(
        self,
        ticker: str,
        tick: dict[str, Any],
        best_bid: int,
        best_ask: int,
        spread: int | None,
    ) -> Signal | None:
        if best_ask < self._min_yes_ask or best_ask > self._max_yes_ask:
            return None

        if spread is not None and spread > self._max_spread:
            return None

        passed, gross_roi, applied_roi = passes_roi_gate(
            best_ask,
            self._min_roi_pct,
            round_trip_fees=self._round_trip_fees,
        )
        if not passed:
            return None

        market_prob = best_ask / 100.0
        model_prob = self._model_probs.get(ticker)
        confidence = model_prob if model_prob is not None else market_prob

        vig_proxy = max((spread or 2) / 2.0, 0.5) / 100.0
        edge = confidence - market_prob

        if self._require_model_edge and model_prob is None:
            return None

        if model_prob is not None:
            if model_prob < self._min_yes_ask / 100.0:
                return None
            edge = model_prob - market_prob
            if edge <= 0:
                return None
            edge_to_vig = edge / vig_proxy
            if edge_to_vig < config.MIN_EDGE_TO_VIG:
                return None
        else:
            edge_to_vig = edge / vig_proxy if vig_proxy > 0 else 0.0

        limit_price, order_type, tif = resolve_yes_buy(
            self._entry_mode, best_bid, best_ask, self._limit_offset,
        )

        size_cents = min(self._stake_cents, config.MAX_POSITION_CENTS)
        if size_cents < 1:
            return None

        entry_vig = vig_proxy_cents(spread)
        self._positions[ticker] = HighProbPosition(
            ticker=ticker,
            state=PositionState.WATCHING,
            entry_vig_cents=entry_vig,
        )

        payout_cents = 100 - best_ask
        logger.signal_generated(
            ticker=ticker,
            side=Side.YES.value,
            size_cents=size_cents,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            phase="entry",
            implied_prob_pct=round(market_prob * 100, 1),
            roi_if_win_pct=round(applied_roi, 2),
            gross_roi_if_win_pct=round(gross_roi, 2),
            entry_mode=self._entry_mode.value,
            strategy=self.name,
        )

        return Signal(
            ticker=ticker,
            side=Side.YES,
            size_cents=size_cents,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=confidence,
            strategy=self.name,
            meta={
                **execution_meta(
                    order_type=order_type,
                    time_in_force=tif,
                    price_mode=self._entry_mode.value,
                    phase="entry",
                ),
                "entry_price_mode":   self._entry_mode.value,
                "post_fill_mode":     self._post_fill_mode.value,
                "take_profit_pct":    self._tp_pct,
                "entry_vig_cents":    entry_vig,
                "implied_prob":       round(market_prob, 4),
                "roi_if_win_pct":     round(applied_roi, 2),
                "gross_roi_if_win_pct": round(gross_roi, 2),
                "fee_adjusted_roi":   getattr(config, "HP_USE_FEE_ADJUSTED_ROI", True),
                "round_trip_fees":    self._round_trip_fees,
                "payout_per_contract_cents": payout_cents,
                "model_prob":         model_prob,
                "best_bid":           best_bid,
                "best_ask":           best_ask,
            },
        )

    # ── Exit ──────────────────────────────────────────────────────────────────

    def _check_exit(
        self, pos: HighProbPosition, tick: dict[str, Any]
    ) -> Signal | None:
        if self._post_fill_mode == PostFillMode.HOLD_TO_SETTLEMENT:
            return None

        best_bid = tick.get("best_bid")
        spread = tick.get("spread") or 2
        if best_bid is None:
            return None

        mode = self._post_fill_mode
        emit_tp = mode in (
            PostFillMode.RESTING_TAKE_PROFIT,
            PostFillMode.TAKE_PROFIT_AND_STOP,
        )
        emit_stop = mode in (
            PostFillMode.RESTING_STOP_LOSS,
            PostFillMode.TAKE_PROFIT_AND_STOP,
        )

        best_ask = tick.get("best_ask")
        if best_ask is None:
            return None

        # Resting take-profit: sell YES at target (passive by default)
        if emit_tp and not pos.tp_order_sent:
            tp_price = pos.take_profit_price
            _, order_type, tif = resolve_yes_sell(
                self._exit_mode, best_bid, best_ask, self._limit_offset,
            )
            if self._exit_mode == EntryPriceMode.PASSIVE:
                tp_price = max(tp_price, best_ask)
            return self._exit_signal(
                pos,
                limit_price=tp_price,
                order_type=order_type,
                time_in_force=tif,
                phase="exit",
                reason="resting_take_profit",
                spread=spread,
                mark_tp_sent=True,
            )

        # Stop: sell when bid breaches trigger
        if emit_stop and not pos.stop_order_sent and best_bid <= pos.stop_loss_trigger:
            stop_mode = (
                EntryPriceMode.CROSS_SPREAD
                if self._exit_mode == EntryPriceMode.PASSIVE
                else self._exit_mode
            )
            limit_price, order_type, tif = resolve_yes_sell(
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
            )

        return None

    def _exit_signal(
        self,
        pos: HighProbPosition,
        limit_price: int,
        order_type: str,
        time_in_force: str,
        phase: str,
        reason: str,
        spread: int,
        *,
        mark_tp_sent: bool = False,
        mark_stop_sent: bool = False,
    ) -> Signal:
        if mark_tp_sent:
            pos.tp_order_sent = True
        if mark_stop_sent:
            pos.stop_order_sent = True
        pos.state = PositionState.EXIT_PENDING

        contracts_value = pos.entry_stake_cents
        vig_proxy = max(spread / 2.0, 0.5) / 100.0
        exit_prob = limit_price / 100.0
        edge = exit_prob - (pos.entry_price_cents / 100.0)

        logger.signal_generated(
            ticker=pos.ticker,
            side=Side.YES.value,
            size_cents=contracts_value,
            limit_price=limit_price,
            phase=phase,
            exit_reason=reason,
            strategy=self.name,
        )

        return Signal(
            ticker=pos.ticker,
            side=Side.YES,
            size_cents=contracts_value,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge / vig_proxy, 4) if vig_proxy else 0.0,
            confidence=exit_prob,
            strategy=self.name,
            meta={
                "phase":           phase,
                "order_type":      order_type,
                "action":          "sell",
                "time_in_force":   time_in_force,
                "exit_reason":     reason,
                "entry_price_cents": pos.entry_price_cents,
                "post_fill_mode":  self._post_fill_mode.value,
            },
        )
