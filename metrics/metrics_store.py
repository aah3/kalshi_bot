"""
metrics/metrics_store.py

SQLite persistence layer for all trade and P&L data.
Used by calculator.py to compute daily dashboard metrics.

Schema:
    trades  — one row per fill received
    equity  — daily equity snapshots for drawdown / Sharpe calculation
"""

import sqlite3
import time
from contextlib import contextmanager
from typing import Any

import config
from logging_.structured_logger import logger


_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_us           INTEGER NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    size_cents      INTEGER NOT NULL,
    entry_price     INTEGER NOT NULL,
    exit_price      INTEGER,
    realised_pnl    INTEGER,
    strategy        TEXT,
    order_id        TEXT,
    is_closed       INTEGER DEFAULT 0
);
"""

_CREATE_EQUITY = """
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_us        INTEGER NOT NULL,
    equity_cents INTEGER NOT NULL
);
"""

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_us        INTEGER NOT NULL,
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL,
    edge         REAL,
    edge_to_vig  REAL,
    size_cents   INTEGER,
    strategy     TEXT,
    filled       INTEGER DEFAULT 0
);
"""


class MetricsStore:
    """Thread-safe SQLite wrapper for trade and equity data."""

    def __init__(self, db_path: str = config.DB_PATH) -> None:
        self._db_path = db_path
        self._init_schema()
        logger.info("MetricsStore initialised", db_path=db_path)

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TRADES)
            conn.execute(_CREATE_EQUITY)
            conn.execute(_CREATE_SIGNALS)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── Writes ────────────────────────────────────────────────────────────────

    def record_fill(self, fill: dict[str, Any]) -> int:
        """Insert a trade fill. Returns the new row ID."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades
                    (ts_us, ticker, side, size_cents, entry_price, strategy, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time() * 1_000_000),
                    fill.get("ticker", ""),
                    fill.get("side", ""),
                    fill.get("size_cents", 0),
                    fill.get("price", 0),
                    fill.get("strategy", ""),
                    fill.get("order_id", ""),
                ),
            )
            return cur.lastrowid

    def record_close(self, order_id: str, exit_price: int, realised_pnl: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET exit_price = ?, realised_pnl = ?, is_closed = 1
                WHERE order_id = ?
                """,
                (exit_price, realised_pnl, order_id),
            )

    def record_signal(self, signal_dict: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO signals
                    (ts_us, ticker, side, edge, edge_to_vig, size_cents, strategy)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time() * 1_000_000),
                    signal_dict.get("ticker", ""),
                    signal_dict.get("side", ""),
                    signal_dict.get("edge"),
                    signal_dict.get("edge_to_vig"),
                    signal_dict.get("size_cents", 0),
                    signal_dict.get("strategy", ""),
                ),
            )

    def mark_signal_filled(self, ticker: str, order_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE signals SET filled = 1 WHERE ticker = ? ORDER BY ts_us DESC LIMIT 1",
                (ticker,),
            )

    def record_equity_snapshot(self, equity_cents: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO equity_snapshots (ts_us, equity_cents) VALUES (?, ?)",
                (int(time.time() * 1_000_000), equity_cents),
            )

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get_closed_trades(self, days: int = 30) -> list[dict]:
        cutoff = int((time.time() - days * 86_400) * 1_000_000)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE is_closed = 1 AND ts_us >= ? ORDER BY ts_us",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_equity_series(self, days: int = 30) -> list[dict]:
        cutoff = int((time.time() - days * 86_400) * 1_000_000)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts_us, equity_cents FROM equity_snapshots WHERE ts_us >= ? ORDER BY ts_us",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_signal_fill_rate(self, days: int = 7) -> dict[str, float]:
        cutoff = int((time.time() - days * 86_400) * 1_000_000)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(filled) AS filled
                FROM signals
                WHERE ts_us >= ?
                """,
                (cutoff,),
            ).fetchone()
        total  = row["total"] or 0
        filled = row["filled"] or 0
        return {
            "total_signals": total,
            "filled":        filled,
            "fill_rate":     round(filled / total, 4) if total > 0 else 0.0,
        }
