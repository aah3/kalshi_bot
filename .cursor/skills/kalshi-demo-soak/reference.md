# Demo soak reference

## Session template

```text
Date:
KALSHI_ENV: demo
Strategy / instance_id:
Command:
Duration:
Tickers discovered:
Orders sent / fills (exchange):
Blotter trade IDs:
Open positions after (portfolio):
Universe refresh evidence:
Issues:
Action items:
Verdict:
```

## Log paths (defaults)

| Mode | Typical log | Typical DB |
|------|-------------|------------|
| Demo profile | `kalshi_bot_demo.jsonl` | `kalshi_bot_demo.db` |
| Instance `gu_sports_underdog` | `kalshi_bot_demo_gu_sports.jsonl` | `kalshi_bot_demo_gu_sports.db` |
| Instance `hp_sports_favorites` | `kalshi_bot_demo_hp_sports.jsonl` | `kalshi_bot_demo_hp_sports.db` |

Always prefer paths printed at startup (`StrategyInstance loaded` / config line).

## Grep / check helpers

```bash
python scripts/check_soak_log.py kalshi_bot_demo_gu_sports.jsonl
```

Manual patterns (PowerShell):

```powershell
Select-String -Path kalshi_bot_demo_gu_sports.jsonl -Pattern 'ERROR|traceback|risk_breach|kill switch|Universe:'
```

## Pass criteria (StrategyInstance 1–2h)

1. Process stayed up for agreed duration (or until intentional stop).
2. Clean SIGINT shutdown; resting orders cancelled.
3. No kill switch / risk_breach.
4. ERROR/traceback absent or explained as benign exchange noise.
5. If `refresh_seconds` > 0 and soak ≥ refresh interval: at least one universe add or drop log line **or** documented “no market churn” with refresh loop still running.
6. Session template filled.
