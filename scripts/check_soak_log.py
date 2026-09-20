#!/usr/bin/env python3
"""
scripts/check_soak_log.py — summarize a bot JSONL log for soak certification.

Usage:
  python scripts/check_soak_log.py kalshi_bot_demo_gu_sports.jsonl
  python scripts/check_soak_log.py path/to/log.jsonl --env-hint demo
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


INTERESTING = (
    "StrategyInstance loaded",
    "Kalshi trading bot started",
    "Universe refresh loop started",
    "Universe: added tickers",
    "Universe: dropped tickers",
    "kill switch",
    "risk_breach",
    "Shutdown complete",
    "graceful shutdown",
)

FAIL_SUBSTR = ("traceback", "kill switch", "risk_breach")


def _load_lines(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"msg": line, "level": "?", "raw": True})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarize Kalshi bot soak JSONL")
    ap.add_argument("log_file", type=Path)
    ap.add_argument("--env-hint", default="", help="demo|production (informational)")
    args = ap.parse_args()

    if not args.log_file.is_file():
        print(f"FAIL: log not found: {args.log_file}", file=sys.stderr)
        return 2

    rows = _load_lines(args.log_file)
    levels = Counter((r.get("level") or "?").upper() for r in rows)
    msgs = [str(r.get("msg") or "") for r in rows]

    hits = {key: 0 for key in INTERESTING}
    for msg in msgs:
        for key in INTERESTING:
            if key.lower() in msg.lower():
                hits[key] += 1

    fail_lines: list[str] = []
    for r in rows:
        blob = json.dumps(r, default=str).lower()
        if any(s in blob for s in FAIL_SUBSTR) or (r.get("level") or "").upper() in (
            "ERROR",
            "CRITICAL",
        ):
            fail_lines.append(
                f"  [{r.get('level')}] {r.get('msg') or r.get('event') or ''}"[:200]
            )

    print(f"log: {args.log_file}  lines={len(rows)}  env_hint={args.env_hint or '-'}")
    print(f"levels: {dict(levels)}")
    print("signals:")
    for key, n in hits.items():
        if n:
            print(f"  {n:4d}  {key}")

    universe_ok = (
        hits["Universe: added tickers"] + hits["Universe: dropped tickers"] > 0
    )
    started = hits["Kalshi trading bot started"] + hits["StrategyInstance loaded"] > 0
    shutdown = hits["Shutdown complete"] + hits["graceful shutdown"] > 0

    print("checks:")
    print(f"  started:           {'yes' if started else 'no'}")
    print(f"  universe churn:    {'yes' if universe_ok else 'no (yet or none)'}")
    print(f"  shutdown seen:     {'yes' if shutdown else 'no (still running?)'}")
    print(f"  error-ish lines:   {len(fail_lines)}")

    if fail_lines:
        print("recent error-ish (max 15):")
        for line in fail_lines[-15:]:
            print(line)

    # Non-zero exit only if clear kill switch / risk_breach present
    hard = hits["kill switch"] + hits["risk_breach"]
    if hard:
        print("VERDICT: FAIL (kill switch / risk_breach)")
        return 1
    if not started and rows:
        print("VERDICT: INCONCLUSIVE (no startup markers)")
        return 0
    print("VERDICT: OK_SO_FAR" if not shutdown else "VERDICT: COMPLETE_CLEAN_OR_CHECK_ERRORS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
