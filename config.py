"""
config.py — single source of truth for all tunable parameters.

Credentials (both environments can live in .env at once):
    KALSHI_ENV=demo | production
    KALSHI_DEMO_API_KEY_ID / KALSHI_DEMO_PRIVATE_KEY_B64
    KALSHI_PROD_API_KEY_ID / KALSHI_PROD_PRIVATE_KEY_B64
    (optional legacy fallback: KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_B64)

On Windows, you can skip `export` and put the same assignments in a `.env` file
next to this module; values are loaded into `os.environ` on import (real env
vars still win if already set).

Everything else defaults to safe demo values.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# ─── Environment ─────────────────────────────────────────────────────────────

ENV = os.getenv("KALSHI_ENV", "demo").lower()   # "demo" | "production"

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://api.kalshi.co/trade-api/v2"
DEMO_WS_URL   = "wss://demo-api.kalshi.co/trade-api/ws/v2"
PROD_WS_URL   = "wss://api.kalshi.co/trade-api/ws/v2"

BASE_URL = DEMO_BASE_URL if ENV != "production" else PROD_BASE_URL
WS_URL   = DEMO_WS_URL   if ENV != "production" else PROD_WS_URL

def _credential_status_line() -> str:
    try:
        from credentials.env_credentials import resolve_credentials
        _id, _b64, source = resolve_credentials(ENV)
        return f"credentials from {source} (key …{_id[-8:]})"
    except Exception as exc:
        return f"credentials not loaded: {exc}"


if ENV == "production":
    print("[CONFIG] *** PRODUCTION MODE ACTIVE ***")
    print(f"[CONFIG] {BASE_URL}")
    print(f"[CONFIG] {_credential_status_line()}")
else:
    print(f"[CONFIG] Running in DEMO mode → {BASE_URL}")
    print(f"[CONFIG] {_credential_status_line()}")

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
# Kalshi charges a maker/taker fee per contract.
# Set this to the current rate from your account tier.
# Default: 7 cents per contract per side (0.07 per contract × 100 = 7c).
# Fee is charged on the TAKER side only for limit orders that cross the spread,
# and on both sides for market orders.
# See: https://kalshi.com/docs/fees

FEE_PER_CONTRACT_CENTS: float = 7.0          # cents per filled contract
FEE_MAKER_REBATE_CENTS: float = 0.0          # maker rebate (0 unless on a pro tier)
# Net fee for a round-trip (entry + exit or settlement):
#   entry fill fee  + settlement (no fee on settlement — Kalshi credits gross)
# So net fee per trade = FEE_PER_CONTRACT_CENTS × contracts (entry only)

# ─── Kelly / Position Sizing ──────────────────────────────────────────────────

KELLY_DIVISOR: int     = 4          # 4 = quarter-Kelly
MAX_POSITION_CENTS: int = 10_000    # hard cap per market ($100)
MIN_EDGE_TO_VIG: float  = 0.02      # minimum edge over vig before trading (2%)

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
HP_STAKE_CENTS: int       = int(os.getenv("KALSHI_HP_STAKE_CENTS", "5000"))
HP_LIMIT_OFFSET: int      = int(os.getenv("KALSHI_HP_LIMIT_OFFSET", "0"))
HP_TAKE_PROFIT_OFFSET: int = int(os.getenv("KALSHI_HP_TAKE_PROFIT_OFFSET", "3"))
HP_STOP_LOSS_PCT: float   = float(os.getenv("KALSHI_HP_STOP_LOSS", "0.12"))

# Pre-trade checks
MIN_ACCOUNT_BALANCE_CENTS: int = 5_000   # refuse new trades if balance drops below $50
MIN_MINUTES_TO_EXPIRY: float   = 10.0    # refuse entries on markets closing in < 10 min

# ─── Risk / Circuit Breaker ───────────────────────────────────────────────────

MAX_DRAWDOWN_PCT: float         = 0.10    # 10% peak-to-trough kills the bot
MAX_SECTOR_CONCENTRATION: float = 0.30   # 30% of portfolio in one category
MAX_OPEN_POSITIONS: int         = 20     # absolute open position count
DAILY_LOSS_LIMIT_CENTS: int     = 50_000 # $500 daily stop-loss

# Per-position risk thresholds (used by alert_manager)
PROFIT_TARGET_PCT: float        = 0.60   # alert when unrealised P&L >= +60%
POSITION_STOP_LOSS_PCT: float   = 0.40   # alert when unrealised P&L <= -40%

# ─── Execution ────────────────────────────────────────────────────────────────

ORDER_TIMEOUT_SECONDS: float  = 10.0
WS_PING_INTERVAL_SECONDS: int = 20

# ─── Settlement Watcher ───────────────────────────────────────────────────────

SETTLEMENT_POLL_INTERVAL_SECONDS: int = 5 * 60   # check resolved markets every 5 min
SETTLEMENT_LOOKBACK_DAYS: int         = 3         # how far back to scan for resolutions

# ─── Database ─────────────────────────────────────────────────────────────────

# SQLite path (default). Override with a postgres:// URL for Postgres.
DB_PATH: str = os.getenv("KALSHI_DB_PATH", "kalshi_bot.db")

# If set to a postgres:// connection string, the blotter switches to Postgres.
# Requires psycopg2-binary to be installed.
# Example: "postgresql://user:password@localhost:5432/kalshi"
POSTGRES_URL: str = os.getenv("KALSHI_POSTGRES_URL", "")

# Use Postgres if a URL is provided, otherwise SQLite
USE_POSTGRES: bool = bool(POSTGRES_URL)

# ─── Metrics ─────────────────────────────────────────────────────────────────

SHARPE_WINDOW_DAYS: int      = 30
RISK_FREE_RATE_ANNUAL: float = 0.05    # 5% annualised

# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_LEVEL: str = os.getenv("KALSHI_LOG_LEVEL", "INFO")
LOG_FILE: str  = os.getenv("KALSHI_LOG_FILE", "kalshi_bot.jsonl")
