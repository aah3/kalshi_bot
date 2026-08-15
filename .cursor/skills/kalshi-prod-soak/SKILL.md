---
name: kalshi-prod-soak
description: >-
  Run and certify Kalshi bot production micro-pilot soaks at the $1 cap:
  confirm YES gate, isolated prod DB/log, limited duration, blotter vs
  portfolio reconciliation, and session template sign-off. Use when the user
  asks for a production soak, prod micro-pilot, run_green_up_prod,
  run_high_prob_prod, real-money soak, or KALSHI_ENV=production certification.
disable-model-invocation: true
---

# Kalshi production soak

Certify a **short, capped** real-money session. Default stake ceiling:
`KALSHI_PROD_MAX_POSITION_CENTS=100` ($1).

## Hard rules

- Refuse to start if demo certification for that strategy is unsigned / user has
  not explicitly approved prod.
- Verify env **before** start:
  `python -c "import config; print(config.ENV, config.MAX_POSITION_CENTS, config.DB_PATH)"`
  Expect `production` and `100` (or tighter).
- Require typed **YES** confirmation (scripts) unless user set SkipConfirm and
  acknowledges risk in-chat.
- Prefer official scripts: `scripts/run_green_up_prod.ps1`,
  `scripts/run_high_prob_prod.ps1`.
- Do **not** flatten on shutdown unless user explicitly asks.
- Keep soaks short (suggest 30–90 minutes) until multi-hour prod is approved.
- Never raise position caps above the env profile ceiling.

## Preflight checklist

```text
Prod Soak Progress:
- [ ] User explicitly requested production
- [ ] ENV=production, max_position=$1 (or stated cap)
- [ ] Isolated DB/log (script defaults)
- [ ] discover-only or -Ticker preview OK
- [ ] MaxConcurrent understood (usually 1)
- [ ] Confirm YES gate plan
```

1. Print risk profile (command above).
2. Discover-only or single-ticker dry look when possible.
3. Remind user: real money; Ctrl+C cancels resting only.

## Start (examples)

```powershell
.\scripts\run_green_up_prod.ps1
.\scripts\run_green_up_prod.ps1 -DiscoverOnly
.\scripts\run_high_prob_prod.ps1
```

If using `--instance` in prod later: require a prod-dedicated YAML with
`max_position_cents: 100` and isolated `persistence` paths — do not reuse demo
instance files against production keys.

## During soak

On the **prod** log file from the script (e.g. `kalshi_bot_prod_green_up.jsonl`):

```bash
python scripts/check_soak_log.py kalshi_bot_prod_green_up.jsonl --env-hint production
```

Watch for: `kill switch`, `risk_breach`, auth errors, duplicate-order warnings,
unrecovered WS silence.

Reconcile:
- `python tools/trade.py portfolio`
- Blotter against the **prod script DB** path

## End soak

1. Ctrl+C → cancel resting; confirm clean shutdown log.
2. `python tools/trade.py orders` / portfolio snapshot.
3. `python tools/blotter.py trades --days 1` (correct DB).
4. Fill session template with `KALSHI_ENV: production`.
5. Verdict + whether another strategy may proceed.

## Output format

```markdown
## Prod soak report
- Script / strategy:
- Cap ($):
- Duration:
- DB / log:
- Fills / blotter IDs:
- Portfolio after:
- Kill switch / errors:
- Shutdown:
- Verdict: PASS | PASS WITH NOTES | FAIL
- Action items:
```

## Related

- Demo / StrategyInstance soaks: skill `kalshi-demo-soak`
- Prod scripts: `scripts/run_*_prod.ps1`
- Roadmap Week 5 micro-pilot
