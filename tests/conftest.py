"""
Shared pytest hooks for test isolation.

Several test modules replace ``sys.modules["config"]`` with a lightweight shim.
Imported consumers (``discovery.market_math``, ``strategy.high_prob_strategy``,
etc.) keep a stale ``config`` reference unless rebound before each test.
"""

from __future__ import annotations

import sys

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
