# Prod soak reference

## Session template

```text
Date:
KALSHI_ENV: production
Strategy:
Script / command:
Max position cents:
Duration:
Tickers:
Orders / fills:
Blotter trade IDs:
Portfolio after (tools/trade.py):
Issues:
Action items:
Verdict:
```

## Script isolation defaults

| Script | DB | Log |
|--------|----|-----|
| `run_green_up_prod.ps1` | `kalshi_bot_prod_green_up.db` | `kalshi_bot_prod_green_up.jsonl` |
| `run_high_prob_prod.ps1` | `kalshi_bot_prod_high_prob.db` | `kalshi_bot_prod_high_prob.jsonl` |

## Abort conditions (stop soak immediately)

- Kill switch / risk_breach
- Unexpected size > cap
- Credentials / auth failure
- Uncontrolled order spam / cancel failures stacking

Then: Ctrl+C, cancel resting via bot shutdown path, snapshot portfolio, notify user.
