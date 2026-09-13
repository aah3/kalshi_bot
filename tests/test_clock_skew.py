"""Tests for Kalshi HTTP Date clock skew compensation."""

from datetime import datetime, timezone

from credentials.clock_skew import offset_ms_from_http_date


def test_offset_when_local_clock_is_ahead():
    server = datetime(2026, 9, 7, 13, 46, 55, tzinfo=timezone.utc)
    local = datetime(2026, 9, 7, 13, 47, 33, tzinfo=timezone.utc)
    date_hdr = "Mon, 07 Sep 2026 13:46:55 GMT"
    offset = offset_ms_from_http_date(date_hdr, now=local)
    assert offset == int((server - local).total_seconds() * 1000)
    assert offset <= -30_000


def test_offset_near_zero_when_clocks_match():
    local = datetime(2026, 9, 7, 13, 46, 55, tzinfo=timezone.utc)
    offset = offset_ms_from_http_date("Mon, 07 Sep 2026 13:46:55 GMT", now=local)
    assert abs(offset) < 1000
