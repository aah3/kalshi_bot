"""
tools/dashboard.py — live terminal dashboard

Combines every major data source into a single refreshing terminal view:

  ┌─────────────────────────────────────────────────────────────────────┐
  │  KALSHI BOT DASHBOARD  2024-11-15 14:32:07 UTC  ENV=demo            │
  ├──────────────┬──────────────────────────────────────────────────────┤
  │  ACCOUNT     │  LIVE POSITIONS (mark-to-market)                     │
  │  PERFORMANCE │  ALERTS                                              │
  │  CALIBRATION │  OPEN ORDERS                                         │
  └──────────────┴──────────────────────────────────────────────────────┘

Refreshes every REFRESH_INTERVAL_SECONDS (default 30s).
Press Ctrl-C to exit cleanly.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

  # Default: refresh every 30 seconds
  python tools/dashboard.py

  # Faster refresh
  python tools/dashboard.py --interval 10

  # Snapshot only (no loop)
  python tools/dashboard.py --once

  # Include Kelly calibration panel
  python tools/dashboard.py --calibration

  # Show last N days of performance stats
  python tools/dashboard.py --days 7
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from credentials.credential_manager import CredentialManager
from execution.rate_limiter import RateLimiter
from metrics.blotter import Blotter
from metrics.calculator import MetricsCalculator
from metrics.metrics_store import MetricsStore
from metrics.performance import PerformanceAnalytics
from risk.kelly_calibrator import KellyCalibrator
from trading.order_entry import OrderEntry
from trading.portfolio_monitor import PortfolioMonitor, PortfolioSnapshot


# ── Layout constants ──────────────────────────────────────────────────────────

W = 110   # total terminal width
REFRESH_INTERVAL_SECONDS = 30


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _clear() -> None:
    """Clear terminal screen."""
    print("\033[2J\033[H", end="", flush=True)


def _hr(char: str = "─", width: int = W) -> str:
    return char * width


def _pnl_color(val: float) -> str:
    """ANSI colour for a P&L value (green=positive, red=negative, white=zero)."""
    if val > 0:
        return f"\033[92m+${val:.2f}\033[0m"   # bright green
    elif val < 0:
        return f"\033[91m-${abs(val):.2f}\033[0m"  # bright red
    return f"${val:.2f}"


def _pct_bar(pct: float, width: int = 20) -> str:
    """Mini ASCII progress bar for percentages."""
    filled = int(min(max(pct, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _alert_icon(severity: str) -> str:
    return {"critical": "🔴", "warning": "🟡", "info": "🟢"}.get(severity, "⚪")


# ── Dashboard sections ────────────────────────────────────────────────────────

def _render_header(snapshot: PortfolioSnapshot | None) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    env = config.ENV.upper()
    env_str = f"\033[91m{env}\033[0m" if env == "PRODUCTION" else f"\033[93m{env}\033[0m"
    print(_hr("═"))
    print(f"  KALSHI BOT DASHBOARD  │  {now}  │  ENV={env_str}  │  DB={config.DB_PATH}")
    print(_hr("═"))


def _render_account(snapshot: PortfolioSnapshot | None) -> None:
    print(f"\n  {'─' * 40} ACCOUNT {'─' * (W - 49)}")

    if snapshot is None:
        print("  No portfolio data available.")
        return

    cash     = snapshot.cash_balance_cents / 100
    invested = snapshot.total_cost_basis_cents / 100
    value    = sum(p.current_value for p in snapshot.positions) / 100
    unreal   = snapshot.total_unrealised_pnl_cents / 100
    realised = snapshot.session_realised_pnl_cents / 100
    max_pay  = snapshot.total_max_payout_cents / 100

    pnl_pct = (unreal / invested * 100) if invested > 0 else 0.0

    print(
        f"  Cash: ${cash:>10.2f}   "
        f"Invested: ${invested:>10.2f}   "
        f"Open value: ${value:>10.2f}   "
        f"Unrealised P&L: {_pnl_color(unreal)} ({pnl_pct:+.1f}%)"
    )
    print(
        f"  Session realised: {_pnl_color(realised)}   "
        f"Max payout (all win): ${max_pay:.2f}   "
        f"Open positions: {snapshot.num_positions}"
    )


def _render_positions(snapshot: PortfolioSnapshot | None) -> None:
    print(f"\n  {'─' * 40} POSITIONS {'─' * (W - 51)}")

    if not snapshot or not snapshot.positions:
        print("  No open positions.")
        return

    header = (
        f"  {'TICKER':<30} {'SIDE':<5} {'QTY':>5}  "
        f"{'ENTRY':>6} {'MARK':>6}  "
        f"{'COST':>9} {'VALUE':>9}  "
        f"{'UNREAL P&L':>12}  {'P&L%':>6}  "
        f"{'IMPL%':>6}  {'EXP':>7}"
    )
    print(header)
    print(f"  {_hr('─', W - 2)}")

    for pos in sorted(snapshot.positions, key=lambda p: abs(p.unrealised_pnl), reverse=True):
        unreal_usd = pos.unrealised_pnl / 100
        exp_str    = f"{pos.minutes_to_close:.0f}m" if pos.minutes_to_close is not None else "—"

        # Colour-code expiry
        if pos.minutes_to_close is not None:
            if pos.minutes_to_close <= 10:
                exp_str = f"\033[91m{exp_str}\033[0m"
            elif pos.minutes_to_close <= 60:
                exp_str = f"\033[93m{exp_str}\033[0m"

        pnl_str = _pnl_color(unreal_usd)

        print(
            f"  {pos.ticker:<30} {pos.side:<5} {pos.contracts:>5}  "
            f"{pos.avg_entry_price:>5}c {(pos.mark_price or 0):>5}c  "
            f"${pos.cost_basis/100:>8.2f} ${pos.current_value/100:>8.2f}  "
            f"{pnl_str:>20}  {pos.pnl_pct:>+5.1f}%  "
            f"{pos.implied_prob*100:>5.1f}%  {exp_str:>7}"
        )


def _render_performance(perf: PerformanceAnalytics, days: int) -> None:
    print(f"\n  {'─' * 35} PERFORMANCE (last {days}d) {'─' * (W - 63)}")

    try:
        closed  = perf._blotter.query_trades(status="closed",  days=days)
        settled = perf._blotter.query_trades(status="settled", days=days)
        all_done = closed + settled

        if not all_done:
            print("  No closed trades in window.")
            return

        wr  = perf._win_rate_stats(all_done)
        pf  = perf.profit_factor(all_done)
        ex  = perf.expectancy(all_done)
        so  = perf.sortino_ratio(all_done)

        total_pnl = sum(t.net_pnl_cents or 0 for t in all_done) / 100
        n  = wr["total"]
        w  = wr["wins"]
        l  = wr["losses"]
        wr_pct = wr["win_rate_pct"]

        bar = _pct_bar(wr_pct, width=20)

        print(
            f"  Trades: {n}  ({w}W / {l}L)  "
            f"Win rate: {wr_pct:.1f}%  [{bar}]  "
            f"Net P&L: {_pnl_color(total_pnl)}"
        )
        print(
            f"  Profit factor: {pf:.3f if pf else '—':>6}  "
            f"Expectancy: {'$'+f'{ex:.4f}' if ex else '—':>10}/trade  "
            f"Sortino: {f'{so:.4f}' if so else '—':>8}"
        )

        # P&L by strategy — compact
        by_strat = perf._blotter.pnl_by_strategy(days=days)
        if by_strat:
            strat_parts = []
            for r in by_strat[:4]:   # show up to 4
                sign = "+" if r["total_pnl_usd"] >= 0 else ""
                strat_parts.append(
                    f"{r['group'][:16]}: {sign}${r['total_pnl_usd']:.2f} "
                    f"({r['win_rate_pct']:.0f}%W)"
                )
            print(f"  By strategy:  {'  │  '.join(strat_parts)}")

    except Exception as e:
        print(f"  Performance data unavailable: {e}")


def _render_metrics(store: MetricsStore, calc: MetricsCalculator) -> None:
    print(f"\n  {'─' * 40} KEY METRICS {'─' * (W - 53)}")
    try:
        m = calc.all_metrics()
        sharpe = m.get("sharpe_ratio")
        dd     = m.get("max_drawdown", {})
        fill   = m.get("fill_rate", {})
        etv    = m.get("avg_edge_to_vig")

        print(
            f"  Sharpe: {f'{sharpe:.4f}' if sharpe else '—':>8}  "
            f"Max drawdown: {dd.get('max_drawdown_pct', 0)*100:.2f}% "
            f"(${dd.get('max_drawdown_cents', 0)/100:.2f})  "
            f"Fill rate: {fill.get('fill_rate', 0)*100:.1f}% "
            f"({fill.get('filled', 0)}/{fill.get('total_signals', 0)})  "
            f"Avg edge-to-vig: {f'{etv:.3f}×' if etv else '—':>8}"
        )
    except Exception as e:
        print(f"  Metrics unavailable: {e}")


def _render_open_orders(order_entry: OrderEntry | None) -> None:
    print(f"\n  {'─' * 40} OPEN ORDERS {'─' * (W - 53)}")

    if order_entry is None:
        print("  Order entry not available.")
        return

    try:
        # We're in a sync context here — use cached state
        orders = getattr(order_entry, "_open_orders_cache", {})
        if not orders:
            print("  No resting orders.")
            return

        for oid, order in list(orders.items())[:8]:
            ticker  = order.get("ticker", "")
            status  = order.get("status", "")
            count   = order.get("count", 0)
            price   = order.get("yes_price", "?")
            print(f"  {oid[:10]}  {ticker:<32}  {status:<10}  "
                  f"qty={count}  price={price}c")
    except Exception:
        print("  Order data unavailable.")


def _render_alerts(alert_summary: list[dict]) -> None:
    print(f"\n  {'─' * 40} ALERTS {'─' * (W - 48)}")

    if not alert_summary:
        print("  \033[92mNo active alerts.\033[0m")
        return

    for a in alert_summary[:8]:
        icon     = _alert_icon(a.get("severity", "info"))
        ago      = int(a.get("fired_ago_seconds", 0))
        ago_str  = f"{ago//60}m{ago%60}s ago" if ago < 3600 else f"{ago//3600}h ago"
        msg      = a.get("message", a.get("alert_type", ""))[:70]
        ticker   = a.get("ticker") or "portfolio"
        print(f"  {icon}  [{ticker:<28}]  {msg:<70}  {ago_str}")


def _render_calibration(calibrator: KellyCalibrator, days: int) -> None:
    print(f"\n  {'─' * 35} KELLY CALIBRATION (last {days}d) {'─' * (W - 70)}")

    try:
        report = calibrator.full_report(days=days)
        ov     = report.get("overall", {})

        if ov.get("n", 0) == 0:
            print("  No settled trades — calibration requires settled outcomes.")
            return

        n   = ov.get("n", 0)
        ratio = ov.get("overall_cal_ratio", 0)
        brier = ov.get("overall_brier_score", 0)
        cal_ok = 0.85 <= ratio <= 1.10

        status = "\033[92m✓ Well-calibrated\033[0m" if cal_ok else "\033[91m⚠ Review sizing\033[0m"
        print(
            f"  {n} settled trades  │  "
            f"Overall ratio: {ratio:.4f}  │  "
            f"Brier: {brier:.4f}  │  {status}"
        )

        for s in report.get("strategies", []):
            grade = s["calibration_grade"]
            ok    = "✓" if s["is_well_calibrated"] else "⚠"
            print(
                f"  {ok}  {s['strategy'][:25]:<25}  "
                f"model={s['model_win_rate']*100:.0f}%  "
                f"actual={s['actual_win_rate']*100:.0f}%  "
                f"ratio={s['calibration_ratio']:.3f}  "
                f"divisor={s['recommended_divisor']}  {grade}"
            )
    except Exception as e:
        print(f"  Calibration data unavailable: {e}")


def _render_footer() -> None:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n  {_hr('─', W - 2)}")
    print(
        f"  Refreshed {now}  │  "
        f"Commands: [trade.py] order  [blotter.py] history  "
        f"[session_report.py] report  │  Ctrl-C to exit"
    )
    print(_hr("═"))


# ── Main dashboard loop ───────────────────────────────────────────────────────

async def run_dashboard(
    interval:    float,
    once:        bool,
    show_cal:    bool,
    days:        int,
) -> None:
    creds   = CredentialManager()
    limiter = RateLimiter()
    blotter = Blotter()
    store   = MetricsStore()
    calc    = MetricsCalculator(store)
    perf    = PerformanceAnalytics(blotter)
    cal     = KellyCalibrator(blotter)

    import aiohttp
    session = aiohttp.ClientSession()
    monitor = PortfolioMonitor(creds, limiter)
    monitor._session = session

    # Build order entry for open order display (read-only cache)
    order_entry = OrderEntry(creds, limiter)
    order_entry._session = session

    # Alert summary: read from last alert_manager run if available
    # (dashboard reads blotter + log; it does not start alert_manager itself)
    alert_summary: list[dict] = []

    try:
        while True:
            # Fetch live data
            try:
                snapshot = await monitor.refresh()
            except Exception:
                snapshot = None

            # Fetch open orders
            try:
                open_orders = await order_entry.list_open_orders()
                # Cache for sync renderer
                order_entry._open_orders_cache = {
                    o.order_id: {
                        "ticker":    o.ticker,
                        "status":    o.status.value,
                        "count":     o.count,
                        "yes_price": o.yes_price,
                    }
                    for o in open_orders
                }
            except Exception:
                order_entry._open_orders_cache = {}

            # Render
            _clear()
            _render_header(snapshot)
            _render_account(snapshot)
            _render_positions(snapshot)
            _render_performance(perf, days)
            _render_metrics(store, calc)
            _render_open_orders(order_entry)
            _render_alerts(alert_summary)
            if show_cal:
                _render_calibration(cal, days)
            _render_footer()

            if once:
                break

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        pass
    finally:
        await session.close()
        print("\n  Dashboard stopped.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="Kalshi live terminal dashboard",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
parser.add_argument("--interval",    type=float, default=REFRESH_INTERVAL_SECONDS,
                    help=f"Refresh interval seconds (default {REFRESH_INTERVAL_SECONDS})")
parser.add_argument("--once",        action="store_true",
                    help="Single snapshot then exit")
parser.add_argument("--calibration", action="store_true",
                    help="Show Kelly calibration panel")
parser.add_argument("--days",        type=int, default=7,
                    help="Performance window in days (default 7)")


if __name__ == "__main__":
    args = parser.parse_args()
    print(f"[dashboard] ENV={config.ENV}  interval={args.interval}s")
    asyncio.run(run_dashboard(
        interval=args.interval,
        once=args.once,
        show_cal=args.calibration,
        days=args.days,
    ))
