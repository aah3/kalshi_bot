"""MetricsStore regression tests.

Guards the ``mark_signal_filled`` portability bug: stock SQLite builds (incl.
Windows) are not compiled with SQLITE_ENABLE_UPDATE_DELETE_LIMIT, so an
``UPDATE ... ORDER BY ... LIMIT`` raises ``near "ORDER": syntax error`` and
crashed the bot whenever a fill arrived.
"""

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    # Other test modules shim sys.modules['config']; reload the real module.
    sys.modules.pop("config", None)
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import config  # noqa: F401

    importlib.reload(sys.modules["config"])
    sys.modules.pop("metrics.metrics_store", None)
    from metrics.metrics_store import MetricsStore

    return MetricsStore(db_path=str(tmp_path / "metrics.db"))


def _signal(ticker: str, size_cents: int) -> dict:
    return {
        "ticker": ticker,
        "side": "yes",
        "edge": 0.1,
        "edge_to_vig": 1.0,
        "size_cents": size_cents,
        "strategy": "green_up",
    }


def test_mark_signal_filled_does_not_raise_and_marks_latest(store):
    store.record_signal(_signal("T-A", 10))
    store.record_signal(_signal("T-A", 20))   # most recent for T-A
    store.record_signal(_signal("T-B", 30))

    # Must not raise sqlite3.OperationalError.
    store.mark_signal_filled(ticker="T-A", order_id="oid-1")

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT size_cents, filled FROM signals WHERE ticker = ? ORDER BY id",
            ("T-A",),
        ).fetchall()
        other = conn.execute(
            "SELECT filled FROM signals WHERE ticker = ?", ("T-B",)
        ).fetchone()

    # Only the latest T-A signal flips to filled; the earlier one and T-B stay.
    assert [tuple(r) for r in rows] == [(10, 0), (20, 1)]
    assert other[0] == 0


def test_mark_signal_filled_unknown_ticker_is_noop(store):
    store.record_signal(_signal("T-A", 10))
    # No matching ticker: the subquery is empty, UPDATE touches nothing.
    store.mark_signal_filled(ticker="DOES-NOT-EXIST", order_id="oid-x")

    with store._connect() as conn:
        total_filled = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE filled = 1"
        ).fetchone()[0]
    assert total_filled == 0


def test_mark_signal_filled_uses_supported_sql(store):
    """Belt-and-suspenders: the UPDATE form must be accepted by stock SQLite."""
    store.record_signal(_signal("T-C", 5))
    try:
        store.mark_signal_filled(ticker="T-C", order_id="oid-c")
    except sqlite3.OperationalError as exc:  # pragma: no cover - regression guard
        pytest.fail(f"mark_signal_filled raised on stock SQLite: {exc}")
