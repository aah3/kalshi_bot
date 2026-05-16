"""
credentials/env_credentials.py

Resolve API credentials for demo vs production from environment variables.

Store both sets in .env — switch with KALSHI_ENV only (no manual key swapping).

Preferred (explicit per environment):
    KALSHI_DEMO_API_KEY_ID / KALSHI_DEMO_PRIVATE_KEY_B64
    KALSHI_PROD_API_KEY_ID / KALSHI_PROD_PRIVATE_KEY_B64

Legacy fallback (used when env-specific vars are unset):
    KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_B64
"""

from __future__ import annotations

import os


class CredentialError(Exception):
    """Raised when credentials are missing or malformed."""

# Env-specific variable names
_ENV_PREFIX = {
    "demo": "KALSHI_DEMO",
    "production": "KALSHI_PROD",
}

# Aliases for production prefix
_PROD_PREFIX_ALIASES = ("KALSHI_PROD", "KALSHI_PRODUCTION")


def normalize_env(env: str) -> str:
    key = env.strip().lower()
    if key in ("prod", "production", "live"):
        return "production"
    if key in ("demo", "paper", "sandbox"):
        return "demo"
    raise CredentialError(
        f"KALSHI_ENV must be 'demo' or 'production', got {env!r}"
    )


def _get_nonempty(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def resolve_credentials(env: str | None = None) -> tuple[str, str, str]:
    """
    Pick API key + private key for the requested environment.

    Returns:
        (api_key_id, private_key_b64, source_description)

    Resolution order:
        1. KALSHI_{DEMO|PROD}_API_KEY_ID and KALSHI_{DEMO|PROD}_PRIVATE_KEY_B64
        2. KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_B64 (legacy single pair)
    """
    norm = normalize_env(env or os.getenv("KALSHI_ENV", "demo"))

    if norm == "demo":
        prefixes = ("KALSHI_DEMO",)
    else:
        prefixes = _PROD_PREFIX_ALIASES

    api_key = _get_nonempty(*(f"{p}_API_KEY_ID" for p in prefixes))
    key_b64 = _get_nonempty(*(f"{p}_PRIVATE_KEY_B64" for p in prefixes))
    source  = f"{prefixes[0]}_*"

    if not api_key or not key_b64:
        legacy_id  = _get_nonempty("KALSHI_API_KEY_ID")
        legacy_b64 = _get_nonempty("KALSHI_PRIVATE_KEY_B64")
        if legacy_id and legacy_b64:
            api_key = legacy_id
            key_b64 = legacy_b64
            source  = "KALSHI_API_KEY_ID (legacy fallback)"
        else:
            missing = []
            if not api_key:
                missing.append(f"{prefixes[0]}_API_KEY_ID")
            if not key_b64:
                missing.append(f"{prefixes[0]}_PRIVATE_KEY_B64")
            raise CredentialError(
                f"Missing credentials for {norm.upper()} environment. "
                f"Set {' and '.join(missing)}, or set legacy KALSHI_API_KEY_ID + "
                f"KALSHI_PRIVATE_KEY_B64. Demo keys: demo.kalshi.com/account/api-keys — "
                f"Prod keys: kalshi.com/account/api-keys"
            )

    return api_key, key_b64, source
