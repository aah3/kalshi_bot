"""
trading/kalshi_order_v2.py

Kalshi REST v2 order-create request/response helpers shared by
``ExecutionManager`` (strategy-driven orders) and ``OrderEntry`` (manual
order placement via ``tools/trade.py``).

Endpoint:  POST /portfolio/orders
Signed as: /trade-api/v2/portfolio/orders

────────────────────────────────────────────────────────────────────────────
REQUEST BODY (subset actually used by this bot)
────────────────────────────────────────────────────────────────────────────

  ticker            string   market ticker
  client_order_id   string   our idempotency key (UUID)
  action            "buy" | "sell"
  side              "yes" | "no"
  type              "limit" | "market"
  count             integer  whole contracts (Kalshi requires an integer here)
  count_fp          string   optional fractional count, e.g. "27.31", only
                              sent when the size does not land on a whole
                              contract (sub-$1 sizing)
  yes_price         integer  cents, required for YES limit/market-cap orders
  no_price          integer  cents, required for NO limit/market-cap orders
  post_only         bool     true for resting GTC limit orders — never take
                              liquidity, only add it
  expiration_ts     integer  unix seconds; omitted = good-till-cancelled

See ``scripts/test_auth_orders.py`` for a live signature probe against this
same endpoint/body shape.
"""

from __future__ import annotations

import math
from typing import Any

# ── Endpoint ──────────────────────────────────────────────────────────────────

CREATE_ORDER_V2_PATH: str = "/portfolio/orders"


# ── Time-in-force normalisation ──────────────────────────────────────────────

_TIME_IN_FORCE_ALIASES: dict[str, str] = {
    "gtc":                  "good_till_canceled",
    "good_till_canceled":   "good_till_canceled",
    "good_til_canceled":    "good_till_canceled",
    "ioc":                  "immediate_or_cancel",
    "immediate_or_cancel":  "immediate_or_cancel",
    "fok":                  "fill_or_kill",
    "fill_or_kill":         "fill_or_kill",
}


def normalize_time_in_force(value: str | None) -> str:
    """
    Map CLI/legacy aliases (gtc/ioc/fok) to this bot's canonical
    time-in-force values: "good_till_canceled" | "immediate_or_cancel" |
    "fill_or_kill".

    Raises ValueError on an unrecognised value so a typo'd config fails
    loudly at order-build time instead of silently resting as GTC.
    """
    key = (value or "good_till_canceled").strip().lower()
    if key not in _TIME_IN_FORCE_ALIASES:
        raise ValueError(f"Unknown time_in_force {value!r}")
    return _TIME_IN_FORCE_ALIASES[key]


# ── Fractional contract sizing ───────────────────────────────────────────────

def kalshi_order_count_fields(contracts: float) -> tuple[int, str]:
    """
    Kalshi's order API takes a whole-number ``count`` of contracts. This bot
    sizes trades in cents (e.g. $1 sleeves), which can land on a fractional
    number of contracts at some prices, so callers pre-round to 2dp and get
    back both:
      - the whole-contract count to submit (floored, never rounded up —
        never buy more than the sized amount)
      - the exact fractional value as a fixed 2dp string, for logging /
        audit / any endpoint that accepts ``count_fp``

    >>> kalshi_order_count_fields(3)
    (3, '3.00')
    >>> kalshi_order_count_fields(0.69)
    (0, '0.69')
    >>> kalshi_order_count_fields(27.31)
    (27, '27.31')
    """
    count_fp = round(float(contracts), 2)
    whole = int(math.floor(count_fp + 1e-9))
    return whole, f"{count_fp:.2f}"


# ── Request body ──────────────────────────────────────────────────────────────

def build_create_order_v2_body(
    ticker:          str,
    client_order_id: str,
    action:          str,             # "buy" | "sell"
    side:            str,             # "yes" | "no"
    contracts:       float,
    time_in_force:   str        = "good_till_canceled",
    yes_price:       int | None = None,
    no_price:        int | None = None,
    post_only:       bool       = False,
) -> dict[str, Any]:
    """
    Build the JSON body for ``POST /portfolio/orders``.

    Exactly one of ``yes_price`` / ``no_price`` should be set — Kalshi
    always prices in YES-cents, so a NO order is submitted as ``no_price``
    (the caller is responsible for the 100-minus-YES conversion; see
    ``ExecutionManager.submit_order`` / ``OrderEntry.place_order``).

    ``time_in_force`` is this bot's own abstraction:
      - good_till_canceled  -> resting "limit" order, no expiration
      - immediate_or_cancel -> "market" order — fill what's available now,
                                cancel the remainder
      - fill_or_kill        -> same wire shape as IOC; the caller must
                                verify a complete fill and treat a partial
                                fill as a failure
    """
    if side not in ("yes", "no"):
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    if yes_price is None and no_price is None:
        raise ValueError("build_create_order_v2_body requires yes_price or no_price")

    tif        = normalize_time_in_force(time_in_force)
    order_type = "limit" if tif == "good_till_canceled" else "market"
    count, count_fp = kalshi_order_count_fields(contracts)

    body: dict[str, Any] = {
        "ticker":          ticker,
        "client_order_id": client_order_id,
        "action":          action,
        "side":            side,
        "type":            order_type,
        "count":           count,
    }

    # Only send count_fp when it carries information beyond the whole count
    # (i.e. a genuinely fractional size) — keeps whole-contract order bodies
    # identical to the historically-verified probe in scripts/test_auth_orders.py.
    if count_fp != f"{count}.00":
        body["count_fp"] = count_fp

    if yes_price is not None:
        body["yes_price"] = int(yes_price)
    if no_price is not None:
        body["no_price"] = int(no_price)

    if order_type == "limit":
        body["post_only"] = bool(post_only)
    elif tif == "fill_or_kill":
        # Kalshi's market orders are IOC by default; mark FOK explicitly so
        # the exchange rejects rather than partially fills.
        body["time_in_force"] = "fill_or_kill"

    return body


# ── Response parsing ──────────────────────────────────────────────────────────

def parse_create_order_v2_response(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalise ``POST /portfolio/orders`` responses.

    Kalshi wraps the created order in an ``{"order": {...}}`` envelope;
    some sandbox/mocked responses return the order fields at the top level.
    Always returns a flat dict so callers can do ``order.get("order_id")``.
    """
    if not isinstance(raw, dict):
        return {}
    order = raw.get("order")
    if isinstance(order, dict):
        return dict(order)
    return dict(raw)
