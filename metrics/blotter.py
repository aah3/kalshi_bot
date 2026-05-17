"""
metrics/blotter.py

Production trade blotter — the authoritative record of every trade.

────────────────────────────────────────────────────────────────────────────────
SCHEMA DESIGN
────────────────────────────────────────────────────────────────────────────────

Two-table design matching your answer: individual legs AND rolled-up parent.

  trades          — one row per ORDER LEG (every individual fill)
  parent_trades   — one row per LOGICAL TRADE (entry + all hedges rolled up)

A single Kelly trade:
    parent_trades: T-001  PRES-2024-DEM  kelly  open
    trades:        L-001  parent=T-001   entry   YES  32c  10 contracts

A green-up trade:
    parent_trades: T-002  PRES-2024-DEM  green_up  open → hedged → closed
    trades:        L-002  parent=T-002   entry   YES  25c  10 contracts
    trades:        L-003  parent=T-002   hedge   NO   30c   8 contracts

An arbitrage trade:
    parent_trades: T-003  PRES-2024-DEM  arb_complementary  open
    trades:        L-004  parent=T-003   leg_1  YES  44c  5 contracts
    trades:        L-005  parent=T-003   leg_2  NO   53c  5 contracts

────────────────────────────────────────────────────────────────────────────────
COLUMNS — trades (leg level)
────────────────────────────────────────────────────────────────────────────────

  leg_id              TEXT      "L-{seq}"  human-readable
  parent_trade_id     TEXT      "T-{seq}"  links to parent_trades
  order_id            TEXT      Kalshi order UUID
  client_order_id     TEXT      our UUID sent at submission
  ticker              TEXT      e.g. "PRES-2024-DEM"
  market_title        TEXT      e.g. "Will Democrats win the presidency?"
  event_ticker        TEXT      e.g. "PRES-2024"
  category            TEXT      e.g. "Politics"
  side                TEXT      "yes" | "no"
  trade_type          TEXT      "entry" | "hedge" | "stop_loss" | "manual" | "leg_1" | "leg_2"
  contracts           INTEGER   number of contracts filled
  entry_price         INTEGER   cents (per contract)
  exit_price          INTEGER   cents — set at close/settlement
  fees_cents          INTEGER   FEE_PER_CONTRACT_CENTS × contracts
  realised_pnl_cents  INTEGER   exit_value - entry_value - fees (set at close)
  settlement_pnl_cents INTEGER  pnl from market resolution (distinct from early exit)
  status              TEXT      "open" | "closed" | "settled" | "cancelled"
  strategy            TEXT      "kelly" | "green_up_full_green" | "arbitrage" | "manual"
  strategy_meta       TEXT      JSON blob (kelly_fraction, edge, arb_type, hedge_mode …)
  entry_time          TEXT      ISO-8601 with microseconds
  exit_time           TEXT      ISO-8601 — set at close
  hold_minutes        REAL      exit_time - entry_time in minutes
  resolution          TEXT      "yes" | "no" | "void" | NULL (unresolved)
  notes               TEXT      free-text trader annotation

────────────────────────────────────────────────────────────────────────────────
COLUMNS — parent_trades (logical trade level)
────────────────────────────────────────────────────────────────────────────────

  trade_id            TEXT      "T-{seq}"
  ticker              TEXT
  market_title        TEXT
  event_ticker        TEXT
  category            TEXT
  strategy            TEXT
  trade_type          TEXT      "single" | "multi_leg"
  status              TEXT      "open" | "partially_hedged" | "hedged" |
                                "closed" | "settled" | "stopped"
  num_legs            INTEGER   how many leg rows belong to this parent
  total_contracts     INTEGER   contracts across all legs (entry side)
  total_cost_cents    INTEGER   sum of (entry_price × contracts) for entry legs
  total_fees_cents    INTEGER   sum of all leg fees
  total_realised_pnl  INTEGER   sum of all leg realised P&L (set at close)
  total_settlement_pnl INTEGER  set when market resolves
  net_pnl_cents       INTEGER   realised + settlement - fees
  entry_time          TEXT      time of first leg fill
  exit_time           TEXT      time of last leg close/settlement
  hold_minutes        REAL
  resolution          TEXT
  notes               TEXT

────────────────────────────────────────────────────────────────────────────────
DATABASE BACKEND
────────────────────────────────────────────────────────────────────────────────

SQLite by default. Set KALSHI_POSTGRES_URL env var to switch to Postgres.
The Blotter class abstracts the connection so callers don't need to know which.
All parameterised queries use ? (SQLite) or %s (Postgres) automatically.
"""

import csv
import io
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generator

import config
from logging_.structured_logger import logger


# ── Schema SQL ────────────────────────────────────────────────────────────────

_CREATE_LEGS = """
CREATE TABLE IF NOT EXISTS trades (
    leg_id               TEXT PRIMARY KEY,
    parent_trade_id      TEXT NOT NULL,
    order_id             TEXT,
    client_order_id      TEXT,
    ticker               TEXT NOT NULL,
    market_title         TEXT DEFAULT '',
    event_ticker         TEXT DEFAULT '',
    category             TEXT DEFAULT 'Unknown',
    side                 TEXT NOT NULL,
    trade_type           TEXT NOT NULL DEFAULT 'entry',
    contracts            INTEGER NOT NULL DEFAULT 0,
    entry_price          INTEGER NOT NULL DEFAULT 0,
    exit_price           INTEGER,
    fees_cents           INTEGER NOT NULL DEFAULT 0,
    realised_pnl_cents   INTEGER,
    settlement_pnl_cents INTEGER,
    status               TEXT NOT NULL DEFAULT 'open',
    strategy             TEXT DEFAULT '',
    strategy_meta        TEXT DEFAULT '{}',
    entry_time           TEXT NOT NULL,
    exit_time            TEXT,
    hold_minutes         REAL,
    resolution           TEXT,
    notes                TEXT DEFAULT ''
);
"""

_CREATE_PARENTS = """
CREATE TABLE IF NOT EXISTS parent_trades (
    trade_id             TEXT PRIMARY KEY,
    ticker               TEXT NOT NULL,
    market_title         TEXT DEFAULT '',
    event_ticker         TEXT DEFAULT '',
    category             TEXT DEFAULT 'Unknown',
    strategy             TEXT DEFAULT '',
    trade_type           TEXT NOT NULL DEFAULT 'single',
    status               TEXT NOT NULL DEFAULT 'open',
    num_legs             INTEGER NOT NULL DEFAULT 0,
    total_contracts      INTEGER NOT NULL DEFAULT 0,
    total_cost_cents     INTEGER NOT NULL DEFAULT 0,
    total_fees_cents     INTEGER NOT NULL DEFAULT 0,
    total_realised_pnl   INTEGER,
    total_settlement_pnl INTEGER,
    net_pnl_cents        INTEGER,
    entry_time           TEXT NOT NULL,
    exit_time            TEXT,
    hold_minutes         REAL,
    resolution           TEXT,
    notes                TEXT DEFAULT ''
);
"""

_CREATE_SEQUENCES = """
CREATE TABLE IF NOT EXISTS id_sequences (
    name    TEXT PRIMARY KEY,
    next_val INTEGER NOT NULL DEFAULT 1
);
"""

_IDX_LEGS    = "CREATE INDEX IF NOT EXISTS idx_legs_parent    ON trades(parent_trade_id);"
_IDX_LEGS_TK = "CREATE INDEX IF NOT EXISTS idx_legs_ticker    ON trades(ticker);"
_IDX_LEGS_ST = "CREATE INDEX IF NOT EXISTS idx_legs_status    ON trades(status);"
_IDX_PAR_TK  = "CREATE INDEX IF NOT EXISTS idx_par_ticker     ON parent_trades(ticker);"
_IDX_PAR_ST  = "CREATE INDEX IF NOT EXISTS idx_par_status     ON parent_trades(status);"
_IDX_PAR_CAT = "CREATE INDEX IF NOT EXISTS idx_par_category   ON parent_trades(category);"
_IDX_PAR_STR = "CREATE INDEX IF NOT EXISTS idx_par_strategy   ON parent_trades(strategy);"


# ── Helper dataclasses ────────────────────────────────────────────────────────

@dataclass
class LegRecord:
    """Mirrors the trades table. Returned by blotter queries."""
    leg_id:              str
    parent_trade_id:     str
    order_id:            str
    ticker:              str
    market_title:        str
    event_ticker:        str
    category:            str
    side:                str
    trade_type:          str
    contracts:           int
    entry_price:         int
    exit_price:          int | None
    fees_cents:          int
    realised_pnl_cents:  int | None
    settlement_pnl_cents: int | None
    status:              str
    strategy:            str
    strategy_meta:       dict
    entry_time:          str
    exit_time:           str | None
    hold_minutes:        float | None
    resolution:          str | None
    notes:               str

    @property
    def entry_cost_cents(self) -> int:
        return self.entry_price * self.contracts

    @property
    def max_payout_cents(self) -> int:
        return 100 * self.contracts

    @property
    def net_pnl(self) -> int | None:
        pnl = self.realised_pnl_cents or self.settlement_pnl_cents
        if pnl is None:
            return None
        return pnl - self.fees_cents

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_id":             self.leg_id,
            "parent_trade_id":    self.parent_trade_id,
            "order_id":           self.order_id,
            "ticker":             self.ticker,
            "market_title":       self.market_title,
            "category":           self.category,
            "side":               self.side,
            "trade_type":         self.trade_type,
            "contracts":          self.contracts,
            "entry_price_cents":  self.entry_price,
            "exit_price_cents":   self.exit_price,
            "entry_cost_usd":     round(self.entry_cost_cents / 100, 2),
            "max_payout_usd":     round(self.max_payout_cents / 100, 2),
            "fees_usd":           round(self.fees_cents / 100, 2),
            "realised_pnl_usd":   round(self.realised_pnl_cents / 100, 2) if self.realised_pnl_cents is not None else None,
            "settlement_pnl_usd": round(self.settlement_pnl_cents / 100, 2) if self.settlement_pnl_cents is not None else None,
            "net_pnl_usd":        round(self.net_pnl / 100, 2) if self.net_pnl is not None else None,
            "status":             self.status,
            "strategy":           self.strategy,
            "entry_time":         self.entry_time,
            "exit_time":          self.exit_time,
            "hold_minutes":       round(self.hold_minutes, 1) if self.hold_minutes else None,
            "resolution":         self.resolution,
            "notes":              self.notes,
        }


@dataclass
class ParentRecord:
    """Mirrors the parent_trades table. Returned by blotter queries."""
    trade_id:            str
    ticker:              str
    market_title:        str
    event_ticker:        str
    category:            str
    strategy:            str
    trade_type:          str
    status:              str
    num_legs:            int
    total_contracts:     int
    total_cost_cents:    int
    total_fees_cents:    int
    total_realised_pnl:  int | None
    total_settlement_pnl: int | None
    net_pnl_cents:       int | None
    entry_time:          str
    exit_time:           str | None
    hold_minutes:        float | None
    resolution:          str | None
    notes:               str

    @property
    def net_pnl_usd(self) -> float | None:
        return round(self.net_pnl_cents / 100, 2) if self.net_pnl_cents is not None else None

    @property
    def roi_pct(self) -> float | None:
        if not self.total_cost_cents or self.net_pnl_cents is None:
            return None
        return round(self.net_pnl_cents / self.total_cost_cents * 100, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id":           self.trade_id,
            "ticker":             self.ticker,
            "market_title":       self.market_title,
            "category":           self.category,
            "strategy":           self.strategy,
            "trade_type":         self.trade_type,
            "status":             self.status,
            "num_legs":           self.num_legs,
            "total_contracts":    self.total_contracts,
            "total_cost_usd":     round(self.total_cost_cents / 100, 2),
            "total_fees_usd":     round(self.total_fees_cents / 100, 2),
            "net_pnl_usd":        self.net_pnl_usd,
            "roi_pct":            self.roi_pct,
            "entry_time":         self.entry_time,
            "exit_time":          self.exit_time,
            "hold_minutes":       round(self.hold_minutes, 1) if self.hold_minutes else None,
            "resolution":         self.resolution,
            "notes":              self.notes,
        }


# ── Blotter ───────────────────────────────────────────────────────────────────

class Blotter:
    """
    Production trade blotter.

    Thread-safe SQLite (or Postgres) persistence layer for all trade lifecycle
    events: open, partial fills, full fills, hedges, closes, and settlement.

    Usage:
        blotter = Blotter()

        # Open a new single-leg trade (Kelly strategy)
        trade_id = blotter.open_trade(
            ticker="PRES-2024-DEM",
            market_title="Will Democrats win the presidency?",
            category="Politics",
            strategy="kelly",
        )
        leg_id = blotter.record_fill(
            parent_trade_id=trade_id,
            order_id="abc-123",
            side="yes",
            trade_type="entry",
            contracts=10,
            entry_price=32,
            strategy="kelly",
            strategy_meta={"edge": 0.12, "kelly_fraction": 0.25},
        )

        # Later: close it (early exit or settlement)
        blotter.close_leg(leg_id, exit_price=68, close_type="manual")
        blotter.close_trade(trade_id)
    """

    def __init__(self, db_path: str = config.DB_PATH) -> None:
        self._db_path   = db_path
        self._postgres  = config.USE_POSTGRES
        self._pg_url    = config.POSTGRES_URL
        # For in-memory SQLite, keep a single persistent connection
        # (each new connect(':memory:') creates an independent empty DB)
        self._mem_conn  = None
        if not self._postgres and db_path == ":memory:":
            import sqlite3 as _sq
            self._mem_conn = _sq.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = _sq.Row
        self._init_schema()
        logger.info("Blotter initialised", db_path=db_path, backend="postgres" if self._postgres else "sqlite")

    # ── Schema init ───────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._conn() as conn:
            from metrics.metrics_store import _migrate_legacy_tables
            _migrate_legacy_tables(conn)
            conn.execute(_CREATE_LEGS)
            conn.execute(_CREATE_PARENTS)
            conn.execute(_CREATE_SEQUENCES)
            conn.execute(_IDX_LEGS)
            conn.execute(_IDX_LEGS_TK)
            conn.execute(_IDX_LEGS_ST)
            conn.execute(_IDX_PAR_TK)
            conn.execute(_IDX_PAR_ST)
            conn.execute(_IDX_PAR_CAT)
            conn.execute(_IDX_PAR_STR)
            # Seed sequences if not present
            conn.execute("INSERT OR IGNORE INTO id_sequences (name, next_val) VALUES ('trade', 1)")
            conn.execute("INSERT OR IGNORE INTO id_sequences (name, next_val) VALUES ('leg', 1)")

    # ── ID generation ─────────────────────────────────────────────────────────

    def _next_id(self, conn, sequence: str, prefix: str) -> str:
        """Atomic sequence increment — returns e.g. 'T-0042' or 'L-0187'."""
        row = conn.execute(
            "SELECT next_val FROM id_sequences WHERE name = ?", (sequence,)
        ).fetchone()
        val = row[0]
        conn.execute(
            "UPDATE id_sequences SET next_val = ? WHERE name = ?", (val + 1, sequence)
        )
        return f"{prefix}-{val:04d}"

    # ── Write: open a parent trade ────────────────────────────────────────────

    def open_trade(
        self,
        ticker:        str,
        market_title:  str       = "",
        event_ticker:  str       = "",
        category:      str       = "Unknown",
        strategy:      str       = "",
        trade_type:    str       = "single",   # "single" | "multi_leg"
        notes:         str       = "",
    ) -> str:
        """
        Open a new parent trade record. Returns the trade_id ("T-NNNN").
        Call this once per logical trade, before recording any fills.
        """
        now = _now_iso()
        with self._conn() as conn:
            trade_id = self._next_id(conn, "trade", "T")
            conn.execute(
                """
                INSERT INTO parent_trades
                    (trade_id, ticker, market_title, event_ticker, category,
                     strategy, trade_type, status, num_legs, total_contracts,
                     total_cost_cents, total_fees_cents, entry_time, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, 0, 0, 0, ?, ?)
                """,
                (trade_id, ticker, market_title, event_ticker, category,
                 strategy, trade_type, now, notes),
            )
        logger.info("Blotter: trade opened", trade_id=trade_id, ticker=ticker, strategy=strategy)
        return trade_id

    # ── Write: record a fill (leg) ────────────────────────────────────────────

    def record_fill(
        self,
        parent_trade_id: str,
        order_id:        str,
        side:            str,            # "yes" | "no"
        trade_type:      str,            # "entry"|"hedge"|"stop_loss"|"manual"|"leg_1"|"leg_2"
        contracts:       int,
        entry_price:     int,            # cents
        strategy:        str       = "",
        strategy_meta:   dict      = None,
        market_title:    str       = "",
        event_ticker:    str       = "",
        category:        str       = "Unknown",
        client_order_id: str       = "",
        notes:           str       = "",
    ) -> str:
        """
        Record a confirmed order fill as a leg row.
        Also updates the parent_trade totals atomically.
        Returns the leg_id ("L-NNNN").
        """
        now        = _now_iso()
        fees       = int(config.FEE_PER_CONTRACT_CENTS * contracts)
        meta_str   = json.dumps(strategy_meta or {})
        cost       = entry_price * contracts

        # Fetch parent ticker/category for leg row if not supplied
        with self._conn() as conn:
            if not market_title or not category:
                parent = conn.execute(
                    "SELECT ticker, market_title, event_ticker, category FROM parent_trades WHERE trade_id = ?",
                    (parent_trade_id,),
                ).fetchone()
                if parent:
                    market_title  = market_title  or parent[1]
                    event_ticker  = event_ticker  or parent[2]
                    category      = category      if category != "Unknown" else parent[3]

            leg_id = self._next_id(conn, "leg", "L")

            conn.execute(
                """
                INSERT INTO trades
                    (leg_id, parent_trade_id, order_id, client_order_id,
                     ticker, market_title, event_ticker, category,
                     side, trade_type, contracts, entry_price,
                     fees_cents, status, strategy, strategy_meta,
                     entry_time, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (leg_id, parent_trade_id, order_id, client_order_id,
                 conn.execute("SELECT ticker FROM parent_trades WHERE trade_id=?", (parent_trade_id,)).fetchone()[0],
                 market_title, event_ticker, category,
                 side, trade_type, contracts, entry_price,
                 fees, strategy, meta_str, now, notes),
            )

            # Update parent totals
            conn.execute(
                """
                UPDATE parent_trades SET
                    num_legs        = num_legs + 1,
                    total_contracts = total_contracts + ?,
                    total_cost_cents = total_cost_cents + ?,
                    total_fees_cents = total_fees_cents + ?,
                    market_title    = CASE WHEN market_title = '' THEN ? ELSE market_title END,
                    event_ticker    = CASE WHEN event_ticker = '' THEN ? ELSE event_ticker END,
                    category        = CASE WHEN category = 'Unknown' THEN ? ELSE category END
                WHERE trade_id = ?
                """,
                (contracts, cost, fees, market_title, event_ticker, category, parent_trade_id),
            )

        logger.info(
            "Blotter: fill recorded",
            leg_id=leg_id,
            parent_trade_id=parent_trade_id,
            trade_type=trade_type,
            side=side,
            contracts=contracts,
            entry_price=entry_price,
            fees_cents=fees,
        )
        return leg_id

    # ── Write: close a leg ────────────────────────────────────────────────────

    def close_leg(
        self,
        leg_id:      str,
        exit_price:  int,         # cents
        close_type:  str = "exit",  # "exit" | "settlement" | "stop_loss"
        resolution:  str | None = None,  # "yes" | "no" | "void"
        notes:       str = "",
    ) -> int:
        """
        Mark a leg as closed. Computes and stores realised P&L.

        P&L formula (per leg):
            entry legs:  pnl = (exit_price - entry_price) × contracts − fees
            hedge legs:  pnl = (exit_price − entry_price) × contracts − fees
            settlement:  if resolution matches side → pnl = (100 - entry_price) × contracts - fees
                         else                       → pnl = -entry_price × contracts - fees

        Returns realised_pnl_cents.
        """
        now = _now_iso()
        with self._conn() as conn:
            leg = conn.execute(
                "SELECT contracts, entry_price, fees_cents, entry_time, side FROM trades WHERE leg_id = ?",
                (leg_id,),
            ).fetchone()
            if not leg:
                logger.error("Blotter: leg not found for close", leg_id=leg_id)
                return 0

            contracts, entry_price, fees, entry_time_str, side = leg

            if close_type == "settlement" and resolution:
                # Binary settlement: contract pays 100c to winner, 0 to loser
                won = (resolution == side)
                payout_per_contract = 100 if won else 0
                pnl = (payout_per_contract - entry_price) * contracts - fees
                settlement_pnl = pnl
                realised_pnl   = None
            else:
                pnl            = (exit_price - entry_price) * contracts - fees
                realised_pnl   = pnl
                settlement_pnl = None

            hold_min = _hold_minutes(entry_time_str, now)

            conn.execute(
                """
                UPDATE trades SET
                    exit_price           = ?,
                    realised_pnl_cents   = ?,
                    settlement_pnl_cents = ?,
                    status               = ?,
                    exit_time            = ?,
                    hold_minutes         = ?,
                    resolution           = ?,
                    notes                = CASE WHEN ? != '' THEN ? ELSE notes END
                WHERE leg_id = ?
                """,
                (
                    exit_price,
                    realised_pnl,
                    settlement_pnl,
                    "settled" if close_type == "settlement" else "closed",
                    now,
                    hold_min,
                    resolution,
                    notes, notes,
                    leg_id,
                ),
            )

        logger.info(
            "Blotter: leg closed",
            leg_id=leg_id,
            exit_price=exit_price,
            pnl_cents=pnl,
            pnl_usd=round(pnl / 100, 2),
            close_type=close_type,
            resolution=resolution,
        )
        return pnl

    # ── Write: close a parent trade ───────────────────────────────────────────

    def close_trade(
        self,
        trade_id:    str,
        resolution:  str | None = None,
        notes:       str = "",
    ) -> None:
        """
        Roll up all closed legs into the parent_trade record.
        Call this after all legs of a logical trade are closed.
        """
        now = _now_iso()
        with self._conn() as conn:
            legs = conn.execute(
                """
                SELECT realised_pnl_cents, settlement_pnl_cents, fees_cents,
                       entry_time, exit_time, contracts, entry_price
                FROM trades WHERE parent_trade_id = ?
                """,
                (trade_id,),
            ).fetchall()

            if not legs:
                return

            total_realised    = sum(r[0] or 0 for r in legs)
            total_settlement  = sum(r[1] or 0 for r in legs)
            total_fees        = sum(r[2] or 0 for r in legs)
            net_pnl           = total_realised + total_settlement   # fees already baked in

            entry_times = [r[3] for r in legs if r[3]]
            exit_times  = [r[4] for r in legs if r[4]]
            first_entry = min(entry_times) if entry_times else now
            last_exit   = max(exit_times)  if exit_times  else now
            hold_min    = _hold_minutes(first_entry, last_exit)

            status = "settled" if any(r[1] is not None for r in legs) else "closed"

            conn.execute(
                """
                UPDATE parent_trades SET
                    status               = ?,
                    total_realised_pnl   = ?,
                    total_settlement_pnl = ?,
                    net_pnl_cents        = ?,
                    exit_time            = ?,
                    hold_minutes         = ?,
                    resolution           = ?,
                    notes                = CASE WHEN ? != '' THEN ? ELSE notes END
                WHERE trade_id = ?
                """,
                (
                    status,
                    total_realised if total_realised else None,
                    total_settlement if total_settlement else None,
                    net_pnl,
                    last_exit,
                    hold_min,
                    resolution,
                    notes, notes,
                    trade_id,
                ),
            )

        logger.info(
            "Blotter: trade closed",
            trade_id=trade_id,
            net_pnl_cents=net_pnl,
            net_pnl_usd=round(net_pnl / 100, 2),
            status=status,
        )

    # ── Write: annotate ───────────────────────────────────────────────────────

    def annotate_trade(self, trade_id: str, notes: str) -> None:
        """Add or update trader notes on a parent trade."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE parent_trades SET notes = ? WHERE trade_id = ?",
                (notes, trade_id),
            )

    def annotate_leg(self, leg_id: str, notes: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET notes = ? WHERE leg_id = ?",
                (notes, leg_id),
            )

    # ── Read: single record ───────────────────────────────────────────────────

    def get_trade(self, trade_id: str) -> ParentRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM parent_trades WHERE trade_id = ?", (trade_id,)
            ).fetchone()
        return _row_to_parent(row) if row else None

    def get_leg(self, leg_id: str) -> LegRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE leg_id = ?", (leg_id,)
            ).fetchone()
        return _row_to_leg(row) if row else None

    def get_legs_for_trade(self, trade_id: str) -> list[LegRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE parent_trade_id = ? ORDER BY entry_time",
                (trade_id,),
            ).fetchall()
        return [_row_to_leg(r) for r in rows]

    # ── Read: query blotter ───────────────────────────────────────────────────

    def query_trades(
        self,
        status:     str | None  = None,      # "open"|"closed"|"settled"|None=all
        category:   str | None  = None,
        strategy:   str | None  = None,
        ticker:     str | None  = None,
        trade_id:   str | None  = None,
        from_date:  str | None  = None,      # "YYYY-MM-DD"
        to_date:    str | None  = None,
        days:       int | None  = None,      # shortcut: last N days
        resolution: str | None  = None,
        limit:      int         = 200,
    ) -> list[ParentRecord]:
        """
        Flexible blotter query. All filters are optional and combinable.

        Examples:
            blotter.query_trades(status="closed", category="Politics")
            blotter.query_trades(strategy="green_up_full_green", days=7)
            blotter.query_trades(ticker="PRES-2024-DEM")
            blotter.query_trades(resolution="yes", days=30)
        """
        clauses, params = [], []

        if trade_id:
            clauses.append("trade_id = ?");   params.append(trade_id)
        if status:
            clauses.append("status = ?");     params.append(status)
        if category:
            clauses.append("category = ?");   params.append(category)
        if strategy:
            clauses.append("strategy LIKE ?"); params.append(f"%{strategy}%")
        if ticker:
            clauses.append("ticker = ?");     params.append(ticker)
        if resolution:
            clauses.append("resolution = ?"); params.append(resolution)
        if days:
            cutoff = _days_ago_iso(days)
            clauses.append("entry_time >= ?"); params.append(cutoff)
        elif from_date:
            clauses.append("entry_time >= ?"); params.append(from_date)
        if to_date:
            clauses.append("entry_time <= ?"); params.append(to_date + "T23:59:59")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql   = f"SELECT * FROM parent_trades {where} ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_parent(r) for r in rows]

    def query_legs(
        self,
        parent_trade_id: str | None = None,
        ticker:          str | None = None,
        trade_type:      str | None = None,
        status:          str | None = None,
        resolution:      str | None = None,
        from_date:       str | None = None,
        to_date:         str | None = None,
        days:            int | None = None,
        limit:           int        = 500,
    ) -> list[LegRecord]:
        clauses, params = [], []
        if parent_trade_id:
            clauses.append("parent_trade_id = ?"); params.append(parent_trade_id)
        if ticker:
            clauses.append("ticker = ?");          params.append(ticker)
        if trade_type:
            clauses.append("trade_type = ?");      params.append(trade_type)
        if status:
            clauses.append("status = ?");          params.append(status)
        if resolution:
            clauses.append("resolution = ?");      params.append(resolution)
        if days:
            clauses.append("entry_time >= ?");     params.append(_days_ago_iso(days))
        elif from_date:
            clauses.append("entry_time >= ?");     params.append(from_date)
        if to_date:
            clauses.append("entry_time <= ?");     params.append(to_date + "T23:59:59")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql   = f"SELECT * FROM trades {where} ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_leg(r) for r in rows]

    # ── Read: P&L summaries ───────────────────────────────────────────────────

    def pnl_by_strategy(self, days: int = 30) -> list[dict]:
        """Aggregate net P&L and trade count grouped by strategy."""
        cutoff = _days_ago_iso(days)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    strategy,
                    COUNT(*)                          AS trade_count,
                    SUM(CASE WHEN net_pnl_cents > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN net_pnl_cents < 0 THEN 1 ELSE 0 END) AS losses,
                    SUM(net_pnl_cents)                AS total_pnl_cents,
                    AVG(net_pnl_cents)                AS avg_pnl_cents,
                    SUM(total_fees_cents)             AS total_fees_cents,
                    AVG(hold_minutes)                 AS avg_hold_minutes
                FROM parent_trades
                WHERE status IN ('closed','settled')
                  AND entry_time >= ?
                GROUP BY strategy
                ORDER BY total_pnl_cents DESC
                """,
                (cutoff,),
            ).fetchall()
        return [_summary_row(r) for r in rows]

    def pnl_by_category(self, days: int = 30) -> list[dict]:
        """Aggregate net P&L grouped by market category."""
        cutoff = _days_ago_iso(days)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    category,
                    COUNT(*)                          AS trade_count,
                    SUM(CASE WHEN net_pnl_cents > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN net_pnl_cents < 0 THEN 1 ELSE 0 END) AS losses,
                    SUM(net_pnl_cents)                AS total_pnl_cents,
                    AVG(net_pnl_cents)                AS avg_pnl_cents,
                    SUM(total_fees_cents)             AS total_fees_cents,
                    AVG(hold_minutes)                 AS avg_hold_minutes
                FROM parent_trades
                WHERE status IN ('closed','settled')
                  AND entry_time >= ?
                GROUP BY category
                ORDER BY total_pnl_cents DESC
                """,
                (cutoff,),
            ).fetchall()
        return [_summary_row(r) for r in rows]

    def open_positions_summary(self) -> list[dict]:
        """All open parent trades with their current cost basis."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT trade_id, ticker, market_title, category, strategy,
                       total_contracts, total_cost_cents, total_fees_cents,
                       num_legs, entry_time
                FROM parent_trades
                WHERE status = 'open'
                ORDER BY entry_time DESC
                """,
            ).fetchall()
        return [dict(zip(
            ["trade_id","ticker","market_title","category","strategy",
             "total_contracts","total_cost_cents","total_fees_cents",
             "num_legs","entry_time"], r
        )) for r in rows]

    def best_trades(self, n: int = 10, days: int = 30) -> list[ParentRecord]:
        cutoff = _days_ago_iso(days)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM parent_trades
                WHERE status IN ('closed','settled') AND entry_time >= ?
                ORDER BY net_pnl_cents DESC LIMIT ?
                """,
                (cutoff, n),
            ).fetchall()
        return [_row_to_parent(r) for r in rows]

    def worst_trades(self, n: int = 10, days: int = 30) -> list[ParentRecord]:
        cutoff = _days_ago_iso(days)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM parent_trades
                WHERE status IN ('closed','settled') AND entry_time >= ?
                ORDER BY net_pnl_cents ASC LIMIT ?
                """,
                (cutoff, n),
            ).fetchall()
        return [_row_to_parent(r) for r in rows]

    # ── Export ────────────────────────────────────────────────────────────────

    def export_csv(
        self,
        records: list[ParentRecord] | list[LegRecord],
        filepath: str | None = None,
    ) -> str:
        """
        Export a list of records to CSV.

        If filepath is None, returns the CSV as a string.
        If filepath is given, writes to disk and returns the path.
        """
        if not records:
            return ""

        rows  = [r.to_dict() for r in records]
        keys  = list(rows[0].keys())
        buf   = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
        csv_str = buf.getvalue()

        if filepath:
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                f.write(csv_str)
            logger.info("Blotter: CSV exported", filepath=filepath, rows=len(rows))
            return filepath

        return csv_str

    # ── Connection management ─────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator:
        """Route to sqlite or postgres backend."""
        cm = self._pg_conn() if self._postgres else self._sqlite_conn()
        with cm as conn:
            yield conn

    @contextmanager
    def _sqlite_conn(self):
        if self._mem_conn is not None:
            # In-memory mode: reuse the single persistent connection
            self._mem_conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield self._mem_conn
                self._mem_conn.commit()
            except Exception:
                self._mem_conn.rollback()
                raise
        else:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def _pg_conn(self):
        """Postgres connection — requires psycopg2-binary."""
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise RuntimeError(
                "psycopg2-binary is required for Postgres support. "
                "Run: pip install psycopg2-binary"
            )
        conn = psycopg2.connect(self._pg_url)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ── Row parsers ───────────────────────────────────────────────────────────────

def _row_to_parent(row) -> ParentRecord:
    r = dict(row)
    return ParentRecord(
        trade_id=r["trade_id"],
        ticker=r["ticker"],
        market_title=r.get("market_title",""),
        event_ticker=r.get("event_ticker",""),
        category=r.get("category","Unknown"),
        strategy=r.get("strategy",""),
        trade_type=r.get("trade_type","single"),
        status=r["status"],
        num_legs=r.get("num_legs",0),
        total_contracts=r.get("total_contracts",0),
        total_cost_cents=r.get("total_cost_cents",0),
        total_fees_cents=r.get("total_fees_cents",0),
        total_realised_pnl=r.get("total_realised_pnl"),
        total_settlement_pnl=r.get("total_settlement_pnl"),
        net_pnl_cents=r.get("net_pnl_cents"),
        entry_time=r["entry_time"],
        exit_time=r.get("exit_time"),
        hold_minutes=r.get("hold_minutes"),
        resolution=r.get("resolution"),
        notes=r.get("notes",""),
    )


def _row_to_leg(row) -> LegRecord:
    r = dict(row)
    meta = {}
    try:
        meta = json.loads(r.get("strategy_meta") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    return LegRecord(
        leg_id=r["leg_id"],
        parent_trade_id=r["parent_trade_id"],
        order_id=r.get("order_id",""),
        ticker=r["ticker"],
        market_title=r.get("market_title",""),
        event_ticker=r.get("event_ticker",""),
        category=r.get("category","Unknown"),
        side=r["side"],
        trade_type=r.get("trade_type","entry"),
        contracts=r.get("contracts",0),
        entry_price=r.get("entry_price",0),
        exit_price=r.get("exit_price"),
        fees_cents=r.get("fees_cents",0),
        realised_pnl_cents=r.get("realised_pnl_cents"),
        settlement_pnl_cents=r.get("settlement_pnl_cents"),
        status=r["status"],
        strategy=r.get("strategy",""),
        strategy_meta=meta,
        entry_time=r["entry_time"],
        exit_time=r.get("exit_time"),
        hold_minutes=r.get("hold_minutes"),
        resolution=r.get("resolution"),
        notes=r.get("notes",""),
    )


def _summary_row(row) -> dict:
    r = dict(row)
    tc = r.get("trade_count",0)
    wins = r.get("wins",0)
    return {
        "group":            r.get("strategy") or r.get("category",""),
        "trade_count":      tc,
        "wins":             wins,
        "losses":           r.get("losses",0),
        "win_rate_pct":     round(wins / tc * 100, 1) if tc else 0.0,
        "total_pnl_usd":    round((r.get("total_pnl_cents") or 0) / 100, 2),
        "avg_pnl_usd":      round((r.get("avg_pnl_cents") or 0) / 100, 2),
        "total_fees_usd":   round((r.get("total_fees_cents") or 0) / 100, 2),
        "avg_hold_minutes": round(r.get("avg_hold_minutes") or 0, 1),
    }


# ── Time helpers ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _days_ago_iso(days: int) -> str:
    from datetime import timedelta
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _hold_minutes(start_iso: str, end_iso: str) -> float:
    """Return elapsed minutes between two ISO-8601 strings."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%S.%f+00:00"
        start = datetime.strptime(start_iso, fmt).replace(tzinfo=timezone.utc)
        end   = datetime.strptime(end_iso,   fmt).replace(tzinfo=timezone.utc)
        return (end - start).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 0.0
