"""
tests/test_circuit_breaker.py

Unit tests for CircuitBreaker — verifies all five risk conditions:
    1. Position size capping (clamp, not reject)
    2. Max open positions limit
    3. Sector concentration limit
    4. Peak-to-trough drawdown kill switch
    5. Daily loss limit kill switch

Run with:
    python -m pytest tests/test_circuit_breaker.py -v

Or without pytest:
    python tests/test_circuit_breaker.py
"""

import asyncio
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Config shim ───────────────────────────────────────────────────────────────
cfg = types.ModuleType("config")
cfg.MAX_POSITION_CENTS       = 10_000  # $100 cap
cfg.MAX_OPEN_POSITIONS       = 3       # low limit for easy testing
cfg.MAX_SECTOR_CONCENTRATION = 0.50   # 50% concentration limit
cfg.MAX_DRAWDOWN_PCT         = 0.10   # 10% drawdown trigger
cfg.DAILY_LOSS_LIMIT_CENTS   = 5_000  # $50 daily loss limit
cfg.FEE_PER_CONTRACT_CENTS   = 0.0
cfg.DB_PATH                  = ":memory:"
cfg.USE_POSTGRES             = False
cfg.POSTGRES_URL             = ""
sys.modules["config"] = cfg

# ── Logger shim ───────────────────────────────────────────────────────────────
log_mod = types.ModuleType("logging_.structured_logger")
class _StubLogger:
    def __getattr__(self, _): return lambda *a, **kw: None
log_mod.logger = _StubLogger()
sys.modules["logging_"]                    = types.ModuleType("logging_")
sys.modules["logging_.structured_logger"] = log_mod

from risk.circuit_breaker import CircuitBreaker, Position
from strategy.base_strategy import Side, Signal


# ── Kill switch tracking ──────────────────────────────────────────────────────

_kill_called = False

async def _mock_kill():
    global _kill_called
    _kill_called = True


def _fresh() -> CircuitBreaker:
    """Return a fresh, untripped CircuitBreaker with kill tracking reset."""
    global _kill_called
    _kill_called = False
    return CircuitBreaker(kill_switch=_mock_kill)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _signal(
    ticker:     str = "TEST",
    size_cents: int = 1_000,
    sector:     str = "Politics",
    price:      int = 50,
) -> Signal:
    return Signal(
        ticker=ticker,
        side=Side.YES,
        size_cents=size_cents,
        limit_price=price,
        edge=0.05,
        edge_to_vig=2.5,
        confidence=0.60,
        strategy="test",
        meta={"sector": sector, "category": sector},
    )


def _fill(
    ticker:     str = "TEST",
    size_cents: int = 1_000,
    price:      int = 50,
    sector:     str = "Politics",
) -> dict:
    return {"ticker": ticker, "side": "yes",
            "size_cents": size_cents, "price": price, "sector": sector}


def _inject_position(cb: CircuitBreaker, ticker: str, size_cents: int,
                     price: int = 50, sector: str = "Politics") -> None:
    """
    Directly inject a Position into the CircuitBreaker's internal dict.
    This bypasses approve() to set up pre-conditions without order submission.
    """
    cb._positions[ticker] = Position(
        ticker=ticker,
        sector=sector,
        size_cents=size_cents,
        entry_price=price,
        current_price=price,
        side="yes",
    )
    cb._sector_exposure[sector] += size_cents


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPositionSizeCap:
    """Check 1: signals above MAX_POSITION_CENTS are clamped, not rejected."""

    def test_above_cap_clamped_and_approved(self):
        cb  = _fresh()
        sig = _signal(size_cents=50_000)   # $500 >> $100 cap
        assert cb.approve(sig) is True
        assert sig.size_cents == cfg.MAX_POSITION_CENTS, (
            f"Expected {cfg.MAX_POSITION_CENTS}c, got {sig.size_cents}"
        )

    def test_at_cap_approved_unchanged(self):
        cb  = _fresh()
        sig = _signal(size_cents=cfg.MAX_POSITION_CENTS)
        assert cb.approve(sig) is True
        assert sig.size_cents == cfg.MAX_POSITION_CENTS

    def test_below_cap_approved_unchanged(self):
        cb  = _fresh()
        sig = _signal(size_cents=500)
        assert cb.approve(sig) is True
        assert sig.size_cents == 500


class TestMaxOpenPositions:
    """Check 2: no new signals approved once MAX_OPEN_POSITIONS is reached."""

    def test_at_limit_rejects_overflow(self):
        cb = _fresh()

        # Inject MAX_OPEN_POSITIONS positions — each in its own sector
        # to avoid triggering the concentration check
        for i in range(cfg.MAX_OPEN_POSITIONS):
            _inject_position(cb, f"TICK-{i}", size_cents=1_000, sector=f"Sector{i}")

        assert len(cb._positions) == cfg.MAX_OPEN_POSITIONS

        overflow = _signal(ticker="TICK-overflow", size_cents=1_000, sector="SectorNew")
        assert cb.approve(overflow) is False, "Should reject when at max positions"

    def test_approved_below_limit(self):
        cb = _fresh()

        for i in range(cfg.MAX_OPEN_POSITIONS - 1):
            _inject_position(cb, f"TICK-{i}", size_cents=1_000, sector=f"Sector{i}")

        sig = _signal(ticker="TICK-new", sector="SectorNew")
        assert cb.approve(sig) is True, "Should approve when one slot remains"

    def test_after_close_accepts_new(self):
        cb = _fresh()

        for i in range(cfg.MAX_OPEN_POSITIONS):
            _inject_position(cb, f"TICK-{i}", size_cents=1_000, sector=f"Sector{i}")

        # Close one position
        cb.record_close("TICK-0", exit_price=60)
        assert len(cb._positions) == cfg.MAX_OPEN_POSITIONS - 1

        new_sig = _signal(ticker="TICK-new", sector="SectorNew")
        assert cb.approve(new_sig) is True, "Should accept after closing one position"


class TestSectorConcentration:
    """Check 3: reject signals that would push one sector over the limit."""

    def test_concentration_breach_rejected(self):
        cb = _fresh()
        # Economics = 8_000c, Politics = 2_000c → total = 10_000c
        # Economics concentration = 80% > 50% limit
        _inject_position(cb, "ECON-1", size_cents=8_000, sector="Economics")
        _inject_position(cb, "POLI-1", size_cents=2_000, sector="Politics")

        breach = _signal(ticker="ECON-2", size_cents=500, sector="Economics")
        assert cb.approve(breach) is False, (
            "Should reject signal that pushes Economics above 50% concentration"
        )

    def test_diverse_sectors_approved(self):
        cb = _fresh()
        _inject_position(cb, "POLI-1", size_cents=2_000, sector="Politics")
        _inject_position(cb, "ECON-1", size_cents=2_000, sector="Economics")

        new_sig = _signal(ticker="SPRT-1", size_cents=2_000, sector="Sports")
        assert cb.approve(new_sig) is True, "Diverse sector should be approved"

    def test_no_positions_skips_concentration_check(self):
        """Empty portfolio — concentration check should not block."""
        cb  = _fresh()
        sig = _signal(ticker="FIRST", size_cents=1_000, sector="Economics")
        assert cb.approve(sig) is True


class TestDrawdownKillSwitch:
    """Check 4: breaker trips when peak-to-trough drawdown exceeds MAX_DRAWDOWN_PCT."""

    def test_drawdown_trips_breaker(self):
        async def _run():
            cb = _fresh()
            # Inject a position: 100 contracts at 50c = 5_000c cost
            _inject_position(cb, "TEST", size_cents=5_000, price=50)
            # Establish peak equity by marking at entry price
            cb.mark_to_market("TEST", 50)
            assert cb.is_tripped is False

            # Drop to 30c: position value = 30 * 100 = 3_000c
            # drawdown = (5_000 - 3_000) / 5_000 = 40% >> 10% limit
            cb.mark_to_market("TEST", 30)
            await asyncio.sleep(0)   # let kill_switch task run
            assert cb.is_tripped is True, "Should trip on 40% drawdown"
            assert _kill_called is True, "Kill switch must be called"

        asyncio.run(_run())

    def test_within_limit_no_trip(self):
        async def _run():
            cb = _fresh()
            _inject_position(cb, "TEST", size_cents=5_000, price=50)
            cb.mark_to_market("TEST", 50)   # establish peak
            cb.mark_to_market("TEST", 47)   # ~6% drop — under 10% limit
            await asyncio.sleep(0)
            assert cb.is_tripped is False

        asyncio.run(_run())

    def test_reset_clears_trip(self):
        async def _run():
            cb = _fresh()
            _inject_position(cb, "TEST", size_cents=5_000, price=50)
            cb.mark_to_market("TEST", 50)
            cb.mark_to_market("TEST", 30)
            await asyncio.sleep(0)
            assert cb.is_tripped is True

            cb.reset()
            assert cb.is_tripped is False, "reset() should clear the tripped state"

        asyncio.run(_run())


class TestDailyLossLimit:
    """Check 5: breaker trips when daily P&L exceeds DAILY_LOSS_LIMIT_CENTS."""

    def test_daily_loss_trips_breaker(self):
        async def _run():
            cb = _fresh()
            # Inject and close a losing position.
            # Position: 200 contracts at 50c = 10_000c cost
            # Close at 20c → P&L = (20 - 50) * 200 = -6_000c > -5_000c limit
            _inject_position(cb, "TEST", size_cents=10_000, price=50)
            cb.record_close("TEST", exit_price=20)
            await asyncio.sleep(0)
            assert cb.is_tripped is True, (
                f"Should trip: daily_pnl={cb._daily_pnl} < -{cfg.DAILY_LOSS_LIMIT_CENTS}"
            )

        asyncio.run(_run())

    def test_small_loss_no_trip(self):
        async def _run():
            cb = _fresh()
            # Small position: 10 contracts at 50c = 500c cost
            # Close at 40c → P&L = (40 - 50) * 10 = -100c — well under limit
            _inject_position(cb, "TEST", size_cents=500, price=50)
            cb.record_close("TEST", exit_price=40)
            await asyncio.sleep(0)
            assert cb.is_tripped is False

        asyncio.run(_run())

    def test_profit_does_not_trip(self):
        async def _run():
            cb = _fresh()
            _inject_position(cb, "TEST", size_cents=5_000, price=50)
            cb.record_close("TEST", exit_price=80)  # profitable close
            await asyncio.sleep(0)
            assert cb.is_tripped is False

        asyncio.run(_run())


class TestAlreadyTripped:
    """Once tripped, every subsequent approve() must return False."""

    def test_all_signals_rejected_when_tripped(self):
        cb = _fresh()
        cb.is_tripped = True
        for ticker in ["A", "B", "C", "D"]:
            assert cb.approve(_signal(ticker=ticker)) is False, (
                f"Tripped breaker should reject {ticker}"
            )


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all_tests():
    test_classes = [
        TestPositionSizeCap,
        TestMaxOpenPositions,
        TestSectorConcentration,
        TestDrawdownKillSwitch,
        TestDailyLossLimit,
        TestAlreadyTripped,
    ]

    total = passed = failed = 0
    failures = []

    for cls in test_classes:
        methods = [m for m in dir(cls) if m.startswith("test_")]
        print(f"\n  {cls.__name__} ({len(methods)} tests)")

        for name in methods:
            total += 1
            instance = cls()
            try:
                getattr(instance, name)()
                print(f"    ✓  {name}")
                passed += 1
            except AssertionError as e:
                print(f"    ✗  {name}  —  {e}")
                failures.append(f"{cls.__name__}.{name}: {e}")
                failed += 1
            except Exception as e:
                import traceback
                tb = traceback.format_exc().strip().splitlines()[-1]
                print(f"    ✗  {name}  —  {tb}")
                failures.append(f"{cls.__name__}.{name}: {tb}")
                failed += 1

    print(f"\n{'═' * 55}")
    print(f"  Results: {passed}/{total} passed  |  {failed} failed")
    if failures:
        print("\n  Failures:")
        for f in failures:
            print(f"    • {f}")
    print(f"{'═' * 55}\n")
    return failed == 0


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ok = run_all_tests()
    sys.exit(0 if ok else 1)
