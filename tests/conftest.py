"""
Shared pytest hooks for test isolation.

Several test modules replace ``sys.modules["config"]`` with a lightweight shim.
Imported consumers (``discovery.market_math``, ``strategy.high_prob_strategy``,
etc.) keep a stale ``config`` reference unless rebound before each test.

It also redirects the structured-logger file and the blotter/metrics SQLite DB
to a throwaway temp directory *before* ``config`` is imported, so running the
test suite can never append to the live ``kalshi_bot_prod.jsonl`` /
``kalshi_bot_prod.db`` (or the demo equivalents). Previously, tests that used
the real logger/Blotter polluted production data with synthetic RISK_BREACH
events and seed trades (e.g. tickers ``T-A`` / ``T8``), which made the prod
logs/DB impossible to trust when diagnosing real runs.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ── Test-environment isolation (must run before ``import config``) ────────────
# config.py resolves LOG_FILE / DB_PATH from KALSHI_{DEMO,PROD}_* env vars and
# only falls back to the generic key when the prefixed one is unset. Override
# every variant so whichever KALSHI_ENV is active still points at the sandbox.
# load_dotenv(override=False) at config import will not clobber these.
_TEST_ARTIFACT_DIR = Path(tempfile.gettempdir()) / "kalshi_bot_test_artifacts"
_TEST_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
_TEST_LOG = str(_TEST_ARTIFACT_DIR / "test.jsonl")
_TEST_DB  = str(_TEST_ARTIFACT_DIR / "test.db")
for _key, _val in (
    ("KALSHI_LOG_FILE", _TEST_LOG),
    ("KALSHI_DEMO_LOG_FILE", _TEST_LOG),
    ("KALSHI_PROD_LOG_FILE", _TEST_LOG),
    ("KALSHI_DB_PATH", _TEST_DB),
    ("KALSHI_DEMO_DB_PATH", _TEST_DB),
    ("KALSHI_PROD_DB_PATH", _TEST_DB),
    ("KALSHI_LOG_CONSOLE", "false"),
):
    os.environ[_key] = _val

import pytest

_CONFIG_CONSUMER_MODULES = (
    "discovery.market_math",
    "discovery.ticker_selector",
    "strategy.high_prob_strategy",
    "risk.circuit_breaker",
)


def sync_config_bindings() -> None:
    """Point cached imports at the active ``config`` module."""
    cfg = sys.modules.get("config")
    if cfg is None:
        return
    for name in _CONFIG_CONSUMER_MODULES:
        mod = sys.modules.get(name)
        if mod is not None:
            mod.config = cfg


@pytest.fixture(autouse=True)
def _rebind_config_after_profile_tests():
    """Keep consumer modules aligned when another test reloaded config."""
    sync_config_bindings()
    yield
    sync_config_bindings()
