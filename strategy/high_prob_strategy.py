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
from strategy.price_targets import stop_loss_trigger_price

# Re-export for tests and callers that imported from this module.
__all__ = [
    "EntryPriceMode",
    "HighProbStrategy",
    "PostFillMode",
    "TakeProfitStyle",
]


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
DEFAULT_MAX_CYCLES_PER_TICKER: int = 0     # 0 = unlimited re-entry after exit


class PostFillMode(str, Enum):
    HOLD_TO_SETTLEMENT  = "hold"
    RESTING_TAKE_PROFIT = "resting_take_profit"
    RESTING_STOP_LOSS   = "resting_stop"
    TAKE_PROFIT_AND_STOP = "tp_and_stop"


class TakeProfitStyle(str, Enum):
    """How resting take-profit limit price is chosen."""

    FIXED  = "fixed"   # use take_profit_price computed on entry fill (default)
    AT_ASK = "at_ask"  # passive exit: max(computed_tp, current ask)


def parse_take_profit_style(
    value: str | None,
    *,
    default: TakeProfitStyle = TakeProfitStyle.FIXED,
) -> TakeProfitStyle:
    if value is None:
        return default
    key = value.strip().lower()
    for style in TakeProfitStyle:
        if style.value == key:
            return style
    raise ValueError(
        f"Unknown take-profit style {value!r}. "
        f"Choose: {', '.join(s.value for s in TakeProfitStyle)}"
    )


def parse_hp_stop_loss_cents(value: int | str | None) -> int | None:
    """Parse --hp-stop-loss-cents / KALSHI_HP_STOP_LOSS_CENTS."""
    if value is None:
        return None
    cents = int(value)
    if cents < 1:
        raise ValueError(f"hp-stop-loss-cents must be at least 1, got {value!r}")
    return min(98, cents)


class PositionState(str, Enum):
    SCANNING     = "scanning"      # registered ticker, no entry order yet
    WATCHING     = "watching"      # entry signal emitted, awaiting fill
    ENTERED      = "entered"       # long YES, managing exit
    EXIT_PENDING = "exit_pending"  # exit order sent
    CLOSED       = "closed"        # round-trip complete (may re-enter)


@dataclass
class HighProbPosition:
    ticker: str
    state: PositionState = PositionState.SCANNING
    cycles_completed: int = 0

    entry_price_cents: int = 0
    entry_stake_cents: int = 0
    entry_order_id: str = ""
    entry_vig_cents: int = 0

    take_profit_price: int = 0
    stop_loss_trigger: int = 0
    tp_order_sent: bool = False
    stop_order_sent: bool = False
    tp_order_id: str = ""
    tp_limit_price: int = 0
    stop_order_id: str = ""

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
        stop_loss_cents: int | None = None,
        tp_style: TakeProfitStyle = TakeProfitStyle.FIXED,
        max_cycles_per_ticker: int = DEFAULT_MAX_CYCLES_PER_TICKER,
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
        self._stop_loss_cents = stop_loss_cents
        self._tp_style = tp_style
        self._max_cycles_per_ticker = max(0, int(max_cycles_per_ticker))
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

    def _resolve_stop_loss_trigger(self, entry_cents: int) -> int:
        if self._stop_loss_cents is not None:
            return stop_loss_trigger_price(entry_cents, self._stop_loss_cents)
        return max(1, int(entry_cents * (1.0 - self._stop_loss_pct)))

    @property
    def name(self) -> str:
        return f"high_prob_{self._entry_mode.value}"

    def add_watch_ticker(self, ticker: str) -> None:
        self._watch_tickers.add(ticker)
        if ticker not in self._positions:
            self._positions[ticker] = HighProbPosition(ticker=ticker)

    def get_position(self, ticker: str) -> HighProbPosition | None:
        return self._positions.get(ticker)

    def register_tp_order(
        self, ticker: str, order_id: str, limit_price: int
    ) -> None:
        pos = self._positions.get(ticker)
        if pos and pos.state == PositionState.EXIT_PENDING:
            pos.tp_order_id    = order_id
            pos.tp_limit_price = limit_price

    def register_stop_order(self, ticker: str, order_id: str) -> None:
        pos = self._positions.get(ticker)
        if pos and pos.state == PositionState.EXIT_PENDING:
            pos.stop_order_id = order_id

    def rollback_exit(self, ticker: str, phase: str) -> None:
        """Allow TP/stop to retry after a failed submit or circuit-breaker reject."""
        pos = self._positions.get(ticker)
        if pos is None or pos.state != PositionState.EXIT_PENDING:
            return
        if phase == "exit":
            pos.state             = PositionState.ENTERED
            pos.tp_order_sent     = False
            pos.tp_order_id       = ""
            pos.tp_limit_price    = 0
        elif phase == "stop_loss":
            pos.stop_order_sent = False
            pos.stop_order_id   = ""
            if pos.tp_order_id:
                pos.state = PositionState.EXIT_PENDING
            else:
                pos.state = PositionState.ENTERED

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "ticker":              ticker,
                "state":               pos.state.value,
                "entry_price_cents":   pos.entry_price_cents,
                "entry_stake_cents":   pos.entry_stake_cents,
                "take_profit_price":   pos.take_profit_price,
                "stop_trigger_price":  pos.stop_loss_trigger,
                "tp_limit_price":      pos.tp_limit_price,
                "cycles_completed":    pos.cycles_completed,
                "time_in_trade_s":     round(time.monotonic() - pos.entered_at, 1),
            }
            for ticker, pos in self._positions.items()
        ]

    def set_model_probability(self, ticker: str, prob: float) -> None:
        if not 0.01 <= prob <= 0.99:
            raise ValueError(f"Probability must be in [0.01, 0.99], got {prob}")
        self._model_probs[ticker] = prob

    def evaluate(self, tick: dict[str, Any]) -> Signal | None:
        from strategy.book_normalize import normalize_tick_sides

        ticker = tick.get("ticker", "")
        best_bid = tick.get("best_bid")
        best_ask = tick.get("best_ask")
        spread = tick.get("spread")

        if not ticker or best_bid is None:
            return None

        if self._watch_tickers and ticker not in self._watch_tickers:
            return None

        pos = self._positions.get(ticker)
        entry_path = (
            pos is None
            or pos.state in (PositionState.SCANNING, PositionState.WATCHING)
            or pos.state == PositionState.CLOSED
        )
        if entry_path and best_ask is None:
            return None

        work_tick = tick if entry_path else normalize_tick_sides(tick)
        if work_tick is None:
            return None

        best_bid = work_tick.get("best_bid")
        best_ask = work_tick.get("best_ask")
        spread = work_tick.get("spread") if spread is None else spread

        if pos and pos.state == PositionState.CLOSED:
            if self._try_begin_new_cycle(pos):
                return self._check_entry(ticker, work_tick, best_bid, best_ask, spread)
            return None

        if pos and pos.state in (PositionState.ENTERED, PositionState.EXIT_PENDING):
            return self._check_exit(pos, work_tick)

        if pos and pos.state == PositionState.WATCHING:
            return None

        if pos is None or pos.state == PositionState.SCANNING:
            return self._check_entry(ticker, work_tick, best_bid, best_ask, spread)

        return None

    def _try_begin_new_cycle(self, pos: HighProbPosition) -> bool:
        if (
            self._max_cycles_per_ticker > 0
            and pos.cycles_completed >= self._max_cycles_per_ticker
        ):
            logger.info(
                "HighProb: max cycles reached — no further entries",
                ticker=pos.ticker,
                cycles_completed=pos.cycles_completed,
                max_cycles=self._max_cycles_per_ticker,
                strategy=self.name,
            )
            return False

        completed = pos.cycles_completed
        self._positions[pos.ticker] = HighProbPosition(
            ticker=pos.ticker,
            cycles_completed=completed,
        )
        logger.info(
            "HighProb: starting new cycle",
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

        if pos.state == PositionState.WATCHING and side == Side.YES.value:
            pos.entry_price_cents = price
            pos.entry_stake_cents = size_c or self._stake_cents
            pos.entry_order_id = order_id
            pos.take_profit_price = self._resolve_take_profit_price(
                price, pos.entry_vig_cents,
            )
            pos.stop_loss_trigger = self._resolve_stop_loss_trigger(price)
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
                stop_loss_cents=self._stop_loss_cents,
                stop_loss_pct=self._stop_loss_pct,
                strategy=self.name,
            )

        # Exit fill — flatten the YES position.
        #
        # Kalshi may report a YES-sell on the contra ("no") side and often omits
        # ``action``, so the EXIT_PENDING path must not depend on side labels.
        # While an exit is working, match by resting order id when known.
        elif pos.state == PositionState.EXIT_PENDING:
            known_ids = [oid for oid in (pos.tp_order_id, pos.stop_order_id) if oid]
            if known_ids and order_id and order_id not in known_ids:
                return
            self._apply_exit_fill(pos, price)

        elif pos.state == PositionState.ENTERED \
                and side == Side.YES.value \
                and (fill.get("action") == "sell" or fill.get("is_sell")):
            self._apply_exit_fill(pos, price)

    def _apply_exit_fill(self, pos: HighProbPosition, exit_price: int) -> None:
        """Mark round-trip complete after a confirmed exit/stop fill."""
        pos.cycles_completed += 1
        pos.state = PositionState.CLOSED
        pos.tp_order_id    = ""
        pos.tp_limit_price = 0
        pos.stop_order_id  = ""
        pos.tp_order_sent     = False
        pos.stop_order_sent   = False
        logger.info(
            "HighProb: exit filled",
            ticker=pos.ticker,
            exit_price_cents=exit_price,
            entry_price_cents=pos.entry_price_cents,
            cycles_completed=pos.cycles_completed,
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
        if spread is not None and spread > self._max_spread:
            return None

        limit_price, order_type, tif = resolve_yes_buy(
            self._entry_mode, best_bid, best_ask, self._limit_offset,
        )

        if limit_price < self._min_yes_ask or limit_price > self._max_yes_ask:
            return None

        passed, gross_roi, applied_roi = passes_roi_gate(
            limit_price,
            self._min_roi_pct,
            round_trip_fees=self._round_trip_fees,
        )
        if not passed:
            return None

        market_prob = limit_price / 100.0
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

        size_cents = min(self._stake_cents, config.MAX_POSITION_CENTS)
        if size_cents < 1:
            return None

        entry_vig = vig_proxy_cents(spread)
        self._positions[ticker] = HighProbPosition(
            ticker=ticker,
            state=PositionState.WATCHING,
            entry_vig_cents=entry_vig,
        )

        payout_cents = 100 - limit_price
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
                "entry_limit_price":  limit_price,
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

        best_ask = tick.get("best_ask")
        if best_ask is None:
            return None  # caller should pass normalize_tick_sides output

        # Resting take-profit: sell YES at target (passive by default)
        if emit_tp and not pos.tp_order_sent:
            tp_price = pos.take_profit_price
            _, order_type, tif = resolve_yes_sell(
                self._exit_mode, best_bid, best_ask, self._limit_offset,
            )
            if (
                self._tp_style == TakeProfitStyle.AT_ASK
                and self._exit_mode == EntryPriceMode.PASSIVE
            ):
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

        # Stop: sell when bid breaches trigger (cancels resting TP if on book)
        if emit_stop and not pos.stop_order_sent and best_bid <= pos.stop_loss_trigger:
            cancel_id = pos.tp_order_id or None
            if cancel_id:
                pos.tp_order_id    = ""
                pos.tp_limit_price = 0
                pos.tp_order_sent  = False

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
                cancel_order_id=cancel_id,
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
        edge = exit_prob - (pos.entry_price_cents / 100.0)

        log_kw: dict[str, Any] = {
            "ticker": pos.ticker,
            "side": Side.YES.value,
            "size_cents": contracts_value,
            "limit_price": limit_price,
            "phase": phase,
            "exit_reason": reason,
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
            "post_fill_mode":    self._post_fill_mode.value,
        }
        if cancel_order_id:
            meta["cancel_order_id"] = cancel_order_id

        return Signal(
            ticker=pos.ticker,
            side=Side.YES,
            size_cents=contracts_value,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge / vig_proxy, 4) if vig_proxy else 0.0,
            confidence=exit_prob,
            strategy=self.name,
            meta=meta,
        )
