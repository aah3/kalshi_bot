# Kalshi Prediction Market Trading Bot

Production-grade automated trading system for Kalshi binary prediction markets.

## Requirements

- Python 3.11+
- A Kalshi account with API access enabled
- API key + RSA private key (from kalshi.com/account/api-keys)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your API key and private key
```

To encode your private key:
```bash
base64 -w 0 your_private_key.pem
# Paste the output into KALSHI_PRIVATE_KEY_B64 in .env
```

Then load the variables:
```bash
export $(grep -v '^#' .env | xargs)
```

### 3. Run the tests

```bash
python tests/test_green_up_formulas.py   # 22/22
python tests/test_circuit_breaker.py     # 16/16
```

---

## Before your first paper trading session

### Step 1 — Find markets to trade

```bash
# List all categories
python tools/screen.py --categories

# Screen a category for the best strategy fits
python tools/screen.py --category Politics

# Full detail on a specific market
python tools/screen.py --ticker PRES-2024-DEM

# Screen an entire event (also runs arb detection)
python tools/screen.py --event PRES-2024
```

### Step 2 — Set tickers and start the bot

```bash
export KALSHI_TICKERS="PRES-2024-DEM,INXD-23DEC29-B4700"
python main.py
```

The bot defaults to `KALSHI_ENV=demo` (paper money). It will not touch
real money unless you explicitly set `KALSHI_ENV=production`.

### Step 3 — Place manual orders

```bash
# Preview an order (no submission)
python tools/trade.py preview --ticker PRES-2024-DEM --side yes --count 10 --price 32

# Place a limit order
python tools/trade.py buy --ticker PRES-2024-DEM --side yes --count 10 --price 32

# Watch live P&L
python tools/trade.py monitor --interval 15
```

### Step 4 — Monitor the dashboard

```bash
python tools/dashboard.py --interval 15 --calibration
```

### Step 5 — Review session results

```bash
# End-of-session report
python tools/session_report.py

# Full blotter history
python tools/blotter.py trades --days 7

# P&L by strategy and category
python tools/blotter_report.py performance --days 7

# Kelly calibration check
python tools/blotter_report.py calibration --days 30
```

---

## Replay recorded data offline

```bash
# Record 30 minutes of live book data
python tools/replay.py record \
    --tickers PRES-2024-DEM \
    --output data/session.jsonl \
    --duration 1800

# Replay at 100x speed through Kelly strategy
python tools/replay.py replay \
    --input data/session.jsonl \
    --strategy kelly \
    --model-prob PRES-2024-DEM:0.62 \
    --speed 100

# Replay green-up strategy
python tools/replay.py replay \
    --input data/session.jsonl \
    --strategy green_up \
    --entry-max 30 \
    --hedge-trigger 70

# Inspect a recording file
python tools/replay.py info --input data/session.jsonl
```

---

## Production checklist (before setting KALSHI_ENV=production)

- [ ] Two consecutive weeks of paper trading with no unhandled exceptions
- [ ] All 38 tests passing: `python tests/test_*.py`
- [ ] Kelly calibration ratio between 0.85–1.10 for all strategies (30+ settled trades)
- [ ] At least one clean SIGINT shutdown observed (Ctrl-C → all orders cancelled)
- [ ] Circuit breaker tested: temporarily set `MAX_DRAWDOWN_PCT=0.01` in config.py,
      confirm the kill switch fires and halts the engine in demo
- [ ] `config.py` reviewed: `KELLY_DIVISOR`, `MAX_POSITION_CENTS`,
      `DAILY_LOSS_LIMIT_CENTS` set to values you are comfortable losing in week 1
- [ ] `KALSHI_ENV=production` is not set in any shell profile or CI variable

---

## Project structure

```
kalshi_bot/
├── config.py                        All tunable parameters
├── main.py                          Entry point, full bot lifecycle
├── .env.example                     Environment variable template
├── requirements.txt
│
├── credentials/
│   └── credential_manager.py        RSA-PSS signing, env-var credential loading
│
├── ingestion/
│   └── market_ingestor.py           WebSocket stream, local order book state
│
├── strategy/
│   ├── base_strategy.py             Pluggable strategy interface
│   ├── kelly_strategy.py            Fractional Kelly + edge-to-vig gating
│   ├── arbitrage_strategy.py        Complementary, exhaustive-set, dominance arb
│   └── green_up_strategy.py         Back High Lay Low in-play hedging
│
├── risk/
│   ├── circuit_breaker.py           Kill switch, drawdown, concentration limits
│   ├── alert_manager.py             Profit target, stop, expiry, fill timeout alerts
│   └── kelly_calibrator.py          Brier score, reliability diagram, divisor recs
│
├── execution/
│   ├── execution_manager.py         Order placement, 25-min token refresh
│   └── rate_limiter.py              Token-bucket + exponential backoff on 429
│
├── logging_/
│   └── structured_logger.py         JSON logs, microsecond timestamps
│
├── metrics/
│   ├── blotter.py                   Trade blotter (legs + parent trades, SQLite/PG)
│   ├── settlement.py                Auto-detect market resolutions, reconcile P&L
│   ├── performance.py               Win rate, profit factor, Sortino, streaks, etc.
│   ├── calculator.py                Sharpe, max drawdown, fill rate, edge-to-vig
│   └── metrics_store.py             Legacy signal/equity store (for Sharpe calc)
│
├── discovery/
│   ├── market_client.py             REST client: categories, markets, order books
│   └── screener.py                  Score and rank markets by strategy fit
│
├── trading/
│   ├── order_entry.py               Manual market/limit order placement + validation
│   └── portfolio_monitor.py         Live mark-to-market P&L, position tracking
│
├── tools/
│   ├── screen.py                    CLI: browse and score available markets
│   ├── trade.py                     CLI: place orders, monitor positions
│   ├── blotter.py                   CLI: query trade history, export CSV
│   ├── blotter_report.py            CLI: performance analytics, calibration
│   ├── session_report.py            CLI: end-of-day session summary
│   ├── replay.py                    Record live WS data; replay offline
│   └── dashboard.py                 Live terminal dashboard (all panels)
│
└── tests/
    ├── test_green_up_formulas.py    22 tests — hedge math vs spec examples
    └── test_circuit_breaker.py      16 tests — all 5 risk conditions
```

---

## Key configuration parameters (config.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `KELLY_DIVISOR` | 4 | Quarter-Kelly position sizing |
| `MAX_POSITION_CENTS` | 10,000 | $100 max per market |
| `MIN_EDGE_TO_VIG` | 0.02 | 2% minimum edge before trading |
| `MAX_DRAWDOWN_PCT` | 0.10 | 10% drawdown triggers kill switch |
| `DAILY_LOSS_LIMIT_CENTS` | 50,000 | $500 daily loss limit |
| `MAX_OPEN_POSITIONS` | 20 | Maximum simultaneous positions |
| `FEE_PER_CONTRACT_CENTS` | 7.0 | Kalshi fee per contract |
| `SETTLEMENT_POLL_INTERVAL_SECONDS` | 300 | Check resolutions every 5 min |
| `PROFIT_TARGET_PCT` | 0.60 | Alert at +60% unrealised P&L |
| `POSITION_STOP_LOSS_PCT` | 0.40 | Alert at -40% unrealised P&L |
