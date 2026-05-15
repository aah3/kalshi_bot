"""
logging_/structured_logger.py

JSON-structured logger for all bot events.

Four canonical event types:
    MARKET_PRICE_UPDATE  — order book / fair value change
    SIGNAL_GENERATED     — strategy produced a trading signal
    ORDER_SENT           — execution manager submitted an order
    FILL_RECEIVED        — exchange confirmed a fill

Every record includes:
    ts_us    — UNIX timestamp with microsecond precision
    event    — one of the four types above (or a freeform level for system logs)
    ...      — event-specific fields

Output is JSON Lines (.jsonl) — one JSON object per line — so it can be
ingested by any log aggregator (Loki, Datadog, CloudWatch, etc.).
"""

import json
import logging
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any

import config


class Event(str, Enum):
    MARKET_PRICE_UPDATE = "MARKET_PRICE_UPDATE"
    SIGNAL_GENERATED    = "SIGNAL_GENERATED"
    ORDER_SENT          = "ORDER_SENT"
    FILL_RECEIVED       = "FILL_RECEIVED"
    SYSTEM              = "SYSTEM"
    RISK_BREACH         = "RISK_BREACH"
    TOKEN_REFRESH       = "TOKEN_REFRESH"
    SHUTDOWN            = "SHUTDOWN"


class _JsonFormatter(logging.Formatter):
    """Converts a LogRecord into a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts_us": int(time.time() * 1_000_000),   # microsecond precision
            "level": record.levelname,
            "event": getattr(record, "event", Event.SYSTEM),
            "msg":   record.getMessage(),
        }
        # Merge any extra fields the caller attached
        for key, value in record.__dict__.items():
            if key not in (
                "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "name",
                "event", "message",
            ):
                payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class StructuredLogger:
    """
    Thin wrapper around Python's logging that enforces JSON output and
    provides typed convenience methods for the four canonical event types.

    Usage:
        logger = StructuredLogger("bot")
        logger.market_price_update(ticker="PRES-2024", yes_bid=52, yes_ask=54)
        logger.signal_generated(ticker="PRES-2024", side="yes", kelly_size=120)
        logger.order_sent(order_id="abc123", ticker="PRES-2024", cents=120)
        logger.fill_received(order_id="abc123", filled_cents=120, price=52)
    """

    def __init__(self, name: str = "kalshi_bot") -> None:
        self._logger = logging.getLogger(name)
        self._logger.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))

        if not self._logger.handlers:
            formatter = _JsonFormatter()

            # Always log to stderr for container / systemd compatibility
            stream_handler = logging.StreamHandler(sys.stderr)
            stream_handler.setFormatter(formatter)
            self._logger.addHandler(stream_handler)

            # Also write to the configured file (JSON Lines)
            log_path = Path(config.LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)

    # ── Canonical event helpers ─────────────────────────────────────────────

    def market_price_update(self, **fields: Any) -> None:
        self._emit(logging.INFO, Event.MARKET_PRICE_UPDATE, "market price update", **fields)

    def signal_generated(self, **fields: Any) -> None:
        self._emit(logging.INFO, Event.SIGNAL_GENERATED, "signal generated", **fields)

    def order_sent(self, **fields: Any) -> None:
        self._emit(logging.INFO, Event.ORDER_SENT, "order sent", **fields)

    def fill_received(self, **fields: Any) -> None:
        self._emit(logging.INFO, Event.FILL_RECEIVED, "fill received", **fields)

    # ── System / operational helpers ────────────────────────────────────────

    def risk_breach(self, reason: str, **fields: Any) -> None:
        self._emit(logging.CRITICAL, Event.RISK_BREACH, reason, **fields)

    def token_refresh(self, **fields: Any) -> None:
        self._emit(logging.DEBUG, Event.TOKEN_REFRESH, "token refreshed", **fields)

    def shutdown(self, **fields: Any) -> None:
        self._emit(logging.WARNING, Event.SHUTDOWN, "graceful shutdown", **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit(logging.INFO, Event.SYSTEM, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit(logging.WARNING, Event.SYSTEM, msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._emit(logging.ERROR, Event.SYSTEM, msg, **fields)

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, Event.SYSTEM, msg, **fields)

    # ── Internal ────────────────────────────────────────────────────────────

    def _emit(self, level: int, event: Event, msg: str, **fields: Any) -> None:
        extra = {"event": event, **fields}
        self._logger.log(level, msg, extra=extra)


# Module-level singleton — import and use directly:
#   from logging_.structured_logger import logger
logger = StructuredLogger()
