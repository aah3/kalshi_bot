"""AlertManager bot-ownership attribution tests.

The portfolio snapshot reflects the *aggregate* exchange position on a ticker
(manual trades + every strategy). A stop/profit alert that quotes the whole
position can misrepresent the bot's own exposure, so the alert is enriched with
a bot-owned breakdown from the blotter. These tests guard that attribution.
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def AlertManager():
    # Other test modules shim sys.modules['config']; reload the real module so
    # alert_manager and its imports bind to a consistent config.
    sys.modules.pop("config", None)
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import config  # noqa: F401

    importlib.reload(sys.modules["config"])
    for mod in ("metrics.blotter", "trading.portfolio_monitor", "risk.alert_manager"):
        sys.modules.pop(mod, None)
    from risk.alert_manager import AlertManager as _AM

    return _AM


class _StubBlotter:
    def __init__(self, legs):
        self._legs = legs

    def query_legs(self, ticker=None, status=None, **_):
        return [
            leg for leg in self._legs
            if (ticker is None or leg.ticker == ticker)
            and (status is None or leg.status == status)
        ]


def _leg(ticker, side, contracts, entry_price, status="open"):
    return SimpleNamespace(
        ticker=ticker, side=side, contracts=contracts,
        entry_price=entry_price, status=status,
    )


def _position(ticker, side, contracts, avg_entry_price, mark_price,
              cost_basis, unrealised_pnl):
    return SimpleNamespace(
        ticker=ticker, side=side, contracts=contracts,
        avg_entry_price=avg_entry_price, mark_price=mark_price,
        cost_basis=cost_basis, unrealised_pnl=unrealised_pnl,
        implied_prob=mark_price / 100.0,
    )


def test_position_stop_reports_bot_owned_subset(AlertManager):
    # Exchange shows 8 YES contracts; the bot only opened 1 of them.
    blotter = _StubBlotter([_leg("TK", "yes", 1, 22)])
    mgr = AlertManager(blotter=blotter)
    pos = _position("TK", "yes", contracts=8, avg_entry_price=31,
                    mark_price=14, cost_basis=250, unrealised_pnl=-130)

    alerts = mgr._check_position_stop(pos)
    assert len(alerts) == 1
    data = alerts[0].data
    assert data["contracts"] == 8
    assert data["bot_owned_contracts"] == 1
    assert data["bot_owned_cost_usd"] == 0.22
    assert "bot owns 1/8 contracts" in alerts[0].message


def test_position_stop_no_note_when_bot_owns_full_position(AlertManager):
    blotter = _StubBlotter([_leg("TK", "yes", 8, 31)])
    mgr = AlertManager(blotter=blotter)
    pos = _position("TK", "yes", contracts=8, avg_entry_price=31,
                    mark_price=14, cost_basis=250, unrealised_pnl=-130)

    alerts = mgr._check_position_stop(pos)
    assert alerts[0].data["bot_owned_contracts"] == 8
    assert "bot owns" not in alerts[0].message


def test_position_stop_ignores_other_side_legs(AlertManager):
    # A NO hedge leg must not count toward a YES position's bot-owned total.
    blotter = _StubBlotter([
        _leg("TK", "yes", 1, 22),
        _leg("TK", "no", 5, 50),
    ])
    mgr = AlertManager(blotter=blotter)
    pos = _position("TK", "yes", contracts=8, avg_entry_price=31,
                    mark_price=14, cost_basis=250, unrealised_pnl=-130)

    alerts = mgr._check_position_stop(pos)
    assert alerts[0].data["bot_owned_contracts"] == 1


def test_position_stop_without_blotter_omits_attribution(AlertManager):
    mgr = AlertManager(blotter=None)
    pos = _position("TK", "yes", contracts=8, avg_entry_price=31,
                    mark_price=14, cost_basis=250, unrealised_pnl=-130)

    alerts = mgr._check_position_stop(pos)
    assert len(alerts) == 1
    assert "bot_owned_contracts" not in alerts[0].data
    assert "bot owns" not in alerts[0].message


def test_position_stop_skipped_when_bot_owns_none(AlertManager):
    # Exchange shows an 8-contract YES position but the bot owns none of it
    # (manual / other-strategy holding). No CRITICAL alert should fire — this
    # was the source of phantom RISK_BREACH events for unrelated positions.
    blotter = _StubBlotter([_leg("OTHER", "yes", 4, 50)])  # different ticker
    mgr = AlertManager(blotter=blotter)
    pos = _position("TK", "yes", contracts=8, avg_entry_price=31,
                    mark_price=14, cost_basis=250, unrealised_pnl=-130)

    assert mgr._check_position_stop(pos) == []
    # Cooldown must NOT be armed by a skipped alert, so a later legitimate
    # bot-owned alert can still fire immediately.
    assert (mgr._cooldown == {}) or all(
        k[0].value != "POSITION_STOP" for k in mgr._cooldown
    )


def test_profit_target_skipped_when_bot_owns_none(AlertManager):
    blotter = _StubBlotter([])  # bot owns nothing on this ticker
    mgr = AlertManager(blotter=blotter)
    pos = _position("TK", "yes", contracts=8, avg_entry_price=31,
                    mark_price=80, cost_basis=250, unrealised_pnl=300)

    assert mgr._check_profit_target(pos) == []
