"""
risk/kill_switch_alert.py

Out-of-band kill-switch alerting.

The circuit breaker's kill switch (``main.kill_switch()``) already logs a
CRITICAL ``risk_breach`` line, but that line goes through the buffered JSONL
file handler and can be lost if the process exits before the handler
flushes (see docs/ROADMAP.md — "RISK_BREACH lines appeared on console
(--quiet) but were not flushed to kalshi_bot_prod.jsonl before exit").

This module writes a small, immediately-fsync'd sentinel file the moment the
kill switch fires, independent of the logging pipeline, so:

  - ``scripts/watch_kill_switch.ps1`` (or any external tail/watcher) can
    detect a trip even if the log line never made it to disk
  - the operator has one unambiguous "the bot kill-switched" artifact with
    the reason and timestamp for post-mortems

Scope — this module ONLY records an alert marker. It never cancels orders,
never flattens positions, and never touches the trading process; that stays
entirely inside ``CircuitBreaker`` / ``ExecutionManager.cancel_all_orders()``.
Watcher scripts built on top of this must never call ``Stop-Process`` —
shutdown is cooperative (cancel resting orders + exit), not a kill.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import config


def kill_switch_alert_path() -> Path:
    """
    Location of the kill-switch sentinel file.

    Always derived from ``config.DB_PATH`` so demo and prod runs (which use
    separate, isolated DB paths per ``docs/STRATEGY_INSTANCE.md``) never
    clobber each other's alert file, and a watcher pointed at one instance's
    DB directory only ever sees that instance's trips.
    """
    db_path = Path(config.DB_PATH)
    stem = db_path.stem or "kalshi_bot"
    return db_path.parent / f"{stem}.kill_switch_alert.json"


def alert_kill_switch(reason: str, **fields: Any) -> Path:
    """
    Write (or overwrite) the kill-switch sentinel file with the trip
    reason, timestamp, environment, and any extra risk fields (e.g.
    ``session_pnl_cents``, ``limit_cents``).

    Call this once, synchronously, as the very first thing the kill switch
    does — before cancelling orders — so the alert exists even if
    cancellation or shutdown afterward raises.

    Returns the path written (useful for logging/testing).
    """
    payload: dict[str, Any] = {
        "ts_us":  int(time.time() * 1_000_000),
        "reason": reason,
        "env":    getattr(config, "ENV", "unknown"),
        **fields,
    }
    path = kill_switch_alert_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Flush + fsync so the marker survives even a process exit immediately
    # after the kill switch fires — the exact failure mode this module
    # exists to route around.
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())

    return path


def clear_kill_switch_alert() -> None:
    """
    Remove any stale sentinel left over from a previous run.

    Call this at process startup, before the circuit breaker can trip, so a
    fresh session never starts already looking tripped to a watcher.
    """
    path = kill_switch_alert_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def read_kill_switch_alert() -> dict[str, Any] | None:
    """
    Read the current sentinel, if any.

    Returns None when no kill switch has fired since the last
    ``clear_kill_switch_alert()`` call, or the file is missing/corrupt.
    Used by tests and by operator tooling (e.g. a watch script polling for
    the marker instead of tailing the full JSONL log).
    """
    path = kill_switch_alert_path()
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
