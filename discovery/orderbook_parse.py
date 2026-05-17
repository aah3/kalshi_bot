"""
discovery/orderbook_parse.py

Normalise Kalshi REST / WebSocket order book payloads into OrderBookSnapshot.

Kalshi exposes bids only; YES ask is derived from the best NO bid:
    yes_ask_cents = 100 - best_no_bid_cents

Supports:
  - orderbook_fp (yes_dollars / no_dollars string pairs) — current REST API
  - orderbook.yes.bids / orderbook.yes.asks — WebSocket snapshot shape
  - Legacy orderbook.yes / orderbook.no as [[price, qty], ...] arrays
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class OrderBookSnapshot:
    """Full order book for one ticker at a point in time."""
    ticker:      str
    fetched_at:  datetime
    yes_bids:    list[tuple[int, int]]   # [(price, qty), ...] best bid first
    yes_asks:    list[tuple[int, int]]   # [(price, qty), ...] best ask first
    best_bid:    int | None
    best_ask:    int | None
    spread:      int | None
    mid_price:   float | None

    def to_tick(self) -> dict[str, Any]:
        return {
            "ticker":        self.ticker,
            "best_bid":      self.best_bid,
            "best_ask":      self.best_ask,
            "spread":        self.spread,
            "mid_price":     self.mid_price,
            "event_type":    "snapshot",
            "updated_at_us": int(self.fetched_at.timestamp() * 1_000_000),
        }


def market_order_yes_price(
    book: OrderBookSnapshot,
    side: str,
    action: str,
    count: int,
    *,
    fallback: int = 99,
) -> int:
    """
    Kalshi ``yes_price`` on market orders:

    - **buy** YES: maximum you will pay (walk the ask ladder).
    - **sell** YES: minimum you will accept (walk the bid ladder).

    Using the buy cap on a sell leaves ``yes_price`` above the best bid, so IOC
    orders cancel with zero fills.
    """
    action = (action or "buy").lower()
    if action == "sell" and side == "yes":
        return _market_sell_yes_floor(book, count, fallback=1)
    if action == "sell" and side == "no":
        return _market_sell_no_floor(book, count, fallback=1)
    return worst_market_fill_price(book, side, count, fallback=fallback)


def _market_sell_yes_floor(
    book: OrderBookSnapshot,
    count: int,
    *,
    fallback: int = 1,
) -> int:
    """Minimum YES price to sell ``count`` contracts aggressively (hit bids)."""
    if count <= 0:
        return fallback

    floor = 99
    remaining = count
    for price, qty in book.yes_bids:
        if remaining <= 0:
            break
        if qty > 0:
            floor = min(floor, price)
        remaining -= min(remaining, qty)

    if floor < 99:
        return max(1, floor)
    if book.best_bid is not None:
        return max(1, book.best_bid)
    return max(1, fallback)


def _market_sell_no_floor(
    book: OrderBookSnapshot,
    count: int,
    *,
    fallback: int = 1,
) -> int:
    """Minimum NO price to sell ``count`` contracts aggressively (lift NO bids)."""
    if count <= 0:
        return fallback

    floor = 99
    remaining = count
    for yes_ask, qty in book.yes_asks:
        if remaining <= 0:
            break
        no_bid = 100 - yes_ask
        if qty > 0:
            floor = min(floor, no_bid)
        remaining -= min(remaining, qty)

    if floor < 99:
        return max(1, floor)
    if book.best_ask is not None:
        return max(1, 100 - book.best_ask)
    return max(1, fallback)


def worst_market_fill_price(
    book: OrderBookSnapshot,
    side: str,
    count: int,
    *,
    fallback: int = 99,
) -> int:
    """
    Highest price (cents) needed to buy ``count`` contracts aggressively.

    Kalshi requires exactly one of yes_price / no_price on every order,
    including type=market. For a buy, set the cap to the worst level consumed
    when walking the ask side of the book.
    """
    if count <= 0:
        return fallback

    if side == "yes":
        worst = 0
        remaining = count
        for price, qty in book.yes_asks:
            if remaining <= 0:
                break
            take = min(remaining, qty)
            if take > 0:
                worst = max(worst, price)
            remaining -= take
        if worst > 0:
            return min(worst, 99)
        if book.best_ask is not None:
            return min(book.best_ask, 99)
        return fallback

    # Buy NO: lift against YES bids (NO ask = 100 - yes_bid).
    worst = 0
    remaining = count
    for yes_bid, qty in book.yes_bids:
        if remaining <= 0:
            break
        take = min(remaining, qty)
        if take > 0:
            worst = max(worst, 100 - yes_bid)
        remaining -= take
    if worst > 0:
        return min(worst, 99)
    if book.best_bid is not None:
        return min(100 - book.best_bid, 99)
    return fallback


def _dollars_to_cents(val: Any) -> int:
    return int(round(float(val) * 100))


def _parse_fp_levels(raw: list) -> list[tuple[int, int]]:
    """[[price_dollars, qty_fp], ...] sorted ascending by price."""
    levels: list[tuple[int, int]] = []
    for lvl in raw or []:
        if not lvl or len(lvl) < 2:
            continue
        price_c = _dollars_to_cents(lvl[0])
        qty     = int(float(lvl[1]))
        if qty > 0:
            levels.append((price_c, qty))
    levels.sort(key=lambda x: x[0])
    return levels


def _parse_legacy_levels(raw: list) -> list[tuple[int, int]]:
    levels: list[tuple[int, int]] = []
    for lvl in raw or []:
        if not lvl:
            continue
        if isinstance(lvl, dict):
            price = int(lvl.get("price", 0))
            qty   = int(lvl.get("quantity", lvl.get("qty", 0)))
        else:
            price = int(lvl[0])
            qty   = int(lvl[1])
        if qty > 0:
            levels.append((price, qty))
    return levels


def _parse_nested_side(side: dict | list | None) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """WebSocket shape: yes: { bids: [...], asks: [...] }."""
    if not side:
        return [], []
    if isinstance(side, list):
        return _parse_legacy_levels(side), []

    bids = _parse_legacy_levels(side.get("bids", []))
    asks = _parse_legacy_levels(side.get("asks", []))
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    return bids, asks


def _yes_asks_from_no_bids(no_bids: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Convert NO bids to YES ask ladder (ascending YES price = best ask first)."""
    asks = [(100 - price, qty) for price, qty in no_bids if 0 < price < 100]
    asks.sort(key=lambda x: x[0])
    return asks


def parse_orderbook_response(
    data: dict[str, Any],
    ticker: str,
    *,
    fetched_at: datetime | None = None,
) -> OrderBookSnapshot | None:
    """
    Parse any known Kalshi order book JSON body into OrderBookSnapshot.
    Returns None if the payload has no recognisable book fields.
    """
    now = fetched_at or datetime.now(timezone.utc)
    fp  = data.get("orderbook_fp")
    ob  = data.get("orderbook", data)

    yes_bids: list[tuple[int, int]] = []
    yes_asks: list[tuple[int, int]] = []

    if fp:
        yes_bids_asc = _parse_fp_levels(fp.get("yes_dollars", []))
        no_bids      = _parse_fp_levels(fp.get("no_dollars", []))
        yes_bids     = sorted(yes_bids_asc, key=lambda x: x[0], reverse=True)
        yes_asks     = _yes_asks_from_no_bids(no_bids)
    elif isinstance(ob, dict):
        yes_side = ob.get("yes")
        no_side  = ob.get("no")

        if isinstance(yes_side, dict) and ("bids" in yes_side or "asks" in yes_side):
            yb, ya = _parse_nested_side(yes_side)
            yes_bids = sorted(yb, key=lambda x: x[0], reverse=True) if yb else []
            yes_asks = sorted(ya, key=lambda x: x[0]) if ya else []
            if not yes_asks and no_side:
                nb, _ = _parse_nested_side(no_side if isinstance(no_side, dict) else None)
                if not nb and isinstance(no_side, list):
                    nb = _parse_legacy_levels(no_side)
                yes_asks = _yes_asks_from_no_bids(sorted(nb, key=lambda x: x[0]))
        else:
            yes_bids = sorted(
                _parse_legacy_levels(yes_side if isinstance(yes_side, list) else []),
                key=lambda x: x[0],
                reverse=True,
            )
            no_bids = sorted(
                _parse_legacy_levels(no_side if isinstance(no_side, list) else []),
                key=lambda x: x[0],
            )
            yes_asks = _yes_asks_from_no_bids(no_bids)

    if not yes_bids and not yes_asks:
        return None

    best_bid = yes_bids[0][0] if yes_bids else None
    best_ask = yes_asks[0][0] if yes_asks else None
    spread   = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
    mid      = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else None

    return OrderBookSnapshot(
        ticker=ticker,
        fetched_at=now,
        yes_bids=yes_bids,
        yes_asks=yes_asks,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        mid_price=mid,
    )
