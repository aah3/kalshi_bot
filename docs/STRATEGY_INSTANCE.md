# StrategyInstance — continuous multi-strategy framework (Phase 1)

Phase 1 adds a declarative **StrategyInstance** so one worker can run all day
with mid-session rediscovery, without rewriting strategy/execution cores.

## What shipped

| Piece | Path |
|--------|------|
| Schema + YAML/JSON loader | `orchestration/strategy_instance.py` |
| Adapter → CLI args / criteria / risk | `orchestration/instance_adapter.py` |
| Universe refresh (add/drop watch) | `orchestration/universe_manager.py` |
| Example instances | `config/instances/*.demo.yaml` |
| CLI | `python main.py --instance PATH [--instance-id ID]` |

## Run (demo)

```bash
# Dry-run discovery only
python main.py --instance config/instances/gu_sports_underdog.demo.yaml --discover-only

# Trade with 15-minute rediscovery
python main.py --instance config/instances/gu_sports_underdog.demo.yaml

# Schedulable wrapper (demo env forced)
.\scripts\run_instance_demo_soak.ps1
.\scripts\run_instance_demo_soak.ps1 -DurationMinutes 90
```

Soak certification skills (Agent): `.cursor/skills/kalshi-demo-soak`,
`.cursor/skills/kalshi-prod-soak`. Log helper: `python scripts/check_soak_log.py <log.jsonl>`.

Requires `PyYAML` (`pip install -r requirements.txt`).

## Behavior notes

- **One process = one instance.** Multi-strategy = N processes (Phase 2 launcher).
- **`refresh_seconds: null`** → startup-only discover (legacy behavior).
- **Drop policy** never unsubscribes tickers with open strategy state, resting
  orders, or blotter open/hedged trades. Protected tickers may temporarily
  exceed `max_watchlist`.
- **Risk sleeve** may tighten env-profile ceilings, never raise them.
- **`instance_id`** is written into blotter `strategy_meta` and startup logs.
- **Lifecycle** `on_flat: keep_running` (default in examples) leaves the process
  up when flat so rediscovery can find new markets.
- **Shutdown** still cancels resting orders only (`on_shutdown: cancel_resting`).
- **`--max-runtime-minutes N`** (CLI or `runtime.max_runtime_minutes`) triggers that same graceful path after N minutes — use this on Windows instead of killing the process.

## Field map (quick)

See chat/plan notes for full CLI 1:1 maps. Core blocks:

- `universe` → discovery + refresh
- `strategy_params` → `build_strategy(...)` kwargs
- `risk` / `persistence` → config + DB/log isolation
- `lifecycle` / `runtime` → exit flags, monitor, quiet

## Kill-switch alerting & unattended autostart

| Piece | Path |
|--------|------|
| Out-of-band kill-switch sentinel (written by `main.kill_switch()`) | `risk/kill_switch_alert.py` |
| Operator watcher (sentinel + log tail; never `Stop-Process`, never flattens) | `scripts/watch_kill_switch.ps1` |
| Register unattended overnight autostart (Task Scheduler, at-logon) | `scripts/register_autostart.ps1` |
| Remove the autostart task | `scripts/unregister_autostart.ps1` |

- `risk/kill_switch_alert.py` writes a small, fsync'd JSON sentinel
  (`<db_stem>.kill_switch_alert.json`, next to the instance DB) the instant
  the circuit breaker's kill switch fires — independent of the buffered
  JSONL log handler, which can lose the `risk_breach` line if the process
  exits immediately after tripping.
- `scripts/watch_kill_switch.ps1` polls that sentinel (plus a log-tail
  fallback) and alerts the operator (console banner + beep). It is
  read-only with respect to the trading process: cancellation already
  happened inside `ExecutionManager.cancel_all_orders()` before the
  watcher ever sees the alert, and it never calls `Stop-Process` or
  flattens positions.
- **Only register autostart when the operator explicitly asks** for an
  unattended overnight soak, and only after an attended session has passed
  on the current code. `register_autostart.ps1` refuses to run without
  `-Confirm` for exactly this reason. Unregister with
  `unregister_autostart.ps1` when the soak is done or before shipping
  further changes.

## Phase 2 (not built)

- Portfolio supervisor launching N workers
- Shared portfolio risk gate
- Schedule windows enforcement
