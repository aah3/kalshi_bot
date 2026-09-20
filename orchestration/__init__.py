"""
orchestration — StrategyInstance config, adapters, and universe refresh.

Phase 1: load a declarative instance, drive one main.py worker, optionally
rediscover markets mid-session.
"""

from orchestration.strategy_instance import (
    StrategyInstance,
    StrategyInstanceError,
    load_strategy_instance,
)

__all__ = [
    "StrategyInstance",
    "StrategyInstanceError",
    "load_strategy_instance",
]
