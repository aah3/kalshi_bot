"""
tools/replay.py — WebSocket recorder and offline strategy replayer

Two modes:

  RECORD  — connects to Kalshi WebSocket and saves all messages to a .jsonl
            file for later replay. Use this during a live/demo session to
            capture real market data.

  REPLAY  — reads a recorded .jsonl file and feeds messages through the full
            strategy stack (ingestor → strategy → circuit breaker) at
            configurable speed, without any network connection or order
            submission. Fills are simulated based on limit price vs book.

────────────────────────────────────────────────────────────────────────────────
WHY THIS MATTERS
────────────────────────────────────────────────────────────────────────────────

Without a replayer you can only test strategies against live markets, which
means waiting for real conditions. With replay you can:

  - Run the Kelly, GreenUp, and Arbitrage strategies against 2 hours of
    real book data in under 60 seconds (at 100× speed)
  - Iterate on strategy parameters (KELLY_DIVISOR, entry_max_price, etc.)
    and see immediately how trades would have changed
  - Reproduce edge cases (fast-moving books, arb opportunities, near-expiry)
    deterministically
  - Run the test suite against recorded data instead of synthetic ticks

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

  # Record 30 minutes of live WebSocket data for PRES-2024-DEM
  python tools/replay.py record \
      --tickers PRES-2024-DEM,INXD-23DEC29-B4700 \
      --output data/session_2024-11-15.jsonl \
      --duration 1800

  # Replay at 50× speed, showing all signals generated
  python tools/replay.py replay \
      --input data/session_2024-11-15.jsonl \
      --speed 50 \
      --strategy kelly \
      --model-prob PRES-2024-DEM:0.62

  # Replay green-up strategy
  python tools/replay.py replay \
      --input data/session_2024-11-15.jsonl \
      --speed 100 \
      --strategy green_up \
      --entry-max 30 \
      --hedge-trigger 70

  # Replay arb detection only
  python tools/replay.py replay \
      --input data/session_2024-11-15.jsonl \
      --strategy arb \
      --speed 0   # 0 = as fast as possible

  # Show recording stats without replaying
  python tools/replay.py info --input data/session_2024-11-15.jsonl
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


# ── Recorder ──────────────────────────────────────────────────────────────────

async def record(
    tickers:      list[str],
    output_path:  str,
    duration_s:   int,
    credentials=None,
) -> None:
    """
    Connect to the Kalshi WebSocket and record all messages to a .jsonl file.

    Each line in the output is a JSON object with:
        {
            "ts_us":    <unix microseconds>,
            "raw":      <original WS message string>
        }

    Args:
        tickers:     Markets to subscribe to.
        output_path: File path for the .jsonl recording.
        duration_s:  How many seconds to record (0 = until Ctrl-C).
        credentials: CredentialManager instance (None = unauthenticated).
    """
    import websockets
    from websockets.exceptions import ConnectionClosedError

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    print(f"\n[recorder] Connecting → {config.WS_URL}")
    print(f"[recorder] Tickers: {tickers}")
    print(f"[recorder] Output:  {output_path}")
    print(f"[recorder] Duration: {duration_s}s  (Ctrl-C to stop early)\n")

    msg_count   = 0
    start_time  = time.monotonic()
    end_time    = start_time + duration_s if duration_s > 0 else float("inf")

    extra_headers = {}
    if credentials:
        extra_headers = credentials.sign_request("GET", "/trade-api/ws/v2")

    try:
        async with websockets.connect(
            config.WS_URL,
            additional_headers=extra_headers,
            ping_interval=20,
        ) as ws:
            # Subscribe
            sub = {
                "id":     1,
                "cmd":    "subscribe",
                "params": {
                    "channels":       ["orderbook_delta", "orderbook_snapshot", "trade"],
                    "market_tickers": tickers,
                },
            }
            await ws.send(json.dumps(sub))
            print("[recorder] Subscribed. Recording...")

            with open(output_path, "w", encoding="utf-8") as f:
                async for raw in ws:
                    if time.monotonic() >= end_time:
                        break

                    record_entry = {
                        "ts_us": int(time.time() * 1_000_000),
                        "raw":   raw,
                    }
                    f.write(json.dumps(record_entry) + "\n")
                    msg_count += 1

                    if msg_count % 100 == 0:
                        elapsed = time.monotonic() - start_time
                        print(f"[recorder] {msg_count} messages  {elapsed:.0f}s elapsed")

    except (ConnectionClosedError, KeyboardInterrupt):
        pass

    elapsed = time.monotonic() - start_time
    print(f"\n[recorder] Done. {msg_count} messages recorded in {elapsed:.1f}s → {output_path}\n")


# ── Replay engine ─────────────────────────────────────────────────────────────

class ReplayEngine:
    """
    Feeds recorded WebSocket messages through the strategy stack offline.

    Simulates fills: if a signal's limit_price >= best_ask (for YES) or
    <= best_bid complement (for NO), the order is considered filled immediately.
    This models aggressive/crossing limit orders realistically.
    """

    def __init__(
        self,
        input_path:    str,
        speed:         float,
        strategy_name: str,
        strategy_kwargs: dict,
    ) -> None:
        self._path     = input_path
        self._speed    = speed           # 0 = max speed, N = N× real time
        self._strat_name = strategy_name
        self._strat_kwargs = strategy_kwargs

        # Counters
        self.signals_generated = 0
        self.signals_blocked   = 0
        self.simulated_fills   = 0
        self.total_pnl_cents   = 0

        # Per-ticker book state for fill simulation
        self._books: dict[str, dict] = defaultdict(dict)

        # Active positions: ticker -> {side, entry_price, contracts}
        self._positions: dict[str, dict] = {}

        self._strategy = None
        self._circuit_breaker = None

    def _build_strategy(self):
        """Instantiate the selected strategy from kwargs."""
        name = self._strat_name

        if name == "kelly":
            from strategy.kelly_strategy import KellyStrategy
            strat = KellyStrategy()
            # Set model probabilities from --model-prob args
            for ticker, prob in self._strat_kwargs.get("model_probs", {}).items():
                strat.set_model_probability(ticker, float(prob))
            return strat

        elif name == "green_up":
            from strategy.green_up_strategy import GreenUpStrategy, HedgeMode
            mode_map = {
                "full_green": HedgeMode.FULL_GREEN,
                "stake_back": HedgeMode.STAKE_BACK,
                "partial":    HedgeMode.PARTIAL,
            }
            mode = mode_map.get(self._strat_kwargs.get("hedge_mode", "full_green"), HedgeMode.FULL_GREEN)
            strat = GreenUpStrategy(
                entry_max_price=self._strat_kwargs.get("entry_max", 25),
                hedge_trigger_price=self._strat_kwargs.get("hedge_trigger", 68),
                hedge_mode=mode,
                stop_loss_threshold=self._strat_kwargs.get("stop_loss", 0.40),
            )
            # Register tickers for watching
            for ticker in self._strat_kwargs.get("tickers", []):
                strat.add_watch_ticker(ticker)
            return strat

        elif name == "arb":
            from strategy.arbitrage_strategy import ArbitrageStrategy
            strat = ArbitrageStrategy()
            # Register complementary pairs if provided
            pairs = self._strat_kwargs.get("comp_pairs", [])
            for pair in pairs:
                parts = pair.split(":")
                if len(parts) == 2:
                    strat.register_complementary(parts[0], parts[1])
            return strat

        elif name == "high_prob":
            from strategy.factory import build_strategy
            tickers = self._strat_kwargs.get("tickers", [])
            model_probs = self._strat_kwargs.get("model_probs", {})
            return build_strategy(
                "high_prob",
                tickers,
                model_probs=model_probs,
                hp_min_yes_ask=self._strat_kwargs.get("hp_min_yes_ask"),
                hp_max_yes_ask=self._strat_kwargs.get("hp_max_yes_ask"),
                hp_entry_mode=self._strat_kwargs.get("hp_entry_mode"),
                hp_post_fill=self._strat_kwargs.get("hp_post_fill"),
                hp_stake_cents=self._strat_kwargs.get("hp_stake_cents"),
            )

        else:
            raise ValueError(f"Unknown strategy: {name}. Choose: kelly, green_up, arb, high_prob")

    def _build_circuit_breaker(self):
        """Dummy async kill switch for replay (never actually kills anything)."""
        from risk.circuit_breaker import CircuitBreaker

        async def noop_kill():
            pass

        return CircuitBreaker(kill_switch=noop_kill)

    async def run(self) -> dict[str, Any]:
        """
        Run the full replay. Returns a summary dict on completion.
        """
        if not os.path.exists(self._path):
            print(f"[replay] ERROR: input file not found: {self._path}")
            return {}

        self._strategy        = self._build_strategy()
        self._circuit_breaker = self._build_circuit_breaker()

        # Count total messages for progress display
        with open(self._path, "r") as f:
            total_msgs = sum(1 for _ in f)

        print(f"\n[replay] Input:    {self._path}  ({total_msgs} messages)")
        print(f"[replay] Strategy: {self._strat_name}")
        print(f"[replay] Speed:    {'max' if self._speed == 0 else f'{self._speed}×'}\n")

        prev_ts_us:  int | None = None
        processed    = 0
        start_wall   = time.monotonic()
        signal_log   = []

        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    entry  = json.loads(line)
                    ts_us  = entry["ts_us"]
                    raw    = entry["raw"]
                except (json.JSONDecodeError, KeyError):
                    continue

                # Speed-controlled sleep between messages
                if self._speed > 0 and prev_ts_us is not None:
                    real_gap_us  = ts_us - prev_ts_us
                    sleep_us     = real_gap_us / self._speed
                    if sleep_us > 0:
                        await asyncio.sleep(sleep_us / 1_000_000)
                prev_ts_us = ts_us

                # Parse and apply message to local book
                tick = self._apply_message(raw)
                if tick:
                    sig = await self._evaluate_tick(tick, ts_us)
                    if sig:
                        signal_log.append(sig)

                processed += 1
                if processed % 500 == 0:
                    pct = processed / total_msgs * 100
                    print(f"[replay] {processed}/{total_msgs}  ({pct:.0f}%)  "
                          f"signals={self.signals_generated}  fills={self.simulated_fills}  "
                          f"pnl=${self.total_pnl_cents/100:+.2f}")

        elapsed = time.monotonic() - start_wall
        return self._build_summary(signal_log, elapsed, total_msgs)

    def _apply_message(self, raw: str) -> dict | None:
        """
        Parse one WebSocket message and update local book state.
        Returns a normalised tick dict if the book changed, else None.
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None

        msg_type = msg.get("type") or msg.get("msg")
        data      = msg.get("msg", msg)

        if msg_type == "orderbook_snapshot":
            return self._apply_snapshot(data)
        elif msg_type == "orderbook_delta":
            return self._apply_delta(data)
        elif msg_type == "trade":
            ticker = data.get("market_ticker", "")
            book   = self._books.get(ticker)
            if book:
                book["last_trade_price"] = data.get("yes_price")
                return self._book_to_tick(ticker, book)
        return None

    def _apply_snapshot(self, data: dict) -> dict | None:
        ticker = data.get("market_ticker", "")
        if not ticker:
            return None

        book = self._books[ticker]
        book["yes_bids"] = {int(l["price"]): int(l["quantity"])
                            for l in data.get("yes", {}).get("bids", [])}
        book["yes_asks"] = {int(l["price"]): int(l["quantity"])
                            for l in data.get("yes", {}).get("asks", [])}
        book["ticker"]   = ticker
        return self._book_to_tick(ticker, book)

    def _apply_delta(self, data: dict) -> dict | None:
        ticker = data.get("market_ticker", "")
        if not ticker:
            return None

        book = self._books[ticker]
        for delta in data.get("deltas", []):
            side  = delta["side"]
            price = int(delta["price"])
            qty   = int(delta["delta"])
            target = book.setdefault(
                "yes_bids" if side == "yes_bid" else "yes_asks", {}
            )
            new_qty = target.get(price, 0) + qty
            if new_qty <= 0:
                target.pop(price, None)
            else:
                target[price] = new_qty
        return self._book_to_tick(ticker, book)

    def _book_to_tick(self, ticker: str, book: dict) -> dict | None:
        bids = book.get("yes_bids", {})
        asks = book.get("yes_asks", {})
        if not bids and not asks:
            return None

        best_bid = max(bids) if bids else None
        best_ask = min(asks) if asks else None
        spread   = (best_ask - best_bid) if best_bid and best_ask else None
        mid      = (best_bid + best_ask) / 2.0 if best_bid and best_ask else None

        return {
            "ticker":        ticker,
            "best_bid":      best_bid,
            "best_ask":      best_ask,
            "spread":        spread,
            "mid_price":     mid,
            "event_type":    "delta",
            "updated_at_us": int(time.time() * 1_000_000),
        }

    async def _evaluate_tick(self, tick: dict, ts_us: int) -> dict | None:
        """Run tick through strategy and circuit breaker. Simulate fill."""
        sig = self._strategy.evaluate(tick)
        if sig is None:
            return None

        self.signals_generated += 1

        if not self._circuit_breaker.approve(sig):
            self.signals_blocked += 1
            return None

        # Simulate fill
        ticker    = sig.ticker
        book      = self._books.get(ticker, {})
        best_ask  = min(book.get("yes_asks", {1000: 1}).keys(), default=None)
        best_bid  = max(book.get("yes_bids", {0: 1}).keys(), default=0)

        filled = False
        fill_price = sig.limit_price or 0

        if sig.side.value == "yes" and best_ask and sig.limit_price >= best_ask:
            filled     = True
            fill_price = best_ask
        elif sig.side.value == "no":
            no_ask = 100 - best_bid
            if sig.limit_price >= no_ask:
                filled     = True
                fill_price = no_ask

        fill_record = {
            "ts_us":       ts_us,
            "ts_readable": datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc).isoformat(),
            "ticker":      ticker,
            "side":        sig.side.value,
            "limit_price": sig.limit_price,
            "fill_price":  fill_price,
            "filled":      filled,
            "size_cents":  sig.size_cents,
            "edge":        sig.edge,
            "edge_to_vig": sig.edge_to_vig,
            "strategy":    sig.strategy,
            "phase":       (sig.meta or {}).get("phase", "entry"),
        }

        if filled:
            self.simulated_fills += 1
            # Record position for P&L tracking
            contracts   = max(sig.size_cents // max(fill_price, 1), 1)
            phase       = (sig.meta or {}).get("phase", "entry")
            if phase == "entry" and ticker not in self._positions:
                self._positions[ticker] = {
                    "side":         sig.side.value,
                    "entry_price":  fill_price,
                    "contracts":    contracts,
                    "strategy":     sig.strategy,
                }
            elif phase in ("hedge", "stop_loss") and ticker in self._positions:
                # Close position — compute P&L
                pos    = self._positions.pop(ticker)
                if pos["side"] == "yes":
                    pnl = (fill_price - pos["entry_price"]) * pos["contracts"]
                else:
                    pnl = (pos["entry_price"] - fill_price) * pos["contracts"]
                pnl -= int(config.FEE_PER_CONTRACT_CENTS * (pos["contracts"] + contracts))
                self.total_pnl_cents += pnl
                fill_record["simulated_pnl_cents"] = pnl
                fill_record["simulated_pnl_usd"]   = round(pnl / 100, 2)

            self._circuit_breaker.record_fill({
                "ticker":     ticker,
                "side":       sig.side.value,
                "price":      fill_price,
                "size_cents": sig.size_cents,
                "sector":     (sig.meta or {}).get("category", "unknown"),
            })
            self._strategy.on_fill({
                "ticker":     ticker,
                "side":       sig.side.value,
                "price":      fill_price,
                "size_cents": sig.size_cents,
                "order_id":   f"sim_{ts_us}",
            })

        return fill_record

    def _build_summary(
        self,
        signal_log: list[dict],
        elapsed:    float,
        total_msgs: int,
    ) -> dict[str, Any]:
        fills    = [s for s in signal_log if s.get("filled")]
        no_fills = [s for s in signal_log if not s.get("filled")]
        fill_rate = len(fills) / len(signal_log) * 100 if signal_log else 0.0

        by_phase: dict[str, int] = defaultdict(int)
        for s in signal_log:
            by_phase[s.get("phase", "entry")] += 1

        summary = {
            "input_file":        self._path,
            "strategy":          self._strat_name,
            "messages_replayed": total_msgs,
            "elapsed_seconds":   round(elapsed, 2),
            "speed_factor":      self._speed,
            "signals_generated": self.signals_generated,
            "signals_blocked":   self.signals_blocked,
            "simulated_fills":   self.simulated_fills,
            "fill_rate_pct":     round(fill_rate, 1),
            "signals_by_phase":  dict(by_phase),
            "total_pnl_cents":   self.total_pnl_cents,
            "total_pnl_usd":     round(self.total_pnl_cents / 100, 2),
            "signal_log":        signal_log,
        }

        print(f"\n{'═' * 65}")
        print(f"  REPLAY COMPLETE — {self._strat_name.upper()}")
        print(f"{'═' * 65}")
        print(f"  Messages replayed:   {total_msgs}")
        print(f"  Elapsed:             {elapsed:.1f}s")
        print(f"  Signals generated:   {self.signals_generated}")
        print(f"  Signals blocked:     {self.signals_blocked}")
        print(f"  Simulated fills:     {self.simulated_fills}  ({fill_rate:.1f}% fill rate)")
        print(f"  Simulated net P&L:   ${self.total_pnl_cents/100:+.2f}")
        if by_phase:
            print(f"  Signals by phase:    {dict(by_phase)}")
        if fills:
            print(f"\n  FILLED SIGNALS (last 10)")
            for s in fills[-10:]:
                phase  = s.get("phase","entry")
                pnl    = s.get("simulated_pnl_usd")
                pnl_s  = f"  pnl=${pnl:+.2f}" if pnl is not None else ""
                print(f"  ✓ {s['ts_readable'][:19]}  {s['ticker']:<30}  "
                      f"{s['side'].upper():<4}  {s['fill_price']}c  {phase}{pnl_s}")
        if no_fills:
            print(f"\n  MISSED FILLS (last 5 — price too passive)")
            for s in no_fills[-5:]:
                print(f"  ✗ {s['ts_readable'][:19]}  {s['ticker']:<30}  "
                      f"{s['side'].upper():<4}  limit={s['limit_price']}c")
        print(f"{'═' * 65}\n")

        return summary


# ── Info command ──────────────────────────────────────────────────────────────

def show_info(input_path: str) -> None:
    """Show statistics about a recording file without replaying."""
    if not os.path.exists(input_path):
        print(f"File not found: {input_path}")
        return

    msg_counts: dict[str, int] = defaultdict(int)
    tickers:    set[str]       = set()
    ts_first = ts_last = None
    total = 0

    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts    = entry.get("ts_us", 0)
                raw   = json.loads(entry.get("raw", "{}"))
                total += 1
                if ts_first is None:
                    ts_first = ts
                ts_last = ts
                msg_type = raw.get("type") or raw.get("msg", "")
                msg_counts[msg_type] += 1
                ticker = (raw.get("msg") or raw).get("market_ticker", "")
                if ticker:
                    tickers.add(ticker)
            except Exception:
                continue

    if not total:
        print("Empty or unreadable file.")
        return

    duration_s = (ts_last - ts_first) / 1_000_000 if ts_first and ts_last else 0
    start_dt   = datetime.fromtimestamp(ts_first / 1_000_000, tz=timezone.utc) if ts_first else None
    end_dt     = datetime.fromtimestamp(ts_last / 1_000_000, tz=timezone.utc) if ts_last else None

    print(f"\n  RECORDING INFO — {input_path}")
    print(f"  {'Total messages:':<30} {total:,}")
    print(f"  {'Duration:':<30} {duration_s:.0f}s  ({duration_s/60:.1f} min)")
    print(f"  {'Start:':<30} {start_dt.isoformat()[:19] if start_dt else '?'} UTC")
    print(f"  {'End:':<30} {end_dt.isoformat()[:19] if end_dt else '?'} UTC")
    print(f"  {'Tickers:':<30} {', '.join(sorted(tickers))}")
    print(f"  {'Message types:'}")
    for mtype, count in sorted(msg_counts.items(), key=lambda x: -x[1]):
        print(f"    {mtype or '(unknown)':<28} {count:,}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

async def _run_replay(args) -> None:
    model_probs = {}
    for mp in (args.model_prob or []):
        parts = mp.split(":")
        if len(parts) == 2:
            model_probs[parts[0]] = float(parts[1])

    tickers = list(model_probs.keys()) or (
        [args.tickers] if hasattr(args, "tickers") and args.tickers else []
    )

    engine = ReplayEngine(
        input_path=args.input,
        speed=args.speed,
        strategy_name=args.strategy,
        strategy_kwargs={
            "model_probs":   model_probs,
            "entry_max":     getattr(args, "entry_max", 25),
            "hedge_trigger": getattr(args, "hedge_trigger", 68),
            "hedge_mode":    getattr(args, "hedge_mode", "full_green"),
            "stop_loss":     getattr(args, "stop_loss", 0.40),
            "tickers":       tickers,
            "comp_pairs":    getattr(args, "comp_pairs", []) or [],
        },
    )
    summary = await engine.run()

    if getattr(args, "json", None) and summary:
        os.makedirs(os.path.dirname(args.json) if os.path.dirname(args.json) else ".", exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"  Summary saved → {args.json}\n")


async def _run_record(args) -> None:
    credentials = None
    if os.getenv("KALSHI_API_KEY_ID"):
        from credentials.credential_manager import CredentialManager
        credentials = CredentialManager()

    tickers = args.tickers.split(",")
    await record(
        tickers=tickers,
        output_path=args.output,
        duration_s=args.duration,
        credentials=credentials,
    )


parser = argparse.ArgumentParser(
    description="Kalshi WebSocket recorder and strategy replayer",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
sub = parser.add_subparsers(dest="cmd", required=True)

# record
p_rec = sub.add_parser("record", help="Record live WS messages to file")
p_rec.add_argument("--tickers",  required=True, help="Comma-separated tickers")
p_rec.add_argument("--output",   required=True, help="Output .jsonl file path")
p_rec.add_argument("--duration", type=int, default=0,
                   help="Seconds to record (0=until Ctrl-C)")

# replay
p_rep = sub.add_parser("replay", help="Replay recorded file through strategy stack")
p_rep.add_argument("--input",    required=True, help="Input .jsonl file path")
p_rep.add_argument("--strategy", required=True,
                   choices=["kelly", "green_up", "arb", "high_prob"])
p_rep.add_argument("--speed",    type=float, default=50.0,
                   help="Playback speed multiplier (0=max, default 50×)")
p_rep.add_argument("--model-prob", nargs="*", metavar="TICKER:PROB",
                   help="Model probabilities for Kelly e.g. PRES-2024-DEM:0.62")
p_rep.add_argument(
    "--entry-max", "--entry_max", "--entry-max-price", "--entry_max_price",
    type=int, default=25, dest="entry_max",
)
p_rep.add_argument(
    "--hedge-trigger", "--hedge_trigger",
    type=int, default=68, dest="hedge_trigger",
)
p_rep.add_argument("--hedge-mode",    default="full_green",
                   choices=["full_green","stake_back","partial"])
p_rep.add_argument("--stop-loss",     type=float, default=0.40)
p_rep.add_argument("--comp-pairs",    nargs="*",  metavar="T1:T2",
                   help="Complementary arb pairs e.g. PRES-DEM:PRES-REP")
p_rep.add_argument("--json",          default=None, metavar="FILE.json")

# info
p_inf = sub.add_parser("info", help="Show recording file statistics")
p_inf.add_argument("--input", required=True)


if __name__ == "__main__":
    args = parser.parse_args()
    print(f"[replay] ENV={config.ENV}")
    if args.cmd == "record":
        asyncio.run(_run_record(args))
    elif args.cmd == "replay":
        asyncio.run(_run_replay(args))
    elif args.cmd == "info":
        show_info(args.input)
