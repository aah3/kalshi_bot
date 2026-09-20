---
name: kalshi-demo-soak
description: >-
  Run and certify Kalshi bot demo soaks (paper trading): StrategyInstance or
  CLI discover sessions, log/blotter health checks, graceful shutdown, and
  session template sign-off. Use when the user asks for a demo soak, paper
  soak, StrategyInstance soak, gu_sports_underdog soak, Week 4 soak, or to
  verify rediscovery / all-day green_up on KALSHI_ENV=demo.
disable-model-invocation: true
---

# Kalshi demo soak

Certify that a **demo** worker stays healthy for a timed soak, then shut down
cleanly and fill the session template.

## Hard rules

- `KALSHI_ENV` must be `demo` before any long run. Verify:
  `python -c "import config; print(config.ENV, config.MAX_POSITION_CENTS, config.DB_PATH, config.LOG_FILE)"`
- Abort if `ENV` is `production`. Hand off to `kalshi-prod-soak` instead.
- Do **not** flatten positions unless the user explicitly asks.
- Prefer StrategyInstance YAML when one exists for the sleeve under test.

## Default next-step soak (StrategyInstance)

Target: `config/instances/gu_sports_underdog.demo.yaml` for **60–120 minutes**.

### Preflight

```text
Soak Progress:
- [ ] Env is demo
- [ ] pytest green (or user waived)
- [ ] discover-only returns ≥1 ticker
- [ ] Instance log/DB paths noted
```

1. Confirm demo env (command above).
2. Optionally: `python -m pytest tests/ -q`
3. Discover preview (must print tickers before soak):

```bash
python main.py --instance config/instances/gu_sports_underdog.demo.yaml --discover-only
```

If zero tickers: relax filters via screen (`tools/screen.py`) or stop and report — do not start an empty soak.

### Start soak

```bash
python main.py --instance config/instances/gu_sports_underdog.demo.yaml
```

- Run in a dedicated terminal; leave it up for the agreed duration (default 60–120m).
- Note start time, PID/terminal, `instance_id`, log file, DB path from startup logs.

### During soak (every 15–30m or on `/loop`)

Use [scripts/check_soak_log.py](scripts/check_soak_log.py) or equivalent greps on the **instance** log file (e.g. `kalshi_bot_demo_gu_sports.jsonl`):

Pass signals:
- `StrategyInstance loaded` / `Kalshi trading bot started` with `instance_id`
- `Universe refresh loop started` when `refresh_seconds` is set
- At least one `Universe: added tickers` **or** `Universe: dropped tickers` after ≥1 refresh interval (900s for the default YAML)
- No runaway ERROR spam

Fail / investigate:
- `traceback`, `kill switch`, `risk_breach`
- Repeated `WebSocket silent` without recovery
- Auth failures

Also check exchange vs bot:
- `python tools/trade.py portfolio`
- `python tools/blotter.py trades --days 1` (against instance DB if overridden)

### End soak

1. Ctrl+C (SIGINT) — expect resting orders cancelled, clean `shutdown` log line.
2. `python tools/trade.py orders` — expect empty (or only non-bot leftovers explained).
3. Fill session template (see [reference.md](reference.md)).
4. Verdict: **PASS** / **PASS WITH NOTES** / **FAIL** with evidence (log snippets, trade IDs).

## Non-instance CLI soak

If user gives explicit CLI instead of `--instance`, follow the same preflight → run → log check → shutdown → template flow. Still require demo env.

## Output format

```markdown
## Demo soak report
- Instance / strategy:
- Duration:
- Env / DB / log:
- Discover tickers (start):
- Universe refresh observed: yes/no (evidence)
- Orders / fills / blotter IDs:
- Errors / kill switch:
- Shutdown: clean / unclean
- Verdict: PASS | PASS WITH NOTES | FAIL
- Action items:
```

## Related

- Prod micro-pilot: skill `kalshi-prod-soak`
- Schema: `docs/STRATEGY_INSTANCE.md`
- Roadmap soak item: StrategyInstance demo soak (`gu_sports_underdog`)
