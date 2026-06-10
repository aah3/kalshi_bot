# Development roadmap — demo certification → production micro-pilot

Goal: a **reliable demo environment for all five strategies** (`kelly`, `green_up`, `high_prob`, `mean_reversion`, `arb`), then production testing at **$1 max per market** (`KALSHI_PROD_MAX_POSITION_CENTS=100`). New features ship only on top of a working, certified baseline.

Switch environments by changing **one variable**: `KALSHI_ENV=demo` or `KALSHI_ENV=production`. Risk limits, database, and log files follow automatically (see [Environment profiles](#environment-profiles)).

---

## Environment profiles

### How to switch

In `.env` (copy from `.env.example`):

```env
# Demo (paper) — default
KALSHI_ENV=demo

# Production (real money) — only when checklist is complete
# KALSHI_ENV=production
```

Restart the bot after changing `KALSHI_ENV`. On startup, `config.py` prints:

- API base URL (demo vs prod)
- Which API key pair is loaded
- Active **risk profile** (max position, daily loss, DB path, log file)

### Per-environment variables (recommended)

Set **both** profiles in `.env` once; toggle with `KALSHI_ENV` only:

| Variable | Demo (suggested) | Prod (suggested) |
|----------|------------------|------------------|
| `KALSHI_DEMO_MAX_POSITION_CENTS` | `10000` ($100) | — |
| `KALSHI_PROD_MAX_POSITION_CENTS` | — | `100` ($1) |
| `KALSHI_DEMO_DAILY_LOSS_LIMIT_CENTS` | `50000` ($500) | — |
| `KALSHI_PROD_DAILY_LOSS_LIMIT_CENTS` | — | `500` ($5) |
| `KALSHI_DEMO_MAX_DRAWDOWN_PCT` | `0.10` | — |
| `KALSHI_PROD_MAX_DRAWDOWN_PCT` | — | `0.05` |
| `KALSHI_DEMO_MAX_CONCURRENT_POSITIONS` | `5` | — |
| `KALSHI_PROD_MAX_CONCURRENT_POSITIONS` | — | `1` |
| `KALSHI_DEMO_DB_PATH` | `kalshi_bot_demo.db` | — |
| `KALSHI_PROD_DB_PATH` | — | `kalshi_bot_prod.db` |
| `KALSHI_DEMO_LOG_FILE` | `kalshi_bot_demo.jsonl` | — |
| `KALSHI_PROD_LOG_FILE` | — | `kalshi_bot_prod.jsonl` |

Generic keys (`KALSHI_MAX_POSITION_CENTS`, `KALSHI_DB_PATH`, …) apply when no `KALSHI_DEMO_*` / `KALSHI_PROD_*` value is set for that field.

### Verify before each run

```bash
python -c "import config; print(config.ENV, config.MAX_POSITION_CENTS, config.DB_PATH)"
```

Expect `demo` and `10000` during certification; expect `production` and `100` only for the micro-pilot.

---

## Definition of done (demo)

Before any production run, every strategy must pass:

| # | Criterion | Verification |
|---|-----------|--------------|
| 1 | Discover → WS tick → signal → order → fill → blotter | Session log + `blotter.py detail` |
| 2 | Exchange matches bot | `tools/trade.py portfolio` vs blotter legs |
| 3 | Live gates | No entries on stale books (unless `--no-live-only` test) |
| 4 | SIGINT shutdown | Ctrl+C → resting orders cancelled, clean exit |
| 5 | Circuit breaker | Demo test with low `KALSHI_DEMO_MAX_DRAWDOWN_PCT=0.01` |
| 6 | Automated tests | `python -m pytest tests/ -v` all green |

---

## Where we are (2026-06-09)

**Code is ahead of certification.** Platform plumbing, strategy hardening, and the new `mean_reversion` strategy are implemented and unit-tested. The blocker to production is **signed live demo sessions**, not missing features.

| Area | Status | Next action |
|------|--------|-------------|
| Automated tests | **248/248 green** | Keep green before each live run |
| Platform (Week 1) | Code done; **not signed** | Complete steps 1.4–1.6 below |
| `high_prob` + `green_up` (Week 2) | Hardened; **partial live** | 3 demo sessions each with session template |
| `mean_reversion` (Week 2) | **Shipped + unit-tested**; **zero live sessions** | Discovery → demo round-trip (steps 2.8–2.10) |
| `kelly` + `arb` (Week 3) | Code done | 2 demo sessions each after Week 2 |
| Demo soak (Week 4) | Not started | Daily 1–2 hr runs after Week 2–3 |
| Prod micro-pilot (Week 5+) | **Partial** — one `green_up` prod run (2026-05-30) | Re-certify all strategies at $1 cap |

**Immediate priority order:** Week 1 sign-off → Week 2 (`high_prob`, `green_up`, `mean_reversion`) → Week 3 → Week 4 soak → Week 5 prod micro-pilot per strategy.

See [Next steps — execute in order](#next-steps--execute-in-order-2026-06-09) for commands and pass/fail criteria.

---

## How to read this plan

- **“Run bot 30 min”** means leave `main.py` connected to Kalshi demo for ~30 minutes, then stop it — not a special mode or timer flag.
- **“30 min” in live gates** (`KALSHI_LIVE_MAX_BOOK_STALE_MINUTES`) is different: it is the max age of a WebSocket order book before the bot blocks *new entries*. Week 1 step 1.4 is about **process stability and shutdown**, not that gate.
- **Strategy** for Week 1 can be anything lightweight; use discovery + `high_prob` or a single ticker + `green_up`. You are certifying **platform** behavior, not strategy P&L.
- After each live step, fill the [Session template](#session-template-copy-per-run) and grep `kalshi_bot_demo.jsonl` for `ERROR`, `traceback`, `risk_breach`, `kill switch`.

---

## Week-by-week plan

### Week 1 — Platform baseline (all strategies)

**Objective:** Shared infrastructure is trustworthy; failures are config/strategy, not plumbing.

| Step | Action | Expected output | If it fails |
|------|--------|-----------------|-------------|
| 1.1 | Copy `.env.example` → `.env`; set demo keys | Startup: `DEMO mode`, credentials loaded | Fix `KALSHI_DEMO_*` keys; see README Troubleshooting |
| 1.2 | `python -m pytest tests/ -v` | All tests pass | Fix regressions before live runs |
| 1.3 | `python -c "import config"` | Risk profile line shows demo DB/log/limits | Check `KALSHI_ENV` and `KALSHI_DEMO_*` vars |
| 1.4 | Run bot ~30 min, **Ctrl+C** | Log: shutdown, orders cancelled; no traceback | See [1.4 detailed](#step-14--run-bot-30-minutes-graceful-shutdown) |
| 1.5 | Trigger circuit breaker (demo) | Bot halts, `kill switch` in log | See [1.5 detailed](#step-15--circuit-breaker-review-and-live-test) |
| 1.6 | Manual sell test | `tools/trade.py sell --market` fills at bid floor | See [1.6 detailed](#step-16--manual-sell--flatten-test) |

**Week 1 deliverable:** Signed checklist rows 4–6 in [README Production checklist](../README.md#production-checklist) for platform only.

# Print on screen
```bash
$env:PYTHONIOENCODING = 'utf-8'

#### Step 1.4 — Run bot ~30 minutes + graceful shutdown

**What it means:** Prove the bot can stay up (WebSocket, portfolio risk sync, optional monitor table) and exit cleanly when you press **Ctrl+C** (SIGINT). You are **not** required to complete a full trade cycle in this step.

**Suggested command** (demo, low activity OK):

```bash
python -c "import config; print(config.ENV, config.MAX_POSITION_CENTS, config.DB_PATH)"

# Discover-only first (required — do not start the soak until this prints tickers,
# or fall back to explicit --tickers from screen.py below)
python main.py --discover --discover-category Sports --strategy high_prob --discover-only

# Demo Sports often has no high_prob band matches (YES ask 85–97¢ + fee-adjusted ROI).
# If discover-only exits with filter_rejections, pick liquid tickers via screen:
python tools/screen.py browse --category Sports --min-volume 10 --full-scan

# Platform soak with explicit tickers (Week 1 goal: stay up + clean shutdown, not P&L):
$env:KALSHI_TICKERS="KXMENWORLDCUP-26-EC,KXMENWORLDCUP-26-NO,KXMENWORLDCUP-26-CIV,KXMENWORLDCUP-26"
$env:KALSHI_TICKERS="KXMLBTOTAL-26MAY231420HOUCHC-6"
python main.py --tickers $env:KALSHI_TICKERS --strategy high_prob --hp-entry-mode passive --monitor-interval 30 --quiet

# If discover-only succeeded, you can use discovery for the soak instead:
python main.py --discover --discover-category Sports --strategy high_prob --discover-top 2 --hp-entry-mode passive --monitor-interval 30

python main.py --tickers KXMLBTOTAL-26MAY231420HOUCHC-6 --strategy high_prob --hp-min-yes-ask 70 --hp-entry-mode limit_offset --monitor-interval 30

python tools/trade.py buy --ticker KXMLBTOTAL-26MAY231420HOUCHC-6 --side yes --count 1 --price 69 --tif gtc --yes

$env:KALSHI_HP_LIMIT_OFFSET = "-1"
python main.py --tickers KXMLBTOTAL-26MAY231420HOUCHC-6 --strategy high_prob --hp-min-yes-ask 70 --hp-entry-mode limit_offset --monitor-interval 30
# --hp-entry-mode cross_spread or market

$env:KALSHI_HP_LIMIT_OFFSET = "-2"   # bid − 2¢

```bash
python main.py --discover --discover-category Sports --strategy green_up --discover-only --no-live-only --discover-top 5

```bash
# DISCOVERY: Table + near-misses (stdout)
python main.py --discover --discover-category Sports --strategy high_prob --discover-only
python main.py --discover --discover-category Sports --strategy green_up --discover-only

# PREVIEW
python tools/trade.py preview --ticker KXNBASPREAD-26MAY24OKCSAS-SAS9 --side yes --count 1 --price 75 --tif gtc

# TRADE
python main.py --tickers KXNBASPREAD-26MAY24OKCSAS-SAS9 --strategy green_up --gu-entry-mode passive --monitor-interval 30
python main.py --tickers KXNBASPREAD-26MAY24OKCSAS-SAS9 --strategy green_up --gu-entry-mode passive --entry-max 30 --monitor-interval 30

python main.py --tickers KXNBASPREAD-26MAY24OKCSAS-SAS9 --strategy green_up --gu-entry-mode passive --entry-max 30 --monitor-interval 30 --no-live-only

# Last discovery log lines (file)
Get-Content kalshi_bot.jsonl -Tail 20

# Filter discovery events only
Select-String -Path kalshi_bot.jsonl -Pattern "Ticker discovery complete|filter_rejections"
```

**While running (spot-check at ~5 and ~25 min):**

| Check | How | Pass |
|-------|-----|------|
| Auth | Console shows portfolio auth OK at startup | No exit code 1 on auth |
| WS alive | Monitor table or log shows bid/ask updating | No repeated WS disconnect errors |
| Risk sync | Log every `KALSHI_PORTFOLIO_RISK_SYNC_SECONDS` (default 30s) | No endless `Portfolio risk sync failed` |
| No crash | Console still running | No Python traceback |

**Stop:** Press **Ctrl+C** once. Wait until the process exits (usually <10s).

**Expected shutdown sequence** (see `main.py` docstring):

1. `shutdown` log with open blotter trade IDs and any resting order IDs
2. WebSocket ingestor stops
3. `execution_manager.stop()` → **all resting bot orders cancelled** on the exchange
4. Final settlement check + session summary printed

**Verify after exit:**

```bash
# No resting orders left from the bot session
python tools/trade.py orders

# Log should contain shutdown, not an unhandled exception
# PowerShell example:
Select-String -Path kalshi_bot_demo.jsonl -Pattern "shutdown|kill switch|traceback" | Select-Object -Last 20
```

**Pass criteria:** Process exits 0; log has `shutdown` / `OS signal`; `trade.py orders` is empty (or only unrelated manual orders you placed yourself). Open **positions** may remain — shutdown does **not** auto-flatten (by design).

**If it fails:** Traceback on exit → note line in log. Orphan resting orders → `python tools/trade.py cancel-all` then inspect `execution/execution_manager.py` `stop()`.

#### Step 1.5 — Circuit breaker: review and live test

**What the circuit breaker does:** Every signal passes through `risk/circuit_breaker.py` → `approve()`. A background loop (`_portfolio_risk_sync_loop` in `main.py`) refreshes your **real demo portfolio** every `KALSHI_PORTFOLIO_RISK_SYNC_SECONDS` (default **30s**) and calls `sync_from_portfolio()`. Any breach sets `is_tripped`, logs `risk_breach`, runs the **kill switch** (cancel all orders + shutdown), and blocks further entries until process restart.

**Five independent checks** (any one can trip):

| # | Check | Config (demo) | Typical trip in live test |
|---|--------|---------------|---------------------------|
| 1 | Peak-to-trough drawdown | `KALSHI_DEMO_MAX_DRAWDOWN_PCT` | Equity falls >1% below session peak |
| 2 | Session loss vs start equity | `KALSHI_DEMO_DAILY_LOSS_LIMIT_CENTS` | Portfolio value down more than limit since bot start |
| 3 | Daily realized P&L | same limit field | Cumulative closed loss today |
| 4 | Max open positions | `MAX_OPEN_POSITIONS` | Too many positions in breaker state |
| 5 | Single order size | `MAX_POSITION_CENTS` | Clamped, rarely trips alone |

**Review (read-only, ~15 min):**

1. Read `risk/circuit_breaker.py` — focus on `approve()`, `sync_from_portfolio()`, `_check_drawdown()`, `_trip()`.
2. Run unit tests: `python -m pytest tests/test_circuit_breaker.py tests/test_circuit_breaker_sync.py -v`
3. Confirm env resolves: `python -c "import config; print(config.MAX_DRAWDOWN_PCT, config.DAILY_LOSS_LIMIT_CENTS, config.PORTFOLIO_RISK_SYNC_SECONDS)"`

**Live demo test (drawdown path — matches production checklist):**

1. In `.env` **temporarily** set aggressive limits (restore after test):

   ```env
   KALSHI_DEMO_MAX_DRAWDOWN_PCT=0.01
   # Optional: shorten sync wait (default 30s is fine)
   # KALSHI_PORTFOLIO_RISK_SYNC_SECONDS=15
   ```

2. Start bot on demo with a ticker you are willing to hold briefly:

   ```bash
   python main.py --strategy green_up --tickers YOUR-TICKER \
     --entry-max 50 --hedge-trigger 90 --gu-entry-mode market --monitor-interval 15
   ```

3. **Trigger drawdown:** Either wait for an open position to mark down ≥1% of **total** `portfolio_value_cents`, **or** open a small losing position manually before/during the run:

   ```bash
   python tools/trade.py buy --ticker YOUR-TICKER --side yes --count 5 --market
   # If price moves against you, next portfolio sync may trip 1% drawdown on a small account
   ```

4. **What you should see within one sync interval (~30s):**
   - JSONL / console: `risk_breach` with reason `max drawdown exceeded` (or `session loss limit exceeded`)
   - `kill switch activated — cancelling all orders and halting`
   - Bot process stops (shutdown event set)
   - No new `ORDER_SENT` after trip

5. **Verify:**
   - `python tools/trade.py orders` → empty
   - Grep log: `kill switch`, `risk_breach`, `is_tripped` / rejecting signals

6. **Restore** `.env` drawdown to `0.10` (or your normal demo value). Restart bot only after limits are sane.

**“Review” deliverable:** One paragraph **per test** in your session log: which limit tripped, peak vs current equity from log fields, time from breach to kill switch, and confirmation orders were cancelled.

##### Test A — Percent drawdown (`MAX_DRAWDOWN_PCT`)

| Step | Action |
|------|--------|
| 1 | `.env`: `KALSHI_DEMO_MAX_DRAWDOWN_PCT=0.01` (restore to `0.10` after) |
| 2 | Note starting equity: `python tools/trade.py portfolio` → `portfolio_value` |
| 3 | Start bot briefly, or hold an open position that can mark down |
| 4 | Need **≥1% drop from session peak** (account-level, not per contract). Example: $10,000 account → ≥$100 drop in `portfolio_value_cents` |
| 5 | Wait ≤ `KALSHI_PORTFOLIO_RISK_SYNC_SECONDS` (default 30s) |
| 6 | Expect log: `max drawdown exceeded`, `kill switch`, process exit |

##### Test B — Session loss limit (`DAILY_LOSS_LIMIT_CENTS`)

**Dollars of session loss**, not per contract. Compares current `portfolio_value_cents` to equity at **first sync after bot start**.

| Step | Action |
|------|--------|
| 1 | `.env`: `KALSHI_DEMO_DAILY_LOSS_LIMIT_CENTS=500` ($5 cap; restore after) |
| 2 | Start bot → first sync sets `_session_start_equity` |
| 3 | Realize **≥$5** loss vs that start (trade, mark-down, or manual) |
| 4 | On next sync: `session loss limit exceeded` + kill switch |

Run Test A and Test B in **separate** sessions; restore normal limits between them. Per-contract fees flow into P&L via `FEE_PER_CONTRACT_CENTS`, not a separate breaker.

#### Step 1.6 — Manual sell / flatten test

Validates `tools/trade.py` and sell pricing independent of strategies.

1. Pick a ticker with a **small** open YES position (from a prior test or `trade.py buy`).
2. Preview sell:

   ```bash
   python tools/trade.py preview --ticker YOUR-TICKER --side yes --count 1 --market
   ```

   Expect: action **SELL**, price at or near **bid** (market sell walks the bid).

3. Execute:

   ```bash
   python tools/trade.py sell --ticker KXMENWORLDCUP-26-SE --side yes --count 1 --market --yes
   # Or flatten entire leg:
   python tools/trade.py close --ticker YOUR-TICKER --yes
   ```

4. `python tools/trade.py portfolio` → position reduced or flat.

**Note:** Manual `trade.py` orders are **not** written to the bot blotter. Week 2+ strategy tests should use bot-driven orders for blotter reconciliation.

---

### Week 2 — Certify `high_prob` and `green_up`

**Objective:** Two strategies with full runbooks and 3+ clean demo sessions each.

#### High-probability

| Step | Command (example) | Expected output | If it fails |
|------|-------------------|-----------------|-------------|
| 2.1 | `main.py --discover --discover-category Politics --strategy high_prob --discover-only` | 5–10 tickers, YES ask 85–97¢ | Widen discovery or check preset; `--discover-no-tradeable-filter` to debug |
| 2.2 | Run 45–60 min, 2 tickers, `--hp-entry-mode cross_spread` | `ORDER_SENT`, fills, blotter `entry` leg | Passive may not fill → use `cross_spread` or `market` |
| 2.3 | Post-session | `blotter.py detail`, `portfolio` match | Reconcile fees; check fill-on-blotter not submit-only |
| 2.4 | Repeat 3 sessions | Stable logs, no orphans on exchange | See Troubleshooting: fill rate vs orders sent |

#### Green-up

| Step | Command (example) | Expected output | If it fails |
|------|-------------------|-----------------|-------------|
| 2.5 | `main.py --discover --discover-category Sports --strategy green_up --discover-top 5 --gu-entry-mode market --max-concurrent-positions 5` | Live Sports tickers, states watching→entered | Live gates block → wait for WS; check `--entry-max` |
| 2.6 | Through hedge or stop | **Hedge:** parent `hedged`, entry + `hedge` legs open until settlement. **Stop:** entry leg **closed** at stop fill price, parent `closed`, realised P&L on entry leg | Tune `--hedge-trigger`, `--gu-exit-mode cross_spread`; if parent stuck `open`, run `scripts/cleanup_stale_trades.py` |
| 2.7 | Flatten manually if needed | `trade.py close` works | Sell uses bid floor; portfolio shows side |

**Week 2 testing depth:** Use the [End-to-end strategy testing](#end-to-end-automated-strategy-testing) playbook below for `green_up`; adapt the same phases for `high_prob` (entry → optional TP/stop → hold/settle) and `mean_reversion` (entry → resting TP/stop → round-trip close).

#### Mean reversion

| Step | Command (example) | Expected output | If it fails |
|------|-------------------|-----------------|-------------|
| 2.8 | `main.py --discover --discover-category Sports --strategy mean_reversion --discover-only` | Mid-range YES (15–85¢), screener-ranked | Widen `--discover-activity-hours`; check min vol 500 |
| 2.9 | Run 45–60 min, `--mr-trade-direction both --mr-post-fill tp_and_stop` | `watching→entered→exit_pending→closed` cycles on volatile tickers | Flat book → no entry (volatility filter); use live Sports |
| 2.10 | Post-session | Blotter `closed` parents with entry + exit legs | Same exit-fill hardening as `high_prob` |

**Note:** `mean_reversion` needs enough ticks to build rolling mean/volatility — prefer active in-play markets over stale books.

**Week 2 deliverable (updated):** Three sections in `testing.md` (or session logs here) with exact commands + **3 signed demo sessions each** for `high_prob`, `green_up`, and `mean_reversion`.

#### Mean reversion — demo certification runbook (Phase A)

**Goal:** One full round-trip (entry → resting TP or stop → blotter `closed`) on one volatile in-play ticker.

| Step | Action | Success criteria |
|------|--------|------------------|
| MR-A.1 | Env check | `python -c "import config; print(config.ENV, config.MAX_POSITION_CENTS)"` → `demo` + expected limits |
| MR-A.2 | Discovery | `python main.py --discover --discover-category Sports --strategy mean_reversion --discover-only` → ≥1 ticker in 15–85¢ band, screener-ranked; if zero, widen `--discover-activity-hours` or use `screen.py browse` |
| MR-A.3 | Pick ticker | Active in-play Sports market with tight spread (≤ `--mr-max-spread` 8¢) and visible price swings |
| MR-A.4 | Warm-up run | Run 10–15 min with `--discover-only` equivalent tick count OR explicit ticker; monitor must show `scanning` → `watching` after `--mr-min-samples` ticks |
| MR-A.5 | Certification session | See command block below; use `cross_spread` entry if passive does not fill in 20 min |
| MR-A.6 | Observe cycle | Log/monitor: `ORDER_SENT` entry → fill → `ENTERED` → `EXIT_PENDING` → exit/stop fill → `CLOSED` |
| MR-A.7 | Reconcile | `blotter.py detail` shows entry + exit legs; entry leg has `realized_pnl_cents`; `trade.py portfolio` matches exchange |
| MR-A.8 | Repeat ×3 | Three sessions on different days/tickers; fill [Session template](#session-template-copy-per-run) each time |

**Certification command** (single ticker, aggressive fills):

```bash
python main.py --strategy mean_reversion --tickers YOUR-TICKER \
  --mr-trade-direction long \
  --mr-post-fill tp_and_stop \
  --mr-entry-mode cross_spread \
  --mr-exit-mode cross_spread \
  --mr-lookback 15 --mr-min-samples 10 \
  --mr-entry-deviation 4 --mr-min-volatility 3 \
  --mr-take-profit-offset 5 --mr-stop-loss-cents 8 \
  --max-concurrent-positions 1 \
  --monitor-interval 15
```

**Pass:** Parent trade status `closed` with realised P&L on entry leg; no orphan resting orders after Ctrl+C; no `traceback` / `ERROR` spam in `kalshi_bot_demo.jsonl`.

**Short-leg variant** (optional second session): `--mr-trade-direction short` on a ticker with YES ask 55–90¢ and spike above rolling mean.

**Prod micro-pilot** (Week 5.2c only after MR-A.8): cap stake at 100¢ — use `--mr-stake-cents 100` or `KALSHI_MR_STAKE_CENTS=100`; create `scripts/run_mean_reversion_prod.ps1` mirroring `run_high_prob_prod.ps1`.

---

### Week 3 — Certify `kelly` and `arb`

**Objective:** Remaining strategies have runbooks; arb expectations documented for thin demo books.

#### Kelly

| Step | Action | Expected output | If it fails |
|------|--------|-----------------|-------------|
| 3.1 | Pick 2 liquid tickers from screener | Tickers with tight spread | Use `tools/screen.py screen` |
| 3.2 | `main.py --strategy kelly --tickers T1,T2 --model-prob T1:0.62,T2:0.55` | Orders only when edge ≥ `MIN_EDGE_TO_VIG` | Raise model prob or lower spread markets |
| 3.3 | 2 sessions + blotter | Entry legs sized ≤ `MAX_POSITION_CENTS` | Kelly size 0 → edge too small |

#### Arbitrage

| Step | Action | Expected output | If it fails |
|------|--------|-----------------|-------------|
| 3.4 | Define `--comp-pairs T1:T2` or category discover | Arb signals in log | Demo may have **no** arb — document as OK |
| 3.5 | 2 sessions | Both legs fill or explicit skip logged | Partial leg → manual close; reduce size |

**Week 3 deliverable:** Five strategy runbooks complete; demo certification sign-off.

---

### Week 4 — Demo soak + production prep

**Objective:** Two weeks of demo confidence compressed into structured review; prod `.env` ready but off.

| Step | Action | Expected output | If it fails |
|------|--------|-----------------|-------------|
| 4.1 | Daily: 1–2 hr bot runs (rotate strategies) | `kalshi_bot_demo.jsonl` without ERROR spam | Fix root cause before prod |
| 4.2 | Weekly: `blotter_report.py performance --days 7` | P&L summary per strategy | Tune params, not code |
| 4.3 | `session_report.py` end of week | Closed/settled trades documented | Settlement watcher gaps → check API |
| 4.4 | Set prod keys in `.env`, keep `KALSHI_ENV=demo` | Prod keys present but unused | Never commit `.env` |
| 4.5 | Dry-run: `KALSHI_ENV=production` + **Ctrl+C immediately** | `PRODUCTION MODE`, prod DB/log, then exit | Wrong key → 401 on first API call |

**Week 4 deliverable:** Demo certification checklist 100% in README; prod profile validated without trading.

---

### Week 5+ — Production micro-pilot ($1 cap per market)

**Only after Week 4 sign-off.** Certify **each strategy separately** at `KALSHI_PROD_MAX_POSITION_CENTS=100` (one contract on a 99¢ market ≈ $1).

| Step | Action | Expected output | If it fails |
|------|--------|-----------------|-------------|
| 5.1 | `.env`: `KALSHI_ENV=production`, `KALSHI_PROD_MAX_POSITION_CENTS=100`, `KALSHI_PROD_MAX_CONCURRENT_POSITIONS=1` | Startup: `PRODUCTION`, max position $1.00 | Revert to `demo` |
| 5.2a | **high_prob:** 1 Politics/Sports ticker, 1 session | ≤ $1 per market; blotter + UI match | See Week 2 high_prob runbook |
| 5.2b | **green_up:** 1 in-play Sports ticker, 1 session | ≤ $1 entry leg; hedge/stop still allowed | See green_up Phase A; use `market` entry if needed |
| 5.2c | **mean_reversion:** 1 volatile Sports ticker, 1 session | ≤ $1 per round-trip; TP/stop exits | See Week 2 mean_reversion runbook; `--mr-post-fill tp_and_stop` |
| 5.3 | One week **per strategy** (alternate days or weeks) | Daily loss &lt; `KALSHI_PROD_DAILY_LOSS_LIMIT_CENTS` | Revert to demo; post-mortem |
| 5.4 | Optional: kelly / arb at same $1 cap | Same checks | Do not raise cap until both 5.2a and 5.2b are stable |

**Example prod commands:**

```bash
# high_prob — one liquid high-P(YES) market
python main.py --strategy high_prob --tickers YOUR-TICKER --hp-entry-mode cross_spread --hp-post-fill hold --max-concurrent-positions 1

# Preview first (bid 78, bid-2 = 76)
python tools/trade.py preview --ticker KXMLBTOTAL-26MAY231420HOUCHC-6 --side yes --count 1 --price 76 --tif gtc --monitor-interval 30

# Place
python tools/trade.py buy --ticker KXMLBTOTAL-26MAY231420HOUCHC-6 --side yes --count 1 --price 76 --tif gtc --yes

#  price = max(1, min(99, best_bid + limit_offset))
#  tif = "gtc" if price <= best_bid else "ioc"
#  return price, "limit", tif

$env:KALSHI_HP_LIMIT_OFFSET = "-2"
python main.py --tickers $env:KALSHI_TICKERS --strategy high_prob `
  --hp-min-yes-ask 75 `
  --hp-entry-mode limit_offset `
  --monitor-interval 30

$env:KALSHI_HP_LIMIT_OFFSET = "-2"
python main.py --tickers KXMLBTOTAL-26MAY231420HOUCHC-6 --strategy high_prob --hp-min-yes-ask 75 --hp-entry-mode limit_offset --monitor-interval 30


# green_up — one live underdog
python main.py --strategy green_up --tickers YOUR-TICKER \
  --entry-max 25 --hedge-trigger 68 --gu-entry-mode market \
  --gu-exit-mode cross_spread --max-concurrent-positions 1

python main.py --tickers $env:KALSHI_TICKERS --strategy green_up `
  --entry-max 85 `
  --gu-entry-mode limit_offset --gu-limit-offset -2 `
  --monitor-interval 30
```

---

## End-to-end automated strategy testing

This section maps your mental model (“find opportunity → enter → manage → exit → P&L → repeat until event ends”) to what the bot **actually** does today, with a concrete **green_up** runbook. Other strategies follow the same **phases** with different exit rules.

### Mental model vs bot behavior

| Your step | Green-up implementation | Config knobs |
|-----------|-------------------------|--------------|
| Find opportunity | `--discover` + Sports preset, or `--tickers T` | `--discover-category`, `--entry-max` |
| Limit at bid (or bid−n¢) | Default `--gu-entry-mode passive` → limit at **YES bid** | `passive`, `cross_spread`, `market`, `limit_offset` (+ `KALSHI_HP_LIMIT_OFFSET` for offset cents) |
| Order fills | `on_fill` → state `ENTERED` | Use `market` or `cross_spread` if passive does not fill in demo |
| Monitor market | WS ticks → `evaluate()` each tick | `--hedge-trigger`, `--stop-loss` |
| Take profit (¢ target) | Default: **hedge** (buy NO) at `--hedge-trigger`. Optional: use `high_prob` with low YES-ask band for resting TP (see below) | `--hedge-mode`, `--hedge-trigger` |
| Hedge opposite side | Buy **NO** when YES bid ≥ hedge trigger | `full_green` \| `stake_back` \| `partial` |
| Stop / cut loss | Sell YES or buy NO on stop path | `--stop-loss`, `--gu-exit-mode` |
| Track P&L | Blotter parent trade + legs; `blotter_report.py` | `tools/blotter.py detail` |
| Repeat on same game | After `HEDGED`/`STOPPED`, next tick resets to `SCANNING` and may enter again (`--gu-max-cycles 0` = unlimited) | `--gu-max-cycles N` to cap round-trips |
| Until game over | Stop when market resolves (`CLOSED`) or you end session | Settlement watcher updates blotter |

**Important:** The bot does **not** currently implement a separate “liquidate at entry + X cents” take-profit mode for green_up (that pattern exists for `high_prob` via `--hp-post-fill`). Green-up “profit taking” is **hedge-driven** or holding YES to settlement.

### Green-up state machine (what to watch)

```text
SCANNING → WATCHING → ENTERED → HEDGING → HEDGED
                ↓
            STOPPING → STOPPED → (may return to SCANNING when flat)
```

Session monitor / logs should show state transitions per ticker. One **parent trade** in the blotter typically spans `entry` + `hedge` or `stop_loss` legs.

### Phase A — Single ticker, controlled entry (certification)

**Goal:** One full cycle on one market you choose, with parameters tight enough to force hedge or stop in a reasonable session.

1. **Pick ticker** (in-play Sports, live gates):

   ```bash
   python tools/screen.py screen --category Sports --top 10
   python tools/screen.py browse --ticker YOUR-TICKER
   python tools/screen.py browse --ticker KXMLB-26-LAD
   ```

   Prefer: YES ask ≤ `--entry-max`, volume ≥ preset, game closing within live window.

2. **Dry run discovery** (optional):

   ```bash
   python main.py --discover --discover-category Sports --strategy green_up \
     --discover-top 5 --discover-only
   ```

3. **Run bot — single ticker, aggressive fill for certification:**

   ```bash
   python main.py --strategy green_up --tickers YOUR-TICKER \
     --entry-max 25 --hedge-trigger 20 --hedge-mode full_green \
     --stop-loss 0.35 \
     --gu-entry-mode passive \
     --gu-exit-mode cross_spread \
     --max-concurrent-positions 1 \
     --monitor-interval 10
   ```

   | Parameter | Certification tip |
   |-----------|---------------------|
   | `--entry-max` | Set ≥ current YES ask so entry is allowed |
   | `--hedge-trigger` | Set **slightly above** current YES bid to test hedge quickly, or near realistic in-play level |
   | `--gu-entry-mode passive` | Resting at bid — may not fill; switch to `market` if no fill in 15 min |
   | `--gu-exit-mode cross_spread` | Hedge/stop legs cross the book (more reliable fills) |

4. **Observe until terminal state:**

   | Phase | Log / monitor | Pass |
   |-------|---------------|------|
   | Entry signal | `ORDER_SENT` buy YES | Price matches mode (bid for passive) |
   | Fill | Fill event / `ENTERED` | Exchange portfolio shows YES |
   | Hedge or stop | `ORDER_SENT` buy NO or stop leg | State → `HEDGING` or `STOPPING` |
   | Complete | `HEDGED` or `STOPPED` | **Hedge:** parent `hedged`, two open legs. **Stop:** entry leg closed with realised P&L, parent `closed` (not a phantom second leg) |

5. **Reconcile:**

   ```bash
   python tools/trade.py portfolio
   python tools/blotter.py trades --strategy green_up --days 1
   python tools/blotter.py detail --trade-id T-XXXX
   ```

   Exchange fills must match blotter legs (fees within tolerance). Parent status: **`hedged`** when both legs filled (await settlement), **`closed`** after stop or high_prob exit, **`settled`** after `SettlementWatcher` resolves the market.

### Phase D — P&L and settlement

| When | Command | What to verify |
|------|---------|----------------|
| Intraday | `blotter.py detail --trade-id …` | Leg prices, fees; stop/exit shows **closed** entry leg with `realized_pnl_cents` |
| After hedge | `blotter.py open --status hedged` | Parent `hedged`; entry + hedge legs open; net P&L null until settlement |
| End of day | `blotter_report.py performance --days 1` | Per-strategy net (realised from closed legs only) |
| After event | `blotter.py trades --status settled` | Resolution matches Kalshi UI; both legs closed |
| Legacy cleanup | `python scripts/cleanup_stale_trades.py` (dry-run) | Reconciles pre-fix phantom legs / stuck parents; `--apply` to persist |

Settlement watcher runs every 5 min while bot is up; final check also runs on shutdown.

### Phase B — Discovery mode, parallel tickers (production-like)

```bash
python main.py --discover --discover-category Sports --strategy green_up \
  --discover-top 5 --max-concurrent-positions 3 \
  --entry-max 25 --hedge-trigger 68 --hedge-mode full_green \
  --gu-entry-mode market --gu-exit-mode cross_spread \
  --monitor-interval 30
```

**Pass:** Multiple tickers in `SCANNING`/`WATCHING`; up to 3 concurrent **entry** legs; hedges still allowed when trigger hits. No orphan exchange orders after Ctrl+C.

### Phase C — Repeat cycles on the same game

After `HEDGED` or `STOPPED`, the strategy resets that ticker toward **SCANNING** when flat and market still passes [live gates](../README.md#live-markets-only-default-on). For the same game:

- Keep the bot running through score swings; you may see **multiple parent trades** on one ticker in the blotter.
- Stop the session when the market resolves or `--discover-max-minutes-to-close` window ends.

**Session end checklist:**

```bash
python tools/blotter.py trades --ticker YOUR-TICKER --days 1
python tools/blotter_report.py performance --days 1
python tools/trade.py portfolio
```

Document: number of round-trips, net P&L per trade ID, any manual `trade.py close` interventions.

### Pricing modes (entry at bid vs bid − n¢)

**Production default:** `--gu-entry-mode passive` (resting limit at best YES **bid**).

**Bid − x cents:** use `limit_offset` with a **negative** offset (price = bid + offset):

```bash
python main.py --strategy green_up --tickers YOUR-TICKER \
  --gu-entry-mode limit_offset --gu-limit-offset -2
# or: KALSHI_GREEN_UP_LIMIT_OFFSET=-2
```

| Mode | Buy YES price | When to use |
|------|---------------|-------------|
| `passive` (default) | Best **bid**, GTC resting | Production |
| `cross_spread` | Best **ask**, IOC | Certification fills |
| `market` | IOC market at touch | Fastest fill |
| `limit_offset` | `bid + offset` cents | `-2` → bid−2¢; `+1` → bid+1¢ (may IOC if above bid) |

### Take-profit: green_up vs high_prob vs mean_reversion

They are **not** the same mechanism today:

| | **green_up** | **high_prob** | **mean_reversion** |
|---|-------------|---------------|---------------------|
| Entry zone | Cheap YES (e.g. ask ≤ 25¢) | High YES (85–97¢) | Mid-range oscillators; dip or spike vs rolling mean |
| “Take profit” | Buy **NO** when YES bid ≥ hedge trigger (formulas: full_green / stake_back / partial) | Resting **sell YES** at entry + offset or % (`--hp-post-fill resting_take_profit`) | Resting **sell YES** (long) or **sell NO** (short) toward mean / offset (`--mr-post-fill`) |
| Implied “fair” | `hedge_trigger` as fair value for Kelly edge, not a model P(YES) | Market implied P from YES ask | Rolling mid-price mean over `--mr-lookback` ticks |

**If you want high_prob-style resting TP on a cheap contract:** you can run `high_prob` with a **low** band, e.g. `--hp-min-yes-ask 10 --hp-max-yes-ask 30`, plus `--hp-post-fill resting_take_profit`. That is a different strategy path than green_up hedging. **`mean_reversion`** is the native path for round-trip volatility capture (buy dip → sell on revert, or fade spike via buy-NO). A native green_up resting-TP mode is not implemented yet (backlog).

### Max cycles per ticker (round-trips)

| Value | Meaning |
|-------|---------|
| `0` (default) | Unlimited new entries on same ticker after each `HEDGED`/`STOPPED` |
| `N ≥ 1` | After **N** completed cycles, ticker moves to `CLOSED` (no more entries) |

```bash
--gu-max-cycles 3
# KALSHI_GREEN_UP_MAX_CYCLES_PER_TICKER=3
```

**Note:** A new cycle only resets the **state machine**. If the exchange still holds YES+NO from a hedge, you may be stacked — flatten via `trade.py close` between cycles if you want a clean book.

### What is *not* automated yet (gaps vs ideal test)

- No built-in green_up “TP at +X¢” — use hedge trigger or `high_prob` for resting TP.
- Shutdown does not flatten; open positions survive Ctrl+C.
- Manual `trade.py` trades do not sync to blotter.
- Demo books can be too thin for passive fills — document `market`/`cross_spread` in certification notes.

### Quick matrix: strategy → exit style

| Strategy | Primary exit | Repeat on same ticker? |
|----------|--------------|-------------------------|
| `green_up` | Hedge (NO) or stop | Yes, while market live |
| `high_prob` | Resting TP / stop / hold | Yes, with post-fill modes |
| `mean_reversion` | Resting TP / stop toward rolling mean | Yes, with `--mr-max-cycles` |
| `kelly` | Hold / manual | Re-enters when edge returns |
| `arb` | Both legs immediate | Pair-dependent |

---

## Session template (copy per run)

```text
Date:
KALSHI_ENV:
Strategy:
Command:
Duration:
Tickers discovered:
Orders sent / fills (exchange):
Blotter trade IDs:
Open positions after (portfolio):
Issues:
Action items:
```

---

## Post-certification feature backlog (defer until demo done)

| Feature | Why wait |
|---------|----------|
| Fill funnel metrics (sent → exchange fill → WS confirm) | Needs stable baseline to interpret |
| Blotter CLI `--resolution` / unified search | Analysis convenience |
| Manual `trade.py` → blotter sync | Workaround exists |
| Auto-flatten on shutdown (optional flag) | Policy choice after manual close workflow is proven |

---

## Quick reference commands

```bash
# Environment check
python -c "import config; print(config.ENV, config.MAX_POSITION_CENTS, config.DB_PATH)"

# Tests
python -m pytest tests/ -v

# Discover preview
python main.py --discover --discover-category Sports --strategy green_up --discover-only

# Portfolio (ground truth)
python tools/trade.py portfolio

# After session
python tools/blotter.py trades --days 1
python tools/blotter.py detail --trade-id T-0001
```

---

## Certification decisions (locked)

| Topic | Decision |
|-------|----------|
| Green-up take profit | Hedge-at-trigger is primary; high_prob-style resting TP = run `high_prob` on a low ask band, or add green_up TP later |
| Entry pricing | **Passive at bid** in prod; `limit_offset` with negative cents for bid−x; `market`/`cross_spread` for certification fills |
| Per-game repeat | **Unlimited** by default (`--gu-max-cycles 0`); set `--gu-max-cycles N` to cap round-trips per ticker |
| Circuit breaker | Test **both** drawdown % (Test A) and session loss cents (Test B) in demo — see [Step 1.5](#step-15--circuit-breaker-review-and-live-test) |
| Prod micro-pilot | **Each strategy separately** at `KALSHI_PROD_MAX_POSITION_CENTS=100`: `high_prob` → `green_up` → `mean_reversion` (Week 5.2a–c) |
| Mean reversion | Native round-trip TP/stop path | Demo certify before prod; see [MR-A runbook](#mean-reversion--demo-certification-runbook-phase-a) |

---

## Implementation status (2026-06-09)

Snapshot of **code shipped** vs **live certification** vs **production ops**. Code being present does not mean the roadmap step is signed off. Supersedes the 2026-05-31 snapshot.

### Summary

| Phase | Code / tooling | Live certification |
|-------|----------------|-------------------|
| Environment profiles | Done | Verify each session with `python -c "import config; …"` |
| Week 1 — platform baseline | Done | **In progress** — soak, circuit breaker, manual sell not formally signed |
| Week 2 — `high_prob` + `green_up` + `mean_reversion` | Done (hardened) | **Partial** — `testing.md` has `high_prob`/`green_up` commands; **one prod green_up micro-run** (2026-05-30); **`mean_reversion` has zero live sessions** |
| Week 3 — `kelly` + `arb` | Done | **Not started** — no documented demo sessions |
| Week 4 — demo soak + prod dry-run | Tooling done | **Not started** — no 2-week soak; prod dry-run not recorded |
| Week 5+ — prod micro-pilot | Scripts for `high_prob`, `green_up` | **Partial** — green_up prod session logged; **`run_mean_reversion_prod.ps1` not yet created** |

**Automated tests:** `248/248` pass (`python -m pytest tests/ -q`). Net since 2026-05-31 snapshot: +24 tests — `mean_reversion` strategy suite, expanded `high_prob` / `book_normalize` / `game_close` coverage.

### Definition of done (demo) — status

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Discover → WS → signal → order → fill → blotter | **Improved** | Exit/stop fills now close entry legs with realised P&L; hedged parents await settlement; prod green_up stop path validated post-fix |
| 2 | Exchange matches bot | **Improved** | Blotter + `trade.py portfolio`; `scripts/cleanup_stale_trades.py` for legacy rows; manual `trade.py` orders still excluded (known gap) |
| 3 | Live gates | Implemented | Default on; `--no-live-only` used in many `testing.md` runs |
| 4 | SIGINT shutdown | Implemented | `main.py` cancel-all + settlement check; needs signed live run |
| 5 | Circuit breaker | Implemented | Unit tests + portfolio sync; live Tests A/B not recorded in checklist |
| 6 | Automated tests | **Done** | `248/248` green in full suite |

### Week-by-week certification progress

| Week | Step | Code ready? | Certified? |
|------|------|-------------|------------|
| 1 | 1.1 Demo keys / startup | Yes | Assumed (active dev) |
| 1 | 1.2 `pytest tests/` | **Yes** | 248/248 green |
| 1 | 1.3 Config profile print | Yes | — |
| 1 | 1.4 ~30 min soak + Ctrl+C | Yes | Not signed in README checklist |
| 1 | 1.5 Circuit breaker live (A + B) | Yes | Not signed |
| 1 | 1.6 Manual sell / flatten | Yes (`tools/trade.py`) | Not signed |
| 2 | `high_prob` 3 sessions | Yes | Commands in `testing.md`; no session template sign-off |
| 2 | `green_up` 3 sessions | Yes | Same |
| 2 | `mean_reversion` 3 sessions | **Yes** (new) | Unit-tested only; see [MR-A runbook](#mean-reversion--demo-certification-runbook-phase-a) |
| 3 | `kelly` 2 sessions | Yes | Screener + `--model-prob`; no runbook sign-off |
| 3 | `arb` 2 sessions | Yes | Demo may show zero arb (expected) |
| 4 | Daily 1–2 hr soak | — | Not started |
| 4 | Weekly blotter / session reports | Yes (`blotter_report.py`, `session_report.py`) | Not run on schedule |
| 4 | Prod keys in `.env`, stay on demo | — | Unknown |
| 4 | Prod dry-run (immediate Ctrl+C) | Yes | Not recorded |
| 5 | Prod micro-pilot per strategy | Partial (`run_high_prob_prod.ps1`, `run_green_up_prod.ps1`) | **Partial** — green_up prod run 2026-05-30; `mean_reversion` prod script pending |

### Built since roadmap was written (beyond original scope)

These ship in code but are not separate certification steps:

- **Discovery drill-down** — `--discover-tag`, `--discover-sport`, `--discover-competition`, `--discover-scope`, `--discover-series`; `tools/screen.py` browse/tags/sports-filters/series
- **Near-miss discovery table** — when zero tickers pass, shows top rejections (tune filters)
- **REST book fallback** — polls REST when WS book stale (`KALSHI_WS_BOOK_REST_FALLBACK_SECONDS`)
- **Sector concentration limit** — `KALSHI_MAX_SECTOR_CONCENTRATION` in circuit breaker (prod scripts set `1.0` for micro-pilot)
- **Trending category** — cross-category volume scan default for discovery

### Built since the 2026-05-25 snapshot (resilience + strategy hardening)

Major reliability work landed in commits `69e104f` (price targets / green-up refactor) and `772e74a` (WS liveness watchdog + REST fill reconciliation):

**Connection & fill resilience**
- **WS liveness watchdog** — `ingestion/market_ingestor.py` forces a reconnect when the socket is silent past `KALSHI_WS_MAX_SILENCE_SECONDS` (default 45 s), catching half-open sockets that keep TCP alive but stop delivering data.
- **WS-health escalation** — `ingestion/rest_book_fallback.py` counts consecutive stale-book polls and calls `force_reconnect()` after `KALSHI_WS_STALE_ESCALATE_POLLS` (default 3) so the WS-only `fill` channel is re-established, not just the market-data books.
- **REST fill reconciliation** — `trading/fill_reconciler.py` polls `GET /portfolio/fills` every `KALSHI_FILL_RECONCILE_SECONDS` (default 20 s) and re-drives `on_fill` for any tracked open order the WebSocket missed. `on_fill_received` dedups by fill/trade id so WS + REST paths are safe to run together. **Closes most of the "fills silently dropped" risk that previously left exits unmanaged.**

**Observability**
- **AlertManager** (`risk/alert_manager.py`) — six alert types (profit target, position stop, expiry warning/urgent, fill timeout, low balance) with per-key cooldown and **bot-ownership attribution** (separates the bot's own legs from manual/other positions on the same ticker via the blotter). Runs as a background task off `PortfolioMonitor`.
- **PortfolioMonitor** (`trading/portfolio_monitor.py`) — mark-to-market snapshot of all open positions (unrealised P&L, implied prob, expiry) feeding the circuit-breaker sync, alert manager, and session table.
- **MetricsStore** (`metrics/metrics_store.py`) — SQLite tables for fills, equity snapshots, and signal intent; exposes `get_signal_fill_rate()` (a first cut at the deferred fill-funnel metric) and legacy-table migration.

**Strategy hardening (`green_up`, `high_prob`)**
- **Per-fill price targets** (`strategy/price_targets.py`) — hedge/stop triggers are now computed from the *actual* entry fill price, so later cycles adapt instead of using a static session threshold. Relative hedging via `--hedge-offset` (hedge when YES bid ≥ entry + N¢).
- **Green-up resting hedge** — `--gu-hedge-style resting` posts a GTC buy-NO at the trigger-implied price immediately after entry fill (vs `trigger`, which waits for the bid to reach the level).
- **Green-up stop that actually fills** — `_stop_sell_mode()` escalates `PASSIVE` stops to `CROSS_SPREAD` so a protective sell crosses to the bid instead of resting at the ask and chasing a falling book without filling. Resting stops reprice as the bid moves (`_manage_resting_stop`), and `rollback_hedge` / `rollback_stop` let legs retry safely after a failed submit or circuit-breaker reject.
- **Contract-rounding alignment** — green-up sizes the entry to whole contracts (`size_cents = contracts × limit_price`) so the logged hedge/locked-profit preview matches what actually fills.
- **High-prob fee-adjusted ROI gate** — entries pass `passes_roi_gate()` with round-trip fees baked in (`HP_USE_FEE_ADJUSTED_ROI`); take-profit supports `--hp-take-profit-pct` (vig-aware) or legacy offset, with `--hp-tp-style fixed|at_ask`, combined `tp_and_stop` post-fill mode, cents-or-pct stop loss, and optional `--hp-require-model-edge`.
- **Shared execution pricing** (`strategy/execution_price.py`) — unified `passive` / `cross_spread` / `market` / `limit_offset` resolution for YES buy, YES sell, YES exit, and NO buy legs across both strategies.
- All new CLI flags are wired in `main.py` (`--gu-hedge-style`, `--gu-limit-offset`, `--gu-max-spread`, `--gu-no-entry-max`, `--hedge-offset`, `--hp-exit-mode`, `--hp-take-profit-pct`, `--hp-stop-loss-cents`, `--hp-tp-style`).

### Green-up in-play execution — FIXED (2026-06-06)

Motivated by a **production green_up session** on `KXNBAGAME-26JUN05NYKSAS-NYK` (cycle 1 hedged correctly; cycle 2 re-entered at 63¢, stop suppressed by wrong `minutes_to_close`, 1¢ hedge IOC unfilled on a one-sided book).

**Game close time (`discovery/game_close.py`, `discovery/market_registry.py`)**
- Parses `*GAME-*` ticker dates and applies **effective `minutes_to_close`** when API settlement is far in the future but the market is actively trading.
- In-play windows report ≤ 5 minutes to close so `check_stop_loss_allowed()` / `KALSHI_STOP_LOSS_CLOSE_WINDOW_MINUTES` allow mid-event stops instead of holding through dips.

**Hedge trigger cap (`strategy/price_targets.py`)**
- Relative hedge triggers cap at **95¢** (`KALSHI_GREEN_UP_HEDGE_TRIGGER_CAP`, default 95) instead of 99¢.
- Skips entries when `entry + offset` would exceed the cap; keeps complement NO legs off 1¢ one-sided resolution books.

**One-sided books (`strategy/book_normalize.py`, `strategy/green_up_strategy.py`)**
- Hedge/stop paths synthesize a missing ask from the bid; entry still requires a real two-sided book.

**Auto take-profit (`risk/alert_actions.py`, `--auto-take-profit`)**
- Optional cross-spread YES sell when `PROFIT_TARGET` fires; bot-ownership scoped; skips green-up legs in `hedging` / `hedged` / `stopping`.

**Shutdown hygiene (`main.py`)**
- `try/finally` always closes shared aiohttp sessions and `ExecutionManager`; ingestor `TimeoutError` during Ctrl+C no longer leaves unclosed sessions.

Tests: `tests/test_game_close.py`, `tests/test_book_normalize.py`, updated `tests/test_price_targets.py`, `tests/test_green_up_execution.py`.

### Correctness gap found in this audit — FIXED (2026-05-29)

- **`PortfolioMonitor.session_realised_pnl_cents` was a placeholder.** `refresh()` previously computed realised P&L as `sum(int(f.get("is_taker", 0)) for f in fills)` — taker flags, **not** dollars — so "Session realised P&L" in the portfolio report / session table / dashboard was meaningless. **Fixed:** `realized_pnl_cents_from_fills()` now does weighted-average-cost matching per `(ticker, side)` over `/portfolio/fills`, booking `contracts_closed × (sell_price − avg_cost)` on each closing sell. Sells beyond the tracked long (opening buy predates the fills window) are ignored rather than assumed free, so the figure is conservative. Settlement payouts (hold-to-expiry) remain on the `SettlementWatcher` → blotter / equity-snapshot path and are intentionally excluded. Covered by `tests/test_portfolio_pnl.py` (10 cases). The circuit breaker was never affected (it keys off `portfolio_value_cents`).

### Trade lifecycle & blotter reconciliation — FIXED (2026-05-30/31)

Commits `d4dfa5f` (green_up + platform hygiene) and `22b2b49` (high_prob parity). Motivated by a **production green_up session** on `KXNBAGAME-26MAY30SASOKC-SAS` (entry 1 YES @ 61¢, stop @ 43¢) that exposed fill-label and blotter accounting bugs.

**Fill recognition (`green_up`, `high_prob`)**
- **Stop/exit fills are side-agnostic** — Kalshi may report contra-side labels (`side: "no"` on a YES stop sell); strategies match by `order_id` and apply the fill regardless.
- **Green-up reprice guard** — `MAX_STOP_CANCEL_FAILURES` caps cancel-404 loops when a stop already filled but the strategy missed the event.
- **High_prob** — `EXIT_PENDING` uses the same pattern via `_apply_exit_fill()`; `main.py` falls back to `close_trade()` if the blotter closed the entry leg but strategy state lagged.

**Blotter P&L (`main.py`, `metrics/blotter.py`)**
- Exit/stop fills call **`close_leg`** on the open **entry** leg (realised P&L), not `record_fill` for a phantom opposing leg.
- **Hedged parents** → `mark_trade_hedged()` → status **`hedged`** until `SettlementWatcher` settles both legs (no premature `close_trade` with `net_pnl=0`).
- **Stop path** → entry leg closed at stop price → parent **`closed`** with realised loss.

**Hygiene & ops**
- **`tests/conftest.py`** — pytest redirects `KALSHI_*_DB_PATH` and log paths to a temp sandbox (fixes test pollution of prod log/DB and phantom `RISK_BREACH` events).
- **`risk/alert_manager.py`** — skip `POSITION_STOP` / `PROFIT_TARGET` when blotter shows **zero bot-owned contracts** on that ticker.
- **`discovery/live_market.py`** — runtime `is_tick_live()` uses WS book/tape freshness only; REST `updated_time` recency is a **discovery** filter (fixes false “market not live” blocks on active in-play markets).
- **`scripts/cleanup_stale_trades.py`** — dry-run/apply reconcile for pre-fix stuck parents and phantom legs (used on prod DB 2026-05-31).
- **Prod log** — `kalshi_bot_prod.jsonl` archived (~66k lines) and reset for clean post-fix telemetry.

**Tests added:** green_up stop fill side mismatch, hedge `mark_trade_hedged`, high_prob contra-side exit, fill-reconciliation end-to-end, alert scoping, live-market runtime gate.

### Mean reversion strategy — ADDED (2026-06)

New **`mean_reversion`** strategy for round-trip volatility capture in oscillating markets:

- **`strategy/mean_reversion_strategy.py`** — rolling mid-price mean, long (buy YES dip) and short (buy NO spike) legs, resting TP toward mean/offset, stop on continued adverse move, volatility floor, `summary()` for monitor table.
- **Discovery preset** — mid-range YES (15–85¢), screener-ranked, high volume, recently updated (`discovery/discovery_presets.py`).
- **Screener** — `_score_mean_reversion()` rewards mid-range price, tight spread, and volume (`discovery/screener.py`).
- **Execution** — `resolve_no_sell` / `resolve_no_sell_exit` in `strategy/execution_price.py`; shares `book_normalize.py` one-sided exit handling with `green_up` / `high_prob`.
- **Blotter lifecycle** — `main.py` `on_fill_received` closes parents on `MRState.CLOSED` and exit fills (same pattern as `high_prob`).
- **CLI / env** — `--mr-*` flags and `KALSHI_MR_*` defaults in `config.py`; wired through `main.py`, `factory.py`, `position_limits.py`, `tools/replay.py`.
- **Tests** — `tests/test_mean_reversion_strategy.py` (long entry, short entry, resting TP, stop, one-sided book, cycle reset).

**Certification status:** implemented and unit-tested; **zero live demo sessions**. Demo runbook: [MR-A](#mean-reversion--demo-certification-runbook-phase-a). Prod script `scripts/run_mean_reversion_prod.ps1` not yet created.

**Pre-live gaps (non-blocking for demo, fix before unattended prod):**

| Gap | Impact | Fix |
|-----|--------|-----|
| No `run_mean_reversion_prod.ps1` | Manual prod commands error-prone | Copy `run_high_prob_prod.ps1` pattern with `KALSHI_MR_STAKE_CENTS=100` |
| `session_table.py` has no MR action hints | Monitor shows state but generic action column | Add `_mr_action_hint()` (TP/stop/mean distance) — optional for certification |
| No `test_fill_reconciliation` case for mean_reversion | Exit fill path less proven than `high_prob` | Add e2e blotter test mirroring `test_high_prob_exit_fill_closes_entry_leg_with_realised_pnl` |
| `testing.md` has no mean_reversion section | Runbook scattered | Add commands from MR-A runbook after first live session |

### Session P&L display reconciled with kill switch — FIXED (2026-05-30)

- **The live monitor's "Session" figure did not match the metric the kill switch trips on.** The session table / dashboard showed `session_realised_pnl_cents` (realised-only, from closing fills), while the circuit breaker's session-loss limit (check #2) trips on the change in **total mark-to-market equity** (`portfolio_value_cents − _session_start_equity`, which includes *unrealised* P&L). In a production run the monitor displayed `Session $0.00` the whole time while a held position bled unrealised P&L, then the breaker fired `session loss limit exceeded session_pnl=-522 limit=-500` — confusing because the two numbers measure different things. **Fixed:** `PortfolioMonitor` now tracks a session-start *equity* baseline and exposes `PortfolioSnapshot.session_total_pnl_cents` (= realised + unrealised since the monitor started). The live monitor's "Session" and the dashboard now display this total; the circuit breaker adopts the same baseline on first `sync_from_portfolio()` so its session-loss metric is **definitionally identical** to the displayed figure (single source of truth). Trip logic is unchanged. Covered by two new cases in `tests/test_circuit_breaker_sync.py` (baseline adoption + trips on unrealised-only session loss).

### Post-certification backlog — status

| Item | Status |
|------|--------|
| Fill funnel metrics (sent → exchange fill → WS confirm) | **Partial** — `MetricsStore.get_signal_fill_rate()` gives sent→filled; full funnel + dashboard still open |
| Blotter CLI `--resolution` / unified search | Deferred |
| Manual `trade.py` → blotter sync | Deferred (workaround exists) |
| Auto-flatten on shutdown (optional flag) | Deferred (by design — positions survive Ctrl+C) |
| Native green_up resting take-profit | Deferred — `--gu-hedge-style resting` covers the hedge leg; use **`mean_reversion`** or `high_prob` low-band for round-trip TP |
| Stale open-trade maintenance | **Done** — `scripts/cleanup_stale_trades.py` |
| Test/prod log+DB isolation in pytest | **Done** — `tests/conftest.py` |
| REST fill reconciliation (WS safety net) | **Done** — `trading/fill_reconciler.py` |

### Production readiness gaps (priority order, updated 2026-06-09)

**P0 — must fix before real money**

1. Complete Week 1–4 demo certification (README checklist all checked) — *still the primary blocker*
2. ~~Fix full `pytest` suite~~ — **DONE** (248/248 green)
3. ~~Trade lifecycle / blotter exit P&L~~ — **DONE** (2026-05-30/31; re-run prod micro-pilot to sign off)
4. Live circuit breaker Tests A + B on demo with session notes
5. Blotter ↔ exchange reconciliation on bot-driven orders for **each strategy you will run in prod** (`high_prob`, `green_up`, `mean_reversion` minimum)
6. **`mean_reversion` demo certification** — 3 signed sessions per [MR-A runbook](#mean-reversion--demo-certification-runbook-phase-a) before prod micro-pilot
7. Confirm `KALSHI_FEE_PER_CONTRACT_CENTS` matches your Kalshi fee tier (feeds high-prob ROI gate)

**P1 — before unattended / 24×7 prod**

8. Two-week demo soak without ERROR spam in `kalshi_bot_demo.jsonl`
9. ~~Fix `PortfolioMonitor` session-realised-P&L placeholder~~ — **DONE**
10. Kelly calibration ratio 0.85–1.10 (30+ settled trades) if running `kelly`
11. Process supervisor (systemd, Windows Service, or PM2) with auto-restart
12. **External** alerting sink for `risk_breach` / `kill switch` / WS disconnect / `AlertManager` CRITICAL
13. Prod dry-run logged: `KALSHI_ENV=production`, immediate Ctrl+C, verify prod DB/log paths
14. Tune resilience knobs and document values: `KALSHI_WS_MAX_SILENCE_SECONDS`, `KALSHI_WS_STALE_ESCALATE_POLLS`, `KALSHI_WS_BOOK_REST_FALLBACK_SECONDS`, `KALSHI_FILL_RECONCILE_SECONDS`
15. Create `scripts/run_mean_reversion_prod.ps1` (isolated prod DB/log like other prod scripts)
16. Add `test_fill_reconciliation` case for `mean_reversion` exit fills (parity with `high_prob`)

**P2 — operational maturity**

17. CI pipeline (`pytest` on push) — no `.github/workflows` today
18. Secrets outside flat `.env` for prod keys
19. SQLite backup or Postgres (`KALSHI_POSTGRES_URL`) for blotter + metrics durability
20. Runbook for orphan orders, partial arb legs, manual flatten, and forced-reconnect storms
21. Python 3.11+ on runtime host
22. `session_table.py` mean-reversion action hints (TP distance, rolling mean)

**P3 — nice to have**

23. Docker / container health checks
24. Full fill-funnel metrics and dashboard hardening (build on `MetricsStore`)
25. Manual trade → blotter sync for mixed manual/bot workflows

---

## Next steps — execute in order (2026-06-09)

Work top-to-bottom. Do not start Week 5 prod until every **Success criteria** row for Weeks 1–4 is checked.

### Block 1 — Week 1 platform sign-off (1–2 days)

| # | Step | How to execute | Success criteria |
|---|------|----------------|------------------|
| 1 | Tests green | `python -m pytest tests/ -v` | 248 passed, 0 failed |
| 2 | Config profile | `python -c "import config; print(config.ENV, config.MAX_POSITION_CENTS, config.DB_PATH)"` | `demo`, `10000`, `kalshi_bot_demo.db` (or your demo overrides) |
| 3 | 30 min soak | [Step 1.4](#step-14--run-bot-30-minutes-graceful-shutdown) with explicit tickers + `--strategy high_prob` | Process runs 30 min; Ctrl+C → exit 0; log has `shutdown`; `python tools/trade.py orders` empty |
| 4 | Circuit breaker A | [Test A](#test-a--percent-drawdown-max_drawdown_pct) — `KALSHI_DEMO_MAX_DRAWDOWN_PCT=0.01` | Log: `max drawdown exceeded`, `kill switch`; orders cancelled; restore `0.10` after |
| 5 | Circuit breaker B | [Test B](#test-b--session-loss-limit-daily_loss_limit_cents) — `KALSHI_DEMO_DAILY_LOSS_LIMIT_CENTS=500` | Log: `session loss limit exceeded`, `kill switch`; restore normal limit after |
| 6 | Manual sell | [Step 1.6](#step-16--manual-sell--flatten-test) | `trade.py sell` or `close` reduces position; portfolio matches |

**Block 1 done when:** README [Production checklist](../README.md#production-checklist) items for pytest, SIGINT shutdown, and circuit breaker are checked.

### Block 2 — Week 2 strategy certification (1–2 weeks)

Run **three signed sessions** per strategy using the [Session template](#session-template-copy-per-run). Each session must complete at least one bot-driven trade cycle (not discover-only).

| Strategy | Runbook | Minimum success per session |
|----------|---------|----------------------------|
| `high_prob` | [Week 2 steps 2.1–2.4](#week-2--certify-high_prob-and-green_up) | Entry fill → TP/stop/hold path documented; blotter legs match exchange |
| `green_up` | [Phase A](#phase-a--single-ticker-controlled-entry-certification) | Entry → hedge **or** stop; parent `hedged` or `closed` with correct P&L |
| `mean_reversion` | [MR-A runbook](#mean-reversion--demo-certification-runbook-phase-a) | Entry → exit/stop → parent `closed`; realised P&L on entry leg |

**Commands to start each strategy discovery:**

```bash
python main.py --discover --discover-category Politics --strategy high_prob --discover-only
python main.py --discover --discover-category Sports --strategy green_up --discover-only
python main.py --discover --discover-category Sports --strategy mean_reversion --discover-only
```

**Block 2 done when:** 9 session logs exist (3 × 3 strategies); no orphan orders after any Ctrl+C; grep `kalshi_bot_demo.jsonl` for session dates shows no unhandled `traceback`.

### Block 3 — Week 3 kelly + arb (3–5 days)

| # | Step | Command | Success criteria |
|---|------|---------|------------------|
| 1 | Kelly tickers | `python tools/screen.py screen --category Sports --top 5` | 2 liquid tickers with spread ≤ 5¢ |
| 2 | Kelly session ×2 | `python main.py --strategy kelly --tickers T1,T2 --model-prob T1:0.62,T2:0.55` | Orders only when edge ≥ gate; blotter entry legs ≤ `MAX_POSITION_CENTS` |
| 3 | Arb session ×2 | `python main.py --strategy arb --comp-pairs T1:T2` or category discover | Both legs fill **or** explicit skip logged; document zero-arb days as OK |

### Block 4 — Week 4 soak + prod prep (2 weeks)

| # | Step | Action | Success criteria |
|---|------|--------|------------------|
| 1 | Daily demo runs | 1–2 hr/day, rotate all 5 strategies | No ERROR/traceback spam in `kalshi_bot_demo.jsonl` |
| 2 | Weekly reports | `python tools/blotter_report.py performance --days 7` | Per-strategy P&L summary readable |
| 3 | Prod keys loaded | Prod keys in `.env`, `KALSHI_ENV=demo` | Keys present; demo still active |
| 4 | Prod dry-run | `KALSHI_ENV=production` → start bot → **immediate Ctrl+C** | Console: `PRODUCTION MODE`; prod DB/log paths; clean shutdown; revert to `demo` |

**Block 4 done when:** README checklist "Two+ weeks demo trading without unhandled exceptions" is checked.

### Block 5 — Production micro-pilot ($1 cap)

**Prerequisite:** Blocks 1–4 complete. Certify strategies **one at a time**; do not combine on first prod week.

| Order | Strategy | Script / command | Success criteria |
|-------|----------|------------------|------------------|
| 1 | `high_prob` | `.\scripts\run_high_prob_prod.ps1` | ≤ $1 stake; blotter + Kalshi UI match; daily loss &lt; $5 |
| 2 | `green_up` | `.\scripts\run_green_up_prod.ps1` | ≤ $1 entry leg; hedge/stop works; re-run after 2026-05-30 fixes |
| 3 | `mean_reversion` | Create then run `run_mean_reversion_prod.ps1` | ≤ $1 round-trip; TP/stop exit; parent `closed` with P&L |
| 4 | `kelly` / `arb` | Manual prod commands at 100¢ cap | Optional; only after 1–3 stable |

**Prod env (all micro-pilot runs):**

```env
KALSHI_ENV=production
KALSHI_PROD_MAX_POSITION_CENTS=100
KALSHI_PROD_MAX_CONCURRENT_POSITIONS=1
KALSHI_PROD_DAILY_LOSS_LIMIT_CENTS=500
```

**Block 5 done when:** One clean prod session per strategy with session template filled; no kill switch trips from config mistakes; post-session `blotter.py detail` reconciles with `trade.py portfolio`.

### Code tasks before unattended prod (can parallelize with Block 2)

| Task | Owner | Done when |
|------|-------|-----------|
| `scripts/run_mean_reversion_prod.ps1` | Dev | Mirrors `run_high_prob_prod.ps1`; isolated `kalshi_bot_prod_mean_rev.db` |
| `tests/test_fill_reconciliation.py` mean_reversion case | Dev | Exit fill closes entry leg with `realized_pnl_cents` |
| `testing.md` mean_reversion section | Dev | MR-A commands + first live session notes |
| `.github/workflows/ci.yml` | Dev | `pytest` on push; badge in README optional |
| External alert webhook | Dev/Ops | `risk_breach` and `kill switch` reach phone/Slack within 60s |

---

## Related docs

- [README.md](../README.md) — setup, strategies, troubleshooting, production checklist
- [testing.md](../testing.md) — command cookbook and session examples
- [.env.example](../.env.example) — full variable template with demo/prod blocks
