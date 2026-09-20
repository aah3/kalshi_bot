"""Align request timestamps with Kalshi's HTTP Date (local clocks often drift)."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def offset_ms_from_http_date(
    date_header: str,
    *,
    now: datetime | None = None,
) -> int:
    """
    Milliseconds to add to local epoch so it matches the server Date header.

    Local clock ahead of Kalshi → negative offset (typical ``header_timestamp_expired``).
    """
    server = parsedate_to_datetime(date_header)
    if server.tzinfo is None:
        server = server.replace(tzinfo=timezone.utc)
    local = now if now is not None else datetime.now(timezone.utc)
    if local.tzinfo is None:
        local = local.replace(tzinfo=timezone.utc)
    return int((server - local).total_seconds() * 1000)
