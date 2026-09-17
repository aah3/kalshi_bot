# Production MVP — agent briefing

**This file is the source of truth for the current production MVP.** Ignore
`docs/ROADMAP.md` and `testing.md` for this MVP — they describe a broader,
multi-strategy roadmap that is explicitly out of scope here.

This file was reconstructed on 2026-09-17 from the operator's working notes
(`prod-mvp-next-steps.md` / `project-context.md`) after being found missing
from `origin/main` and the full git history. If a fuller version of this
document exists elsewhere, treat that one as authoritative and update this
copy to match.

## Mission (do not expand)

Run **one** `green_up` Sports sleeve in production at:

- **$1** max position size
- **1** concurrent position
- **$5** daily loss limit

Fixed to this scope until the operator explicitly asks otherwise:

- Instance config: `config/instances/gu_sports_underdog.prod.yaml`
- Runner: `.\scripts\run_instance_prod.ps1`
- Shutdown cancels resting orders only — it does **not** flatten open positions
- Do **not** add `kelly` / `arb` / `mean_reversion`, and do **not** raise size,
  until the operator asks

## Hard rules

- No live trading without explicit prod approval for that session
- Never point a demo YAML at production keys (`run_instance_prod.ps1` and
  `register_autostart.ps1` both refuse `*.demo.*` instance paths)
- Never flatten the operator's manual politics GTC positions — the bot only
  acts on its own blotter-tracked legs
- Never `Stop-Process` the bot on Windows — shutdown is cooperative
  (SIGINT / `--max-runtime-minutes`), which cancels resting orders only
- The circuit breaker's Session P&L is the **whole account** equity change,
  not just the bot's own legs (`PortfolioMonitor.session_total_pnl_cents`) —
  do not narrow this to bot-only P&L
- Do not re-register Task Scheduler autostart unless the operator explicitly
  asks for an unattended overnight soak

## Status (as of 2026-09-17)

- Three attended $1 sessions plus one ~4.5h always-on run completed; hedge
  proven (T-0002, T-0005), leftover-parent settlements work (T-0004)
- Kill-switch alerting (`risk/kill_switch_alert.py`,
  `scripts/watch_kill_switch.ps1`) and autostart tooling
  (`scripts/register_autostart.ps1` / `unregister_autostart.ps1`) landed via
  [PR #2](https://github.com/aah3/kalshi_bot/pull/2)
- Stop-fill blotter bug fixed in the same PR: a stop-loss close now rolls the
  parent trade up to status `stopped` instead of silently landing as
  `closed` — unit-tested, not yet validated in a live session
- Task Scheduler was registered, used, then unregistered (14 Sep) — nothing
  currently starts at logon

## Next steps (do not reorder without operator sign-off)

1. Attended 90-minute $1 session — first live validation of the stop-fill
   fix; requires explicit prod approval
2. Always-on overnight $1 soak — only if the operator asks for it; requires
   re-registering Task Scheduler autostart
3. Raise size — only after that soak, and only if the operator asks

See `/cursor/stores/bc-84d340da-a959-468a-93ab-e9d74ac961ee/docs/prod-mvp-next-steps.md`
in the project agent store for the fuller running log of what has already
happened per session.
