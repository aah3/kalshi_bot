# Kalshi Prediction Market Trading Bot

Production-grade automated trading system for [Kalshi](https://kalshi.com) binary prediction markets. Supports multiple pluggable strategies, automatic market discovery, manual order tools, offline replay, and a full trade blotter with settlement reconciliation.

## Requirements

- Python 3.11+
- A Kalshi account with API access enabled
- API key + RSA private key ([kalshi.com/account/api-keys](https://kalshi.com/account/api-keys))

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_B64
```

Encode your private key:

```bash
# Linux / macOS
base64 -w 0 your_private_key.pem

# Windows PowerShell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("your_private_key.pem"))
```

The bot loads `.env` on startup. Shell exports override `.env` values.

### 3. Run tests

```bash
python -m pytest tests/ -v
```

---

## Strategies

| Strategy | CLI / env | Profile |
|----------|-----------|---------|
| **Kelly** | `--strategy kelly` | Fractional Kelly when model P(YES) beats the market by `MIN_EDGE_TO_VIG` |
| **Green Up** | `--strategy green_up` | Back cheap YES, hedge when price runs (full green / stake back / partial) |
| **Arbitrage** | `--strategy arb` | Complementary, exhaustive-set, and dominance arbs |
| **High probability** | `--strategy high_prob` | Buy high-implied-P(YES) contracts; optional resting take-profit / stop |

### High-probability strategy

Buys **YES** when the market already prices a high chance of winning (default YES ask **85–97¢**), with **fee-adjusted ROI** gating so entries still clear costs after `FEE_PER_CONTRACT_CENTS`.

**Entry modes** (`--hp-entry-mode` / `KALSHI_HP_ENTRY_MODE`):

| Mode | Behaviour |
|------|-----------|
| `market` | Market order (IOC) |
| `limit_at_ask` | Aggressive limit at best ask (default) |
| `limit_at_bid` | Passive limit at best bid (resting) |
| `limit_at_mid` | Limit at mid-price |
| `limit_offset` | Limit at `bid + KALSHI_HP_LIMIT_OFFSET` |

**Post-fill** (`--hp-post-fill` / `KALSHI_HP_POST_FILL`):

| Mode | Behaviour |
|------|-----------|
| `hold` | Hold to settlement |
| `resting_take_profit` | Resting sell YES at entry + offset |
| `resting_stop` | IOC sell if bid breaches stop |
| `tp_and_stop` | Resting TP plus stop on breach |

---

## Market discovery

Automatically select tickers at startup instead of hard-coding `KALSHI_TICKERS`.

### Strategy presets

When you use `--discover` (or `KALSHI_DISCOVER=true`), the bot applies a **discovery preset** matching `--strategy` unless you override filters on the CLI:

| Preset | Used for | Default filters |
|--------|----------|-----------------|
| `high_prob` | `high_prob` | YES ask 85–97¢, spread ≤8¢, min vol 200, rank by **fee-adjusted ROI** |
| `green_up` | `green_up` | YES ask ≤35¢, min vol 500, activity 48h |
| `kelly` | `kelly` | Spread ≤10¢, min vol 100 |
| `arb` | `arb` | Top 25 by volume, full scan |

Force a preset: `--discover-preset high_prob`  
Disable presets: `--discover-preset none`

### Examples

```bash
# Preview tickers only (prints KALSHI_TICKERS=... line)
python main.py --discover --discover-category Politics --strategy high_prob --discover-only

# Discover and trade with high-prob defaults
python main.py --discover --discover-category Sports --strategy high_prob

# Override preset floor but keep ranking
python main.py --discover --discover-category Politics --strategy high_prob \
  --discover-min-yes-ask 88 --discover-rank-by fee_adjusted_roi

# Green-up discovery (underdog window)
python main.py --discover --discover-category Sports --strategy green_up --discover-only
```

### Discovery environment variables

| Variable | Description |
|----------|-------------|
| `KALSHI_DISCOVER` | `true` to enable discovery via env |
| `KALSHI_DISCOVER_CATEGORY` | e.g. `Politics`, `Sports` |
| `KALSHI_DISCOVER_TOP` | Max tickers (default 10) |
| `KALSHI_DISCOVER_MIN_YES_ASK` | Minimum YES ask (cents) |
| `KALSHI_DISCOVER_MAX_YES_ASK` | Maximum YES ask (cents) |
| `KALSHI_DISCOVER_MAX_SPREAD` | Max spread (cents) |
| `KALSHI_DISCOVER_MIN_VOLUME` | Min 24h volume |
| `KALSHI_DISCOVER_ACTIVITY_HOURS` | Only recently updated markets |

---

## Quick start (paper trading)

### 1. Screen markets

```bash
python tools/screen.py categories
python tools/screen.py browse --category Politics
python tools/screen.py screen --category Politics
python tools/screen.py browse --ticker SOME-TICKER
```

The screener scores each market for Kelly, Green Up, **high_prob**, and arbitrage fit.

### 2. Run the bot (demo)

```bash
# Manual tickers
export KALSHI_TICKERS="TICKER-A,TICKER-B"
python main.py --strategy kelly --model-prob TICKER-A:0.62

# Auto-discovery + high-probability strategy
python main.py --discover --discover-category Politics --strategy high_prob \
  --hp-entry-mode limit_at_bid --hp-post-fill resting_take_profit
```

Defaults to **demo** (`KALSHI_ENV=demo`). Real money only when `KALSHI_ENV=production`.

### 3. Manual orders

```bash
python tools/trade.py preview --ticker TICKER-A --side yes --count 10 --price 32
python tools/trade.py buy --ticker TICKER-A --side yes --count 10 --price 32
python tools/trade.py monitor --interval 15
```

### 4. Dashboard and reports

```bash
python tools/dashboard.py --interval 15 --calibration
python tools/session_report.py
python tools/blotter.py trades --days 7
python tools/blotter_report.py performance --days 7
python tools/blotter_report.py calibration --days 30
```

### 5. Live monitor table

```bash
python main.py --strategy high_prob --tickers TICKER-A --monitor-interval 15
```

---

## Offline replay

```bash
# Record live book data
python tools/replay.py record --tickers TICKER-A --output data/session.jsonl --duration 1800

# Replay strategies
python tools/replay.py replay --input data/session.jsonl --strategy kelly \
  --model-prob TICKER-A:0.62 --speed 100

python tools/replay.py replay --input data/session.jsonl --strategy green_up \
  --entry-max 30 --hedge-trigger 70

python tools/replay.py replay --input data/session.jsonl --strategy high_prob
```

---

## Production checklist

- [ ] Two+ weeks demo trading without unhandled exceptions
- [ ] `python -m pytest tests/ -v` all green
- [ ] Kelly calibration ratio 0.85–1.10 (30+ settled trades per strategy)
- [ ] Clean SIGINT shutdown (orders cancelled)
- [ ] Circuit breaker tested in demo (`MAX_DRAWDOWN_PCT=0.01`)
- [ ] `config.py` reviewed: `MAX_POSITION_CENTS`, `DAILY_LOSS_LIMIT_CENTS`, fees
- [ ] `KALSHI_ENV=production` not set in shell profiles by accident

---

## Project structure

```
kalshi_bot/
├── config.py                     Tunable parameters (fees, Kelly, HP, risk)
├── main.py                       Bot entry point + discovery CLI
├── strategy/
│   ├── base_strategy.py          Signal interface
│   ├── factory.py                build_strategy() by name
│   ├── kelly_strategy.py         Fractional Kelly + edge-to-vig
│   ├── green_up_strategy.py      Back high / lay low hedging
│   ├── high_prob_strategy.py     High P(YES), fee-aware ROI, exit modes
│   └── arbitrage_strategy.py     Multi-leg arb
├── discovery/
│   ├── market_client.py          REST: markets, books, categories
│   ├── screener.py               Score markets per strategy
│   ├── ticker_selector.py        Filter, rank, select tickers
│   ├── discovery_presets.py      Strategy-aligned discovery defaults
│   └── market_math.py            Gross / fee-adjusted ROI helpers
├── execution/
│   ├── execution_manager.py      Orders (limit/market, buy/sell, TIF)
│   └── rate_limiter.py           Token bucket + 429 backoff
├── ingestion/
│   └── market_ingestor.py        WebSocket order books + fills
├── risk/
│   ├── circuit_breaker.py        Kill switch, limits
│   ├── alert_manager.py          P&L / expiry / fill-timeout alerts
│   └── kelly_calibrator.py       Brier score, divisor recommendations
├── metrics/                      Blotter, settlement, performance, Sharpe
├── trading/                      Manual order entry, portfolio monitor
├── monitoring/                   Live session terminal table
└── tools/                        screen, trade, replay, blotter, dashboard
```

---

## Key configuration (`config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `KELLY_DIVISOR` | 4 | Quarter-Kelly sizing |
| `MAX_POSITION_CENTS` | 10,000 | $100 cap per market |
| `MIN_EDGE_TO_VIG` | 0.02 | Minimum edge vs half-spread |
| `FEE_PER_CONTRACT_CENTS` | 7.0 | Per-contract fee (set from your tier) |
| `HP_MIN_YES_ASK` / `HP_MAX_YES_ASK` | 85 / 97 | High-prob entry window |
| `HP_MIN_ROI_PCT` | 2.0 | Min ROI % (fee-adjusted when enabled) |
| `HP_USE_FEE_ADJUSTED_ROI` | true | Gate entries on net ROI after fees |
| `HP_ASSUME_ROUND_TRIP_FEES` | false | Also count exit fee in ROI gate (or infer from post-fill) |
| `MAX_DRAWDOWN_PCT` | 0.10 | Kill switch drawdown |
| `DAILY_LOSS_LIMIT_CENTS` | 50,000 | Daily stop ($500) |

### High-probability environment variables

```env
KALSHI_STRATEGY=high_prob
KALSHI_HP_MIN_YES_ASK=85
KALSHI_HP_MAX_YES_ASK=97
KALSHI_HP_MIN_ROI_PCT=2.0
KALSHI_HP_USE_FEE_ADJUSTED_ROI=true
KALSHI_HP_ENTRY_MODE=limit_at_ask
KALSHI_HP_POST_FILL=resting_take_profit
KALSHI_HP_STAKE_CENTS=5000

KALSHI_DISCOVER=true
KALSHI_DISCOVER_CATEGORY=Politics
```

---

## Fee-adjusted ROI

For a YES buy at ask `A` cents with entry fee `F`:

- **Gross ROI** if YES wins: `(100 - A) / A × 100`
- **Fee-adjusted ROI** (hold to settlement): `(100 - A - F) / (A + F) × 100`

Example at 90¢ ask, $0.07 fee: gross ≈ 11.1%, net ≈ **3.1%**.

Discovery and `high_prob` use this net figure when `HP_USE_FEE_ADJUSTED_ROI=true`. Set `KALSHI_HP_ASSUME_ROUND_TRIP_FEES=true` (or use a non-`hold` post-fill mode) to require entries to clear two fees.
