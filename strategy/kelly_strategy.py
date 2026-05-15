"""
strategy/kelly_strategy.py

Fractional Kelly strategy with edge-to-vig gating.

Kelly criterion for binary bets:
    f* = (p * b - q) / b

Where:
    p  = model probability of YES winning
    q  = 1 - p
    b  = net odds (payout / cost - 1)

For a Kalshi YES contract bought at price `ask` cents:
    cost   = ask / 100
    payout = 1.0 (if YES wins)
    b      = (1 - cost) / cost  = (100 - ask) / ask

Fractional Kelly:
    position_fraction = f* / KELLY_DIVISOR

Edge-to-vig gate:
    vig_proxy = spread / 2          (half-spread approximates the exchange's take)
    edge      = p - ask / 100       (model probability minus market-implied probability)
    edge_to_vig = edge / vig_proxy  must exceed MIN_EDGE_TO_VIG before trading
"""

from typing import Any

import config
from strategy.base_strategy import BaseStrategy, Side, Signal


class KellyStrategy(BaseStrategy):
    """
    Trades when the model probability diverges from the market-implied probability
    by more than the configured MIN_EDGE_TO_VIG threshold.

    The model probability must be supplied externally via `set_model_probability`.
    In a real system this would come from a calibrated ML model or research signal.
    """

    def __init__(self, model_probabilities: dict[str, float] | None = None) -> None:
        """
        Args:
            model_probabilities: Optional seed dict mapping ticker -> P(YES wins).
                                 Update live with set_model_probability().
        """
        self._model_probs: dict[str, float] = model_probabilities or {}

    @property
    def name(self) -> str:
        return "fractional_kelly"

    def set_model_probability(self, ticker: str, prob: float) -> None:
        """Update the model's probability estimate for a given market."""
        if not 0.01 <= prob <= 0.99:
            raise ValueError(f"Probability must be in [0.01, 0.99], got {prob}")
        self._model_probs[ticker] = prob

    def evaluate(self, tick: dict[str, Any]) -> Signal | None:
        ticker    = tick.get("ticker", "")
        best_bid  = tick.get("best_bid")
        best_ask  = tick.get("best_ask")
        spread    = tick.get("spread")

        if best_bid is None or best_ask is None or spread is None:
            return None   # incomplete book — skip

        model_prob = self._model_probs.get(ticker)
        if model_prob is None:
            return None   # no model signal for this market

        # ── Edge-to-vig gate ────────────────────────────────────────────────
        vig_proxy = max(spread / 2, 0.5)           # half-spread in cents
        vig_pct   = vig_proxy / 100.0

        yes_edge = model_prob - (best_ask / 100.0)  # buy YES edge
        no_edge  = (1 - model_prob) - ((100 - best_bid) / 100.0)  # buy NO edge

        if yes_edge > no_edge and yes_edge > 0:
            side          = Side.YES
            edge          = yes_edge
            limit_price   = best_ask       # aggressive limit — take the offer
            market_prob   = best_ask / 100.0
        elif no_edge > 0:
            side          = Side.NO
            edge          = no_edge
            limit_price   = 100 - best_bid # buy NO at complement of YES bid
            market_prob   = (100 - best_bid) / 100.0
        else:
            return None   # no positive edge on either side

        edge_to_vig = edge / vig_pct
        if edge_to_vig < config.MIN_EDGE_TO_VIG:
            return None   # edge doesn't clear the vig hurdle

        # ── Kelly sizing ────────────────────────────────────────────────────
        p   = model_prob if side == Side.YES else (1 - model_prob)
        q   = 1 - p
        b   = (1 - market_prob) / market_prob  # net odds

        if b <= 0:
            return None

        kelly_full = (p * b - q) / b
        kelly_full = max(0.0, kelly_full)      # never negative
        kelly_frac = kelly_full / config.KELLY_DIVISOR

        # Scale fraction to cents, cap at MAX_POSITION_CENTS
        size_cents = int(kelly_frac * config.MAX_POSITION_CENTS)
        size_cents = min(size_cents, config.MAX_POSITION_CENTS)

        if size_cents < 1:
            return None   # rounding killed the trade

        return Signal(
            ticker=ticker,
            side=side,
            size_cents=size_cents,
            limit_price=limit_price,
            edge=round(edge, 5),
            edge_to_vig=round(edge_to_vig, 4),
            confidence=p,
            strategy=self.name,
            meta={
                "kelly_full":    round(kelly_full, 4),
                "kelly_divisor": config.KELLY_DIVISOR,
                "vig_proxy_pct": round(vig_pct, 4),
                "model_prob":    model_prob,
                "market_prob":   round(market_prob, 4),
            },
        )
