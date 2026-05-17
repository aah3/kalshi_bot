"""Blotter query_trades / query_legs filter tests."""

import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def blotter(tmp_path):
    # Other tests may shim sys.modules['config']; reload the real module here.
    sys.modules.pop("config", None)
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import config  # noqa: F401

    importlib.reload(sys.modules["config"])
    sys.modules.pop("metrics.blotter", None)
    from metrics.blotter import Blotter

    return Blotter(db_path=str(tmp_path / "test.db"))


def test_query_trades_by_resolution_and_trade_id(blotter):
    tid = blotter.open_trade(
        ticker="T-A",
        category="Sports",
        strategy="green_up_full_green",
    )
    with blotter._conn() as conn:
        conn.execute(
            "UPDATE parent_trades SET resolution = ?, status = ? WHERE trade_id = ?",
            ("yes", "settled", tid),
        )

    by_res = blotter.query_trades(resolution="yes")
    assert len(by_res) == 1
    assert by_res[0].trade_id == tid

    by_id = blotter.query_trades(trade_id=tid)
    assert len(by_id) == 1

    assert blotter.query_trades(resolution="no") == []


def test_query_legs_by_trade_type(blotter):
    tid = blotter.open_trade(ticker="T-B", strategy="kelly")
    blotter.record_fill(
        parent_trade_id=tid,
        order_id="o1",
        side="yes",
        entry_price=25,
        contracts=2,
        trade_type="entry",
    )
    blotter.record_fill(
        parent_trade_id=tid,
        order_id="o2",
        side="no",
        entry_price=30,
        contracts=2,
        trade_type="hedge",
    )

    entries = blotter.query_legs(parent_trade_id=tid, trade_type="entry")
    hedges  = blotter.query_legs(parent_trade_id=tid, trade_type="hedge")
    assert len(entries) == 1
    assert len(hedges) == 1
    assert entries[0].trade_type == "entry"
    assert hedges[0].trade_type == "hedge"
