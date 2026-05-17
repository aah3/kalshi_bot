# Kalshi Prediction Market Trading Bot

Production-grade automated trading system for [Kalshi](https://kalshi.com) binary prediction markets. Supports multiple pluggable strategies, automatic market discovery, a live terminal monitor, manual order tools, offline replay, and a persistent trade blotter (queryable by date, category, ticker, strategy, and resolution) with performance analytics and settlement reconciliation.

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
# Edit .env: KALSHI_DEMO_API_KEY_ID + KALSHI_DEMO_PRIVATE_KEY_B64 (demo)
# For production: KALSHI_PROD_* and KALSHI_ENV=production
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

### Green-up strategy

Back cheap **YES** (underdog), then hedge with **NO** when price runs. Hedge sizing uses full-green, stake-back, or partial formulas (see `strategy/green_up_strategy.py`).

The bot watches **all N discovered tickers** in parallel. Each order is priced from that market’s live bid/ask on every tick.

**CLI parameters** (also available via env — see below):

| Flag | Default | Description |
|------|---------|-------------|
| `--entry-max` | 25¢ | Enter only when best YES ask ≤ this |
| `--hedge-trigger` | 68¢ | Hedge when YES **bid** reaches this |
| `--hedge-mode` | `full_green` | `full_green` \| `stake_back` \| `partial` |
| `--stop-loss` | 0.40 | Stop if YES bid falls 40% below entry |
| `--gu-entry-mode` | `passive` | How to price **buy YES** entries (see order pricing) |
| `--gu-exit-mode` | `passive` | How to price **buy NO** hedge/stop legs |
| `--max-concurrent-positions` | `0` (unlimited) | Cap simultaneous open/pending positions |

**Order pricing** (`--gu-entry-mode` / `--gu-exit-mode`, same set as high-prob):

| Mode | Buy | Sell |
|------|-----|------|
| `passive` (default) | Limit at **bid** (resting) | Limit at **ask** (resting) |
| `cross_spread` | Limit at **ask** (IOC, cross) | Limit at **bid** (IOC, cross) |
| `market` | IOC market | IOC market |
| `limit_at_bid` / `limit_at_ask` | Explicit bid/ask limits | Same aliases as above |

```bash
# Preview top 5 Sports markets (screener-ranked, min volume enforced)
python main.py --discover --discover-category Sports --strategy green_up \
  --discover-top 5 --discover-only

# Autonomous demo: 5 markets, passive entries, max 5 open positions
python main.py --discover --discover-category Sports --strategy green_up \
  --discover-top 5 --max-concurrent-positions 5 \
  --entry-max 25 --hedge-trigger 68 --hedge-mode full_green \
  --gu-entry-mode passive --gu-exit-mode cross_spread \
  --monitor-interval 5
```

### High-probability strategy

Buys **YES** when the market already prices a high chance of winning (default YES ask **85–97¢**), with **fee-adjusted ROI** gating so entries still clear costs after `FEE_PER_CONTRACT_CENTS`.

**Entry / exit modes** (`--hp-entry-mode`, `--hp-exit-mode`):

| Mode | Buy YES | Sell YES |
|------|---------|----------|
| `passive` (default) | Limit at bid (GTC) | Limit at ask (GTC) |
| `cross_spread` | Limit at ask (IOC) | Limit at bid (IOC) |
| `market` | IOC market | IOC market |
| `limit_at_mid` / `limit_offset` | Mid or bid+offset | Mid or ask−offset |

Stop-loss exits use `cross_spread` when exit mode is `passive`, so stops actually cross the book.

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
| `green_up` | `green_up` | YES ask ≤35¢, min vol 500, activity 48h, rank by **screener score** |
| `kelly` | `kelly` | Spread ≤10¢, min vol 100 |
| `arb` | `arb` | Top 25 by volume, full scan |

**Screener volume floor:** every strategy scorer rejects markets below **200** contracts 24h volume (`SCREENER_MIN_VOLUME_24H` in `discovery/screener.py`). Discovery presets may set a higher `KALSHI_DISCOVER_MIN_VOLUME`.

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

# Green-up: top 5 Sports markets, screener-ranked, trade all in parallel
python main.py --discover --discover-category Sports --strategy green_up \
  --discover-top 5 --max-concurrent-positions 5 \
  --entry-max 25 --hedge-trigger 68 --hedge-mode full_green \
  --gu-entry-mode passive --monitor-interval 5
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
| `KALSHI_DISCOVER_RANK_BY` | `volume`, `fee_adjusted_roi`, or `screener` |
| `KALSHI_MAX_CONCURRENT_POSITIONS` | Cap open/pending positions (`0` = unlimited) |

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

### 4. Live monitor table

While `main.py` runs, a full-screen table shows cash, P&L, per-ticker bid/ask, strategy state (watching → entered → hedged), and alerts. Refresh interval defaults to `KALSHI_MONITOR_INTERVAL` (15s); override with `--monitor-interval` (use `0` to disable).

```bash
python main.py --strategy green_up --tickers TICKER-A \
  --entry-max 10 --hedge-trigger 13 --monitor-interval 5

python main.py --strategy high_prob --tickers TICKER-A --monitor-interval 15
```

The monitor can fall back to REST order books when the WebSocket book is empty; the strategy evaluates on **WebSocket ticks** (Kalshi `orderbook_delta` channel, including FP dollar snapshots/deltas).

### 5. Dashboard and session reports

```bash
python tools/dashboard.py --interval 15 --calibration
python tools/session_report.py
python tools/session_report.py --date 2026-05-17 --json reports/session.json
python tools/blotter_report.py performance --days 7
python tools/blotter_report.py calibration --days 30
```

---

## Trade history and blotter

Every trade opened through **`main.py`** is recorded in a SQLite database (default `kalshi_bot.db`, path via `KALSHI_DB_PATH`). Two tables:

| Table | Contents |
|-------|----------|
| `parent_trades` | One row per logical trade (e.g. full green-up cycle) — net P&L, category, strategy, hold time, resolution |
| `trades` | One row per **leg** (entry, hedge, stop) — side, prices, fees, `trade_type` |

The same database also holds **metrics** tables (`signals`, `metrics_fills`, `equity_snapshots`) for fill rate, Sharpe, and drawdown. Structured JSON logs go to `kalshi_bot.jsonl` (`KALSHI_LOG_FILE`).

**Not recorded automatically:** orders placed only via `tools/trade.py` (manual CLI) or directly on the Kalshi website — those live on the exchange, not in the bot blotter unless you add them manually.

### Query CLI (`tools/blotter.py`)

| Command | Purpose |
|---------|---------|
| `trades` | List parent trades (filterable) |
| `search` | Same filters as `trades`; add `--legs` for fill-level rows |
| `legs` | Individual fills (entry / hedge / stop) |
| `detail` | Full parent trade + all legs (`--trade-id T-NNNN`) |
| `open` | Open positions still in the blotter |
| `pnl-by-strategy` / `pnl-by-category` | Aggregated P&L |
| `best` / `worst` | Top N trades by net P&L |
| `note` | Annotate a trade or leg |
| `settle` | Manually mark settled (`--resolution yes\|no\|void`) |

**Filters** (combinable on `trades`, `search`, and `legs` where applicable):

| Filter | Flag | Example |
|--------|------|---------|
| Status | `--status` | `open`, `closed`, `settled` |
| Category | `--category` | `Sports`, `Politics` |
| Strategy | `--strategy` | `green_up` (substring match) |
| Ticker | `--ticker` | exact market ticker |
| Trade ID | `--trade-id` | `T-0042` |
| Resolution | `--resolution` | `yes`, `no`, `void` |
| Date range | `--days N` or `--from` / `--to` | last 7 days; `2026-05-01` … `2026-05-17` |
| Leg type | `--trade-type` | `entry`, `hedge`, `stop_loss` (with `search --legs` or `legs`) |
| Export | `--csv FILE` | write results to CSV |
| Limit | `--limit` | default 200 trades / 500 legs |

```bash
# Parent trades
python tools/blotter.py trades --days 7
python tools/blotter.py trades --category Sports --strategy green_up --status closed
python tools/blotter.py search --resolution yes --status settled --days 30
python tools/blotter.py search --trade-id T-0042
python tools/blotter.py detail --trade-id T-0042

# Leg-level (entry / hedge / stop)
python tools/blotter.py search --legs --ticker TICKER-A --days 14
python tools/blotter.py search --legs --trade-type hedge --days 30
python tools/blotter.py legs --trade-id T-0042

# Aggregates and export
python tools/blotter.py pnl-by-strategy --days 30
python tools/blotter.py pnl-by-category --days 30 --csv category_pnl.csv
python tools/blotter.py trades --status closed --days 30 --csv trades.csv
```

On shutdown, `main.py` prints a short summary of closed/settled trades from the last 24 hours.

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
│   └── market_ingestor.py        WebSocket order books (FP snapshots/deltas) + fills
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
| `POSITION_STOP_LOSS_PCT` | 0.40 | Alert when unrealised loss ≥ 40% of cost |
| `KALSHI_DB_PATH` | `kalshi_bot.db` | Blotter + metrics SQLite file |
| `KALSHI_MONITOR_INTERVAL` | 15 | Live table refresh (seconds); `0` = off |

### Green-up environment variables

```env
KALSHI_STRATEGY=green_up
KALSHI_GREEN_UP_ENTRY_MAX=25
KALSHI_GREEN_UP_HEDGE_TRIGGER=68
KALSHI_GREEN_UP_HEDGE_MODE=full_green
KALSHI_GREEN_UP_STOP_LOSS=0.40
KALSHI_GREEN_UP_ENTRY_MODE=passive
KALSHI_GREEN_UP_EXIT_MODE=passive
KALSHI_MAX_CONCURRENT_POSITIONS=5
```

CLI flags (`--entry-max`, `--hedge-trigger`, `--hedge-mode`, `--stop-loss`, `--gu-entry-mode`, `--gu-exit-mode`, `--max-concurrent-positions`) override these at runtime.

### High-probability environment variables

```env
KALSHI_STRATEGY=high_prob
KALSHI_HP_MIN_YES_ASK=85
KALSHI_HP_MAX_YES_ASK=97
KALSHI_HP_MIN_ROI_PCT=2.0
KALSHI_HP_USE_FEE_ADJUSTED_ROI=true
KALSHI_HP_ENTRY_MODE=passive
KALSHI_HP_EXIT_MODE=passive
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
