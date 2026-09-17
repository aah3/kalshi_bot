# AGENTS.md

## Cursor Cloud specific instructions

This is a **Python 3.12 command-line application** (a Kalshi prediction-market trading bot). There is no web server or GUI — everything runs via `main.py` and the `tools/*.py` scripts. Standard setup/run commands live in `README.md` ("Setup", "Run tests") and `docs/ROADMAP.md`.

### Environment
- Dependencies install into a virtualenv at `.venv/` (the startup update script creates it and runs `pip install -r requirements.txt`). Only `python3` exists on PATH (no `python`), so run tools with `.venv/bin/python ...` (e.g. `.venv/bin/python main.py --help`).
- No linter/type-checker is configured in this repo (no ruff/flake8/black/mypy config). "Lint" here effectively means the test suite plus module import checks.

### Running & testing without credentials
- Live trading, discovery, and the `tools/trade.py`/`tools/orderbook.py`/`tools/screen.py` network commands require Kalshi API keys (`KALSHI_DEMO_*` / `KALSHI_PROD_*` in `.env`). Without keys, `config` still imports fine and prints a `[CONFIG] credentials not loaded` warning — this is expected, not an error.
- To exercise the strategy engine end-to-end offline (no network, no keys), use the replay tool: `.venv/bin/python tools/replay.py replay --input <recording.jsonl> --strategy high_prob --hp-entry-mode cross_spread --speed 0`. A recording is JSONL with `{"ts_us", "raw"}` lines where `raw` is a WS `orderbook_snapshot`/`orderbook_delta` message. The `replay` subcommand has no `--tickers`/`--quiet` flags; `high_prob`/`mean_reversion` evaluate whatever ticker appears in the feed.
- Run tests with `.venv/bin/python -m pytest tests/ -q`. Tests are self-isolating (`tests/conftest.py` redirects the logger/DB to a temp sandbox so runs never touch `kalshi_bot_prod.*`).

### Known gotcha
- `tests/test_green_up_execution.py::test_entry_size_rounds_to_whole_contracts` passes in isolation but **fails during the full suite** due to a pre-existing test-ordering issue: `strategy.green_up_strategy` is not in conftest's `_CONFIG_CONSUMER_MODULES` rebind list, so it keeps a stale `config` reference after profile tests reload `config`. This is a test-isolation bug, not an environment problem.
