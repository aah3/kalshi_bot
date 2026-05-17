"""Max concurrent position counting."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cfg = types.ModuleType("config")
cfg.KELLY_DIVISOR = 4
cfg.MAX_POSITION_CENTS = 10_000
cfg.MIN_EDGE_TO_VIG = 0.02
cfg.FEE_PER_CONTRACT_CENTS = 0.0
sys.modules["config"] = cfg

log_mod = types.ModuleType("logging_.structured_logger")


class _StubLogger:
    def __getattr__(self, _):
        return lambda *a, **kw: None


log_mod.logger = _StubLogger()
sys.modules["logging_"] = types.ModuleType("logging_")
sys.modules["logging_.structured_logger"] = log_mod

from strategy.green_up_strategy import GreenUpStrategy, PositionState
from strategy.position_limits import count_open_positions


def test_count_green_up_open_states():
    strat = GreenUpStrategy(entry_max_price=25, hedge_trigger_price=68)
    strat.add_watch_ticker("A")
    strat.add_watch_ticker("B")
    assert count_open_positions(strat) == 0

    strat._positions["A"].state = PositionState.ENTERED
    strat._positions["B"].state = PositionState.WATCHING
    assert count_open_positions(strat) == 2

    strat._positions["A"].state = PositionState.HEDGED
    assert count_open_positions(strat) == 1
