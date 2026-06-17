"""
discovery/game_close.py

Effective close-time estimates for in-play *game* markets.

Kalshi game tickers (e.g. KXNBAGAME-26JUN05NYKSAS-NYK) often carry a far-future
``close_time`` (settlement) while the sporting event itself ends hours earlier.
Risk gates such as ``check_stop_loss_allowed`` use ``minutes_to_close`` from the
market registry — without adjustment, stops are wrongly suppressed for hours.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from discovery.market_client import MarketSummary

# Tickers like KXNBAGAME-26JUN05NYKSAS-NYK, KXWCGAME-26JUN13QATSUI-SUI
_GAME_DATE_RE = re.compile(
    r"GAME-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})",
    re.IGNORECASE,
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# When API close is this far out, prefer the game-window estimate.
_API_CLOSE_OVERRIDE_MINUTES = 24 * 60

# Recent trade activity → treat as in-play for stop-loss gating.
_IN_PLAY_MAX_UPDATE_MINUTES = 180.0

# In-play markets report this many minutes-to-close so the 5-minute stop gate
# allows protective exits during the event.
_IN_PLAY_STOP_GATE_MINUTES = 5.0

# Latest plausible game end: start of game day + this offset (covers late US games).
_GAME_END_OFFSET = timedelta(hours=30)


def is_game_ticker(ticker: str) -> bool:
    """True when ``ticker`` embeds a GAME event date (sports match markets)."""
    return bool(ticker and _GAME_DATE_RE.search(ticker.upper()))


def parse_game_day_start(ticker: str) -> datetime | None:
    """Parse the calendar game day (UTC midnight) from a GAME ticker."""
    m = _GAME_DATE_RE.search((ticker or "").upper())
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2).upper(), m.group(3)
    month = _MONTHS.get(mon)
    if month is None:
        return None
    try:
        return datetime(2000 + int(yy), month, int(dd), tzinfo=timezone.utc)
    except ValueError:
        return None


def estimated_game_end(ticker: str) -> datetime | None:
    """Upper bound for when the sporting event is likely finished."""
    start = parse_game_day_start(ticker)
    if start is None:
        return None
    return start + _GAME_END_OFFSET


def is_within_game_window(ticker: str, now: datetime | None = None) -> bool:
    """True when ``now`` falls between game-day start and estimated game end."""
    start = parse_game_day_start(ticker)
    end = estimated_game_end(ticker)
    if start is None or end is None:
        return False
    now = now or datetime.now(timezone.utc)
    return start <= now <= end


def effective_minutes_to_close(market: MarketSummary) -> float | None:
    """
    Return minutes-to-close for risk gates, adjusted for in-play game markets.

    Non-game markets pass through unchanged. Game markets use a synthetic window
    when API settlement is far in the future but the event is active.
    """
    status = (market.status or "").strip().lower()
    if status in ("closed", "settled", "finalized"):
        return _IN_PLAY_STOP_GATE_MINUTES

    # Dead or resolved book — allow protective exits even when API close is far out.
    yes_bid = market.yes_bid
    if yes_bid is not None and yes_bid <= 1:
        stale = (
            market.minutes_since_update is None
            or market.minutes_since_update > 30.0
        )
        if stale or status in ("closed", "settled", "finalized"):
            return _IN_PLAY_STOP_GATE_MINUTES

    api_mins = market.minutes_to_close
    if not is_game_ticker(market.ticker):
        return api_mins

    game_end = estimated_game_end(market.ticker)
    if game_end is None:
        return api_mins

    now = datetime.now(timezone.utc)
    game_mins = max(0.0, (game_end - now).total_seconds() / 60.0)

    in_play = (
        is_within_game_window(market.ticker, now)
        and market.volume_24h > 0
        and market.minutes_since_update is not None
        and market.minutes_since_update <= _IN_PLAY_MAX_UPDATE_MINUTES
    )
    if in_play:
        return min(game_mins, _IN_PLAY_STOP_GATE_MINUTES)

    if api_mins is None or api_mins > _API_CLOSE_OVERRIDE_MINUTES:
        return game_mins

    return min(api_mins, game_mins)


def apply_effective_close(market: MarketSummary) -> MarketSummary:
    """Mutate ``market.minutes_to_close`` in place and return it."""
    market.minutes_to_close = effective_minutes_to_close(market)
    return market
