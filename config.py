"""
config.py — single source of truth for all tunable parameters.

Credentials (both environments can live in .env at once):
    KALSHI_ENV=demo | production
    KALSHI_DEMO_API_KEY_ID / KALSHI_DEMO_PRIVATE_KEY_B64
    KALSHI_PROD_API_KEY_ID / KALSHI_PROD_PRIVATE_KEY_B64

Risk / paths (per-environment overrides — switch with KALSHI_ENV only):
    KALSHI_DEMO_MAX_POSITION_CENTS / KALSHI_PROD_MAX_POSITION_CENTS
    KALSHI_DEMO_DAILY_LOSS_LIMIT_CENTS / KALSHI_PROD_DAILY_LOSS_LIMIT_CENTS
    KALSHI_DEMO_DB_PATH / KALSHI_PROD_DB_PATH
    KALSHI_DEMO_LOG_FILE / KALSHI_PROD_LOG_FILE
    (Generic KALSHI_MAX_POSITION_CENTS etc. apply to both if set and no per-env value)

On Windows, put assignments in `.env` next to this module; shell exports win if already set.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def _configure_stdio_utf8() -> None:
    """Windows consoles default to cp1252; Kalshi CLI output uses UTF-8 symbols."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio_utf8()

# ─── Environment ─────────────────────────────────────────────────────────────

ENV = os.getenv("KALSHI_ENV", "demo").lower().strip()   # "demo" | "production"
IS_PRODUCTION = ENV == "production"
_ENV_PREFIX = "PROD" if IS_PRODUCTION else "DEMO"

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_WS_URL   = "wss://demo-api.kalshi.co/trade-api/ws/v2"
PROD_WS_URL   = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

BASE_URL = PROD_BASE_URL if IS_PRODUCTION else DEMO_BASE_URL
WS_URL   = PROD_WS_URL   if IS_PRODUCTION else DEMO_WS_URL


def _prefixed_env(suffix: str) -> str | None:
    """KALSHI_DEMO_* or KALSHI_PROD_* for the active environment."""
    val = os.getenv(f"KALSHI_{_ENV_PREFIX}_{suffix}")
    return val.strip() if val else None


def _resolve_env_str(
    suffix: str,
    generic_key: str,
    *,
    demo_default: str,
    prod_default: str,
) -> str:
    return (
        _prefixed_env(suffix)
        or os.getenv(generic_key, "").strip()
        or (prod_default if IS_PRODUCTION else demo_default)
    )


def _resolve_env_int(
    suffix: str,
    generic_key: str,
    *,
    demo_default: int,
    prod_default: int,
) -> int:
    raw = _prefixed_env(suffix) or os.getenv(generic_key, "").strip()
    if raw:
        return int(raw)
    return prod_default if IS_PRODUCTION else demo_default


def _resolve_env_float(
    suffix: str,
    generic_key: str,
    *,
    demo_default: float,
    prod_default: float,
) -> float:
    raw = _prefixed_env(suffix) or os.getenv(generic_key, "").strip()
    if raw:
        return float(raw)
    return prod_default if IS_PRODUCTION else demo_default


def _credential_status_line() -> str:
    try:
        from credentials.env_credentials import resolve_credentials
        _id, _b64, source = resolve_credentials(ENV)
        return f"credentials from {source} (key …{_id[-8:]})"
    except Exception as exc:
        return f"credentials not loaded: {exc}"


# ─── Authentication (resolved for active KALSHI_ENV) ─────────────────────────

try:
    from credentials.env_credentials import resolve_credentials as _resolve_creds
    API_KEY_ID, PRIVATE_KEY_B64, CREDENTIAL_SOURCE = _resolve_creds(ENV)
except Exception:
    API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
    PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "")
    CREDENTIAL_SOURCE = "KALSHI_API_KEY_ID (unresolved)"

TOKEN_REFRESH_INTERVAL_SECONDS: int = 25 * 60   # refresh 5 min before 30-min expiry

# ─── Rate Limiting ────────────────────────────────────────────────────────────

RATE_LIMIT_READ_TOKENS_PER_SECOND: int   = 10
RATE_LIMIT_WRITE_TOKENS_PER_SECOND: int  = 5
RATE_LIMIT_MAX_BACKOFF_SECONDS: float    = 60.0
RATE_LIMIT_INITIAL_BACKOFF_SECONDS: float = 1.0

# ─── Fees ─────────────────────────────────────────────────────────────────────

FEE_PER_CONTRACT_CENTS: float = float(os.getenv("KALSHI_FEE_PER_CONTRACT_CENTS", "7.0"))
FEE_MAKER_REBATE_CENTS: float = 0.0

# ─── Kelly / Position Sizing ──────────────────────────────────────────────────

KELLY_DIVISOR: int = int(os.getenv("KALSHI_KELLY_DIVISOR", "4"))
MAX_POSITION_CENTS: int = _resolve_env_int(
    "MAX_POSITION_CENTS",
    "KALSHI_MAX_POSITION_CENTS",
    demo_default=10_000,
    prod_default=100,
)
MIN_EDGE_TO_VIG: float = float(os.getenv("KALSHI_MIN_EDGE_TO_VIG", "0.02"))

# ─── High-probability strategy ─────────────────────────────────────────────────

HP_MIN_YES_ASK: int       = int(os.getenv("KALSHI_HP_MIN_YES_ASK", "85"))
HP_MAX_YES_ASK: int       = int(os.getenv("KALSHI_HP_MAX_YES_ASK", "97"))
HP_MIN_ROI_PCT: float     = float(os.getenv("KALSHI_HP_MIN_ROI_PCT", "2.0"))
HP_USE_FEE_ADJUSTED_ROI: bool = os.getenv(
    "KALSHI_HP_USE_FEE_ADJUSTED_ROI", "true"
).strip().lower() in ("1", "true", "yes", "on")
HP_ASSUME_ROUND_TRIP_FEES: bool = os.getenv(
    "KALSHI_HP_ASSUME_ROUND_TRIP_FEES", ""
).strip().lower() in ("1", "true", "yes", "on")
HP_MAX_SPREAD_CENTS: int  = int(os.getenv("KALSHI_HP_MAX_SPREAD", "8"))
_hp_stake_default = min(5_000, MAX_POSITION_CENTS)
HP_STAKE_CENTS: int = min(
    int(os.getenv("KALSHI_HP_STAKE_CENTS", str(_hp_stake_default))),
    MAX_POSITION_CENTS,
)
HP_LIMIT_OFFSET: int      = int(os.getenv("KALSHI_HP_LIMIT_OFFSET", "0"))
HP_TAKE_PROFIT_OFFSET: int = int(os.getenv("KALSHI_HP_TAKE_PROFIT_OFFSET", "3"))
_hp_tp_pct_raw = os.getenv("KALSHI_HP_TAKE_PROFIT_PCT", "").strip()
HP_TAKE_PROFIT_PCT: float | None = (
    float(_hp_tp_pct_raw) if _hp_tp_pct_raw else None
)
HP_STOP_LOSS_PCT: float   = float(os.getenv("KALSHI_HP_STOP_LOSS", "0.12"))

# Pre-trade checks (enforced in main.py via risk.entry_gates)
MIN_ACCOUNT_BALANCE_CENTS: int = int(os.getenv("KALSHI_MIN_BALANCE_CENTS", "5000"))
MIN_MINUTES_TO_EXPIRY: float   = float(os.getenv("KALSHI_MIN_MINUTES_TO_EXPIRY", "10"))
BLOCK_ENTRIES_ON_LOW_BALANCE: bool = os.getenv(
    "KALSHI_BLOCK_LOW_BALANCE", "true"
).strip().lower() in ("1", "true", "yes", "on")

DEFAULT_STRATEGY: str = os.getenv("KALSHI_DEFAULT_STRATEGY", "high_prob")
ORDER_SUBMIT_MAX_RETRIES: int = int(os.getenv("KALSHI_ORDER_MAX_RETRIES", "3"))
PORTFOLIO_RISK_SYNC_SECONDS: float = float(
    os.getenv("KALSHI_PORTFOLIO_RISK_SYNC_SECONDS", "30")
)

# ─── Risk / Circuit Breaker ───────────────────────────────────────────────────

MAX_DRAWDOWN_PCT: float = _resolve_env_float(
    "MAX_DRAWDOWN_PCT",
    "KALSHI_MAX_DRAWDOWN_PCT",
    demo_default=0.10,
    prod_default=0.05,
)
_sector_conc_raw = float(os.getenv("KALSHI_MAX_SECTOR_CONCENTRATION", "1.0"))
# Fraction of portfolio in one sector (1.0 = 100%). Env values >1 treated as percent.
MAX_SECTOR_CONCENTRATION: float = (
    _sector_conc_raw / 100.0 if _sector_conc_raw > 1.0 else _sector_conc_raw
)
MAX_OPEN_POSITIONS: int = int(os.getenv("KALSHI_MAX_OPEN_POSITIONS", "20"))
MAX_CONCURRENT_POSITIONS: int = _resolve_env_int(
    "MAX_CONCURRENT_POSITIONS",
    "KALSHI_MAX_CONCURRENT_POSITIONS",
    demo_default=0,
    prod_default=1,
)
DAILY_LOSS_LIMIT_CENTS: int = _resolve_env_int(
    "DAILY_LOSS_LIMIT_CENTS",
    "KALSHI_DAILY_LOSS_LIMIT_CENTS",
    demo_default=50_000,
    prod_default=500,
)

LIVE_TRADING_ONLY: bool = os.getenv("KALSHI_LIVE_ONLY", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
LIVE_MAX_MINUTES_SINCE_UPDATE: float = float(
    os.getenv("KALSHI_LIVE_MAX_MINUTES_SINCE_UPDATE", "120")
)
LIVE_MAX_MINUTES_TO_CLOSE: float | None = (
    float(os.getenv("KALSHI_LIVE_MAX_MINUTES_TO_CLOSE"))
    if os.getenv("KALSHI_LIVE_MAX_MINUTES_TO_CLOSE", "").strip()
    else 360.0
)
LIVE_MAX_BOOK_STALE_MINUTES: float = float(
    os.getenv("KALSHI_LIVE_MAX_BOOK_STALE_MINUTES", "30")
)
LIVE_MAX_TRADE_STALE_MINUTES: float | None = (
    float(os.getenv("KALSHI_LIVE_MAX_TRADE_STALE_MINUTES"))
    if os.getenv("KALSHI_LIVE_MAX_TRADE_STALE_MINUTES", "").strip()
    else 120.0
)

PROFIT_TARGET_PCT: float        = 0.60
POSITION_STOP_LOSS_PCT: float   = 0.40

# ─── Execution ────────────────────────────────────────────────────────────────

ORDER_TIMEOUT_SECONDS: float  = 10.0
WS_PING_INTERVAL_SECONDS: int = 20

# ─── Settlement Watcher ───────────────────────────────────────────────────────

SETTLEMENT_POLL_INTERVAL_SECONDS: int = 5 * 60
SETTLEMENT_LOOKBACK_DAYS: int         = 3

# ─── Database ─────────────────────────────────────────────────────────────────

DB_PATH: str = _resolve_env_str(
    "DB_PATH",
    "KALSHI_DB_PATH",
    demo_default="kalshi_bot_demo.db",
    prod_default="kalshi_bot_prod.db",
)
POSTGRES_URL: str = os.getenv("KALSHI_POSTGRES_URL", "")
USE_POSTGRES: bool = bool(POSTGRES_URL)

# ─── Metrics ─────────────────────────────────────────────────────────────────

SHARPE_WINDOW_DAYS: int      = 30
RISK_FREE_RATE_ANNUAL: float = 0.05

# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_LEVEL: str = os.getenv("KALSHI_LOG_LEVEL", "INFO")
LOG_FILE: str = _resolve_env_str(
    "LOG_FILE",
    "KALSHI_LOG_FILE",
    demo_default="kalshi_bot_demo.jsonl",
    prod_default="kalshi_bot_prod.jsonl",
)


def _risk_profile_line() -> str:
    return (
        f"risk profile ({_ENV_PREFIX}): "
        f"max_position=${MAX_POSITION_CENTS / 100:.2f}  "
        f"daily_loss_limit=${DAILY_LOSS_LIMIT_CENTS / 100:.2f}  "
        f"max_drawdown={MAX_DRAWDOWN_PCT:.0%}  "
        f"concurrent_positions={MAX_CONCURRENT_POSITIONS or 'unlimited'}  "
        f"db={DB_PATH}  log={LOG_FILE}"
    )


def _print_startup_banner() -> None:
    if IS_PRODUCTION:
        print("[CONFIG] *** PRODUCTION MODE ACTIVE ***")
        print(f"[CONFIG] {BASE_URL}")
    else:
        print(f"[CONFIG] Running in DEMO mode -> {BASE_URL}")
    print(f"[CONFIG] {_credential_status_line()}")
    print(f"[CONFIG] {_risk_profile_line()}")


_print_startup_banner()
