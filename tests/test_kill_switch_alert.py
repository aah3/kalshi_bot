"""Tests for risk/kill_switch_alert.py — the out-of-band kill-switch sentinel."""

import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def alert_module(tmp_path, monkeypatch):
    sys.modules.pop("config", None)
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import config
    importlib.reload(config)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "kalshi_bot_prod.db"))

    sys.modules.pop("risk.kill_switch_alert", None)
    import risk.kill_switch_alert as mod
    return mod


def test_alert_path_derived_from_db_path(alert_module, tmp_path):
    path = alert_module.kill_switch_alert_path()
    assert path.parent == tmp_path
    assert path.name == "kalshi_bot_prod.kill_switch_alert.json"


def test_alert_kill_switch_writes_readable_sentinel(alert_module):
    assert alert_module.read_kill_switch_alert() is None

    written = alert_module.alert_kill_switch(
        "session loss limit exceeded",
        session_pnl_cents=-521,
        limit_cents=-500,
    )
    assert written.exists()

    payload = alert_module.read_kill_switch_alert()
    assert payload is not None
    assert payload["reason"] == "session loss limit exceeded"
    assert payload["session_pnl_cents"] == -521
    assert payload["limit_cents"] == -500
    assert "ts_us" in payload and "env" in payload


def test_clear_kill_switch_alert_removes_sentinel(alert_module):
    alert_module.alert_kill_switch("max drawdown exceeded")
    assert alert_module.read_kill_switch_alert() is not None

    alert_module.clear_kill_switch_alert()
    assert alert_module.read_kill_switch_alert() is None


def test_clear_kill_switch_alert_is_a_noop_when_nothing_to_clear(alert_module):
    # Must not raise even when no sentinel exists yet.
    alert_module.clear_kill_switch_alert()
    assert alert_module.read_kill_switch_alert() is None


def test_read_kill_switch_alert_survives_corrupt_file(alert_module):
    path = alert_module.kill_switch_alert_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert alert_module.read_kill_switch_alert() is None


def test_main_kill_switch_writes_alert_before_cancelling_orders(alert_module, tmp_path):
    """main.kill_switch() must record the out-of-band alert — the whole point
    is that it survives even if order cancellation or shutdown afterward
    raises, so it has to happen first, synchronously."""
    import asyncio
    import main

    saved = (main._execution, main._shutdown_event)
    try:
        main._execution = None
        main._shutdown_event = asyncio.Event()

        asyncio.run(main.kill_switch())

        payload = alert_module.read_kill_switch_alert()
        assert payload is not None
        assert "kill switch activated" in payload["reason"]
        assert main._shutdown_event.is_set()
    finally:
        main._execution, main._shutdown_event = saved
