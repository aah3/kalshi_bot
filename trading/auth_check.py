"""
trading/auth_check.py

Verify that API credentials work for trading (portfolio) endpoints.
Market data endpoints may work without valid keys; orders require portfolio auth.
"""

from __future__ import annotations

import aiohttp

import config
from credentials.credential_manager import CredentialManager
from execution.rate_limiter import BucketType, RateLimiter


DEMO_KEYS_URL = "https://demo.kalshi.com/account/api-keys"
PROD_KEYS_URL = "https://kalshi.com/account/api-keys"


async def verify_portfolio_credentials(
    credentials: CredentialManager,
    rate_limiter: RateLimiter,
    session: aiohttp.ClientSession,
) -> tuple[bool, str]:
    """
    GET /portfolio/balance — requires a valid key for the current ENV.

    Returns:
        (ok, message) — message explains failure when ok is False.
    """
    path      = "/portfolio/balance"
    sign_path = f"/trade-api/v2{path}"

    async with rate_limiter.throttle(BucketType.READ):
        headers = credentials.sign_request("GET", sign_path)
        resp = await session.get(f"{config.BASE_URL}{path}", headers=headers)

    if resp.status == 200:
        return True, "Portfolio credentials verified"

    text = await resp.text()
    if resp.status == 401 and "header_timestamp_expired" in text:
        async with rate_limiter.throttle(BucketType.READ):
            headers = credentials.sign_request("GET", sign_path)
            resp = await session.get(f"{config.BASE_URL}{path}", headers=headers)
        if resp.status == 200:
            return True, "Portfolio credentials verified (timestamp retry)"
        text = await resp.text()
    if resp.status == 401:
        if "NOT_FOUND" in text:
            keys_url = DEMO_KEYS_URL if config.ENV != "production" else PROD_KEYS_URL
            return False, (
                f"API key not found on {config.ENV.upper()} ({config.BASE_URL}). "
                f"Create keys for this environment: {keys_url}"
            )
        if "INCORRECT_API_KEY_SIGNATURE" in text:
            return False, (
                "API key signature rejected — the private key does not match "
                f"KALSHI_{'DEMO' if config.ENV != 'production' else 'PROD'}_API_KEY_ID. "
                "Re-download the .key file from the same key row in the Kalshi API console."
            )

    return False, (
        f"Portfolio auth failed: HTTP {resp.status} — {text[:300]}"
    )


async def calibrate_clock_offset(
    credentials: CredentialManager,
    session: aiohttp.ClientSession,
) -> int:
    """
    Align signed timestamps to Kalshi's Date header.

    Windows clocks often sit 30s+ ahead of Kalshi, which yields
    ``header_timestamp_expired`` on portfolio endpoints.
    """
    from credentials.clock_skew import offset_ms_from_http_date
    from logging_.structured_logger import logger

    resp = await session.get(f"{config.BASE_URL}/exchange/status")
    date_hdr = resp.headers.get("Date")
    if not date_hdr:
        return 0
    offset = offset_ms_from_http_date(date_hdr)
    credentials.set_clock_offset_ms(offset)
    if abs(offset) >= 2_000:
        logger.warning(
            "Applied Kalshi clock offset to request signatures",
            offset_ms=offset,
            http_date=date_hdr,
        )
    return offset
