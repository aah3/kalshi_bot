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


def test_mark_trade_hedged_keeps_parent_open_for_settlement(blotter):
    tid = blotter.open_trade(ticker="T-H", strategy="green_up_full_green")
    blotter.record_fill(
        parent_trade_id=tid,
        order_id="e1",
        side="yes",
        entry_price=25,
        contracts=1,
        trade_type="entry",
    )
    blotter.record_fill(
        parent_trade_id=tid,
        order_id="h1",
        side="no",
        entry_price=40,
        contracts=1,
        trade_type="hedge",
    )

    blotter.mark_trade_hedged(tid, locked_profit_cents=350, notes="hedged")

    parents = blotter.query_trades(trade_id=tid)
    assert len(parents) == 1
    assert parents[0].status == "hedged"
    assert parents[0].net_pnl_cents is None
    assert "locked_profit_cents=350" in parents[0].notes

    unsettled = blotter.open_positions_summary()
    assert any(p["trade_id"] == tid for p in unsettled)

    legs = blotter.query_legs(parent_trade_id=tid, status="open")
    assert len(legs) == 2


def test_close_leg_stop_loss_marks_leg_and_parent_stopped(blotter):
    """A stop-loss close must roll up to a ``stopped`` parent, not ``closed`` —
    that is what lets the blotter distinguish a stop-capped loss from a normal
    exit (POSITION STOP alerts firing with no ``stopped`` parent recorded)."""
    tid = blotter.open_trade(ticker="T-STOP", strategy="green_up_full_green_market")
    leg_id = blotter.record_fill(
        parent_trade_id=tid,
        order_id="e1",
        side="yes",
        entry_price=61,
        contracts=1,
        trade_type="entry",
    )

    pnl = blotter.close_leg(leg_id, exit_price=43, close_type="stop_loss")
    assert pnl < 0

    leg = blotter.get_leg(leg_id)
    assert leg.status == "stopped"
    assert leg.exit_price == 43

    blotter.close_trade(tid, notes="stopped")

    parent = blotter.get_trade(tid)
    assert parent.status == "stopped"
    assert parent.net_pnl_cents == pnl

    # Stopped trades must be discoverable via the stopped-status filter and
    # must roll into the strategy/category P&L aggregates like any other
    # closed position.
    assert [p.trade_id for p in blotter.query_trades(status="stopped")] == [tid]
    strategy_pnl = {row["group"]: row for row in blotter.pnl_by_strategy(days=1)}
    assert "green_up_full_green_market" in strategy_pnl
    assert strategy_pnl["green_up_full_green_market"]["trade_count"] == 1


def test_close_leg_settlement_still_takes_priority_over_stopped(blotter):
    """If a stopped leg's market later settles, settlement status must win."""
    tid = blotter.open_trade(ticker="T-STOP-SETTLE", strategy="green_up_full_green_market")
    leg_id = blotter.record_fill(
        parent_trade_id=tid,
        order_id="e1",
        side="yes",
        entry_price=61,
        contracts=1,
        trade_type="entry",
    )
    blotter.close_leg(leg_id, exit_price=43, close_type="stop_loss")

    hedge_leg_id = blotter.record_fill(
        parent_trade_id=tid,
        order_id="h1",
        side="no",
        entry_price=39,
        contracts=1,
        trade_type="hedge",
    )
    blotter.close_leg(hedge_leg_id, exit_price=100, close_type="settlement", resolution="no")

    blotter.close_trade(tid)

    parent = blotter.get_trade(tid)
    assert parent.status == "settled"
