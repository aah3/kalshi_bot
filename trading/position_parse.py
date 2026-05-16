"""
trading/position_parse.py

Normalise Kalshi GET /portfolio/positions market_positions payloads.

Current API (v3+) uses signed ``position_fp`` (positive = YES, negative = NO)
and dollar string fields. Legacy responses used integer ``position`` / ``side``.
"""

from __future__ import annotations

from typing import Any


def _dollars_to_cents(val: Any) -> int:
    if val is None:
        return 0
    try:
        return int(round(float(val) * 100))
    except (TypeError, ValueError):
        return int(val or 0)


def _fp_contracts(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(int(val or 0))


def parse_market_position(raw: dict[str, Any]) -> dict[str, Any] | None:
    """
    Parse one ``market_positions`` element into fields for ``Position``.

    Returns None when the row has no open size.
    """
    ticker = raw.get("ticker", "")
    if not ticker:
        return None

    side = "yes"
    contracts_fp = 0.0

    if "position_fp" in raw:
        contracts_fp = _fp_contracts(raw["position_fp"])
        if contracts_fp > 0:
            side = "yes"
        elif contracts_fp < 0:
            side = "no"
            contracts_fp = abs(contracts_fp)
        else:
            return None
    else:
        legacy = raw.get("position", raw.get("contracts"))
        if legacy is None:
            return None
        try:
            legacy_int = int(legacy)
        except (TypeError, ValueError):
            return None
        if legacy_int == 0:
            return None
        if legacy_int < 0:
            side = "no"
            contracts_fp = float(abs(legacy_int))
        else:
            side = raw.get("side", "yes").lower()
            contracts_fp = float(legacy_int)

    contracts = max(int(round(contracts_fp)), 0)
    if contracts == 0 and contracts_fp > 0:
        contracts = 1

    if contracts <= 0:
        return None

    exposure_cents = _dollars_to_cents(raw.get("market_exposure_dollars"))
    if exposure_cents == 0 and raw.get("market_exposure") is not None:
        exposure_cents = int(raw.get("market_exposure", 0) or 0)

    # total_traded_dollars is another fallback for cost basis
    if exposure_cents == 0:
        exposure_cents = _dollars_to_cents(raw.get("total_traded_dollars"))

    avg_entry = exposure_cents // contracts if contracts > 0 else 0

    return {
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "contracts_fp": contracts_fp,
        "avg_entry_price": avg_entry,
        "cost_basis_cents": exposure_cents,
        "market_title": raw.get("market_title", raw.get("title", "")),
        "realized_pnl_cents": _dollars_to_cents(raw.get("realized_pnl_dollars")),
    }
