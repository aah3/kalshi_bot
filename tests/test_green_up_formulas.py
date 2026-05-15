"""
tests/test_green_up_formulas.py

Unit tests for GreenUpPosition hedge math.

Every test case is derived directly from the spec examples so the formulas
are verified against the exact numbers in the design document.

Run with:
    python -m pytest tests/test_green_up_formulas.py -v

Or without pytest:
    python tests/test_green_up_formulas.py
"""

import sys
import os
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Minimal config shim (no env vars needed for unit tests) ──────────────────
import types
cfg = types.ModuleType("config")
cfg.KELLY_DIVISOR         = 4
cfg.MAX_POSITION_CENTS    = 10_000
cfg.MIN_EDGE_TO_VIG       = 0.02
cfg.FEE_PER_CONTRACT_CENTS = 0.0   # zero fees for clean formula verification
cfg.PROFIT_TARGET_PCT     = 0.60
cfg.POSITION_STOP_LOSS_PCT = 0.40
cfg.MIN_ACCOUNT_BALANCE_CENTS = 5_000
sys.modules["config"] = cfg

# Stub logger so strategy imports don't fail
import types as _types
log_mod = _types.ModuleType("logging_.structured_logger")
class _StubLogger:
    def __getattr__(self, _): return lambda *a, **kw: None
log_mod.logger = _StubLogger()
sys.modules["logging_"] = _types.ModuleType("logging_")
sys.modules["logging_.structured_logger"] = log_mod

from strategy.green_up_strategy import GreenUpPosition, HedgeMode


# ── Helpers ───────────────────────────────────────────────────────────────────

def assert_close(actual, expected, tolerance=1, label=""):
    """Assert two cent values are within `tolerance` cents of each other."""
    diff = abs(actual - expected)
    if diff > tolerance:
        raise AssertionError(
            f"{label}: expected {expected}c, got {actual}c "
            f"(diff={diff}c, tolerance={tolerance}c)"
        )


# ── Test suite ────────────────────────────────────────────────────────────────

class TestFullGreenFormula:
    """
    Spec example (Formula 1):
        Entry:  $100 stake at 5.00 odds (20c YES price)
        Potential return: $100 × 5.00 = $500
        Hedge at:  2.50 odds (40c NO price)
        Hedge stake = $500 / 2.50 = $200
        Locked profit = $500 - $100 - $200 = $200
    Mapped to cents:
        entry_stake=10_000c, entry_price=20c, no_price=40c
        hedge_stake=20_000c, locked_profit=20_000c
    """

    def setup(self):
        self.pos = GreenUpPosition(
            ticker="TEST-MARKET",
            entry_price_cents=20,     # 5.00 decimal odds
            entry_stake_cents=10_000, # $100
        )

    def test_entry_decimal_odds(self):
        self.setup()
        assert self.pos.entry_decimal_odds == 5.0, (
            f"Expected 5.0 odds, got {self.pos.entry_decimal_odds}"
        )

    def test_potential_return(self):
        self.setup()
        # $100 × 5.00 = $500 = 50_000c
        assert self.pos.potential_return_cents == 50_000, (
            f"Expected 50_000c, got {self.pos.potential_return_cents}"
        )

    def test_hedge_stake(self):
        self.setup()
        no_price = 40  # 2.50 decimal odds
        hedge_c, locked_c = self.pos.compute_full_green(no_price)
        # $500 / 2.50 = $200 = 20_000c
        assert_close(hedge_c, 20_000, tolerance=1, label="full_green hedge_stake")

    def test_locked_profit(self):
        self.setup()
        no_price = 40
        hedge_c, locked_c = self.pos.compute_full_green(no_price)
        # $500 - $100 - $200 = $200 = 20_000c
        assert_close(locked_c, 20_000, tolerance=1, label="full_green locked_profit")

    def test_if_yes_wins(self):
        """If YES wins: collect $500 from entry, pay out $200 hedge cost."""
        self.setup()
        no_price = 40
        hedge_c, _ = self.pos.compute_full_green(no_price)
        yes_payout = self.pos.potential_return_cents
        net_if_yes = yes_payout - self.pos.entry_stake_cents - hedge_c
        # $500 - $100 - $200 = $200
        assert_close(net_if_yes, 20_000, tolerance=1, label="if_yes_wins net")

    def test_if_no_wins(self):
        """If NO wins: collect hedge_stake × NO_odds, lose entry stake."""
        self.setup()
        no_price = 40
        no_decimal_odds = 100.0 / no_price  # 2.50
        hedge_c, _ = self.pos.compute_full_green(no_price)
        no_payout = int(hedge_c * no_decimal_odds)
        net_if_no = no_payout - self.pos.entry_stake_cents - hedge_c
        # $200 × 2.50 = $500; $500 - $100 - $200 = $200
        assert_close(net_if_no, 20_000, tolerance=2, label="if_no_wins net")

    def test_equal_profit_both_outcomes(self):
        """Core property: profit must be equal regardless of outcome."""
        self.setup()
        no_price = 40
        hedge_c, locked = self.pos.compute_full_green(no_price)

        yes_payout = self.pos.potential_return_cents
        no_decimal_odds = 100.0 / no_price
        no_payout  = int(hedge_c * no_decimal_odds)

        profit_if_yes = yes_payout - self.pos.entry_stake_cents - hedge_c
        profit_if_no  = no_payout  - self.pos.entry_stake_cents - hedge_c

        assert_close(profit_if_yes, profit_if_no, tolerance=2,
                     label="equal profit both outcomes")

    def test_zero_price_returns_zero(self):
        self.setup()
        hedge_c, locked_c = self.pos.compute_full_green(0)
        assert hedge_c == 0 and locked_c == 0

    def test_high_entry_price(self):
        """Entry at 50c (2.00 odds), hedge at 30c (3.33 odds)."""
        pos = GreenUpPosition(
            ticker="TEST",
            entry_price_cents=50,
            entry_stake_cents=10_000,  # $100
        )
        # potential = 10_000 × 2.00 = 20_000c ($200)
        assert pos.potential_return_cents == 20_000
        hedge_c, locked_c = pos.compute_full_green(30)
        # hedge = 20_000 / (100/30) = 20_000 / 3.333 ≈ 6_000c
        assert_close(hedge_c, 6_000, tolerance=5, label="50c_entry hedge")
        # locked = 20_000 - 10_000 - 6_000 = 4_000c
        assert_close(locked_c, 4_000, tolerance=5, label="50c_entry locked")


class TestStakeBackFormula:
    """
    Spec example (Formula 2):
        Entry:  $100 stake at 5.00 odds (20c YES price)
        Hedge at:  2.50 odds (40c NO price)
        Hedge stake = $100 / (2.50 - 1) = $100 / 1.50 = $66.67 ≈ $66.67
        If NO wins:  $66.67 × 2.50 = $166.67 - $100 - $66.67 ≈ $0
        If YES wins: $500 - $100 - $66.67 = $333.33
    Mapped to cents:
        entry_stake=10_000c, entry_price=20c, no_price=40c
        hedge_stake≈6_667c
        profit_if_yes≈33_333c
    """

    def setup(self):
        self.pos = GreenUpPosition(
            ticker="TEST-MARKET",
            entry_price_cents=20,
            entry_stake_cents=10_000,
        )

    def test_hedge_stake(self):
        self.setup()
        no_price = 40  # 2.50 odds
        hedge_c, _ = self.pos.compute_stake_back(no_price)
        # $100 / (2.50 - 1) = $100 / 1.50 = $66.67 = 6_667c
        assert_close(hedge_c, 6_667, tolerance=2, label="stake_back hedge_stake")

    def test_profit_if_yes_wins(self):
        self.setup()
        no_price = 40
        hedge_c, profit_if_yes = self.pos.compute_stake_back(no_price)
        # $500 - $100 - $66.67 = $333.33 = 33_333c
        assert_close(profit_if_yes, 33_333, tolerance=5, label="stake_back profit_if_yes")

    def test_breakeven_if_no_wins(self):
        """If NO wins, we should break even (net ≈ $0)."""
        self.setup()
        no_price = 40
        no_decimal_odds = 100.0 / no_price  # 2.50
        hedge_c, _ = self.pos.compute_stake_back(no_price)
        no_payout = hedge_c * no_decimal_odds
        net_if_no = no_payout - self.pos.entry_stake_cents - hedge_c
        # Should be close to 0 (break-even)
        assert abs(net_if_no) <= 200, (  # tolerance 200c ($2) for int rounding
            f"Expected near-zero break-even, got {net_if_no}c"
        )

    def test_stake_back_less_than_full_green(self):
        """Stake-back hedge is always smaller than full-green hedge."""
        self.setup()
        no_price = 40
        full_green_h, _ = self.pos.compute_full_green(no_price)
        stake_back_h, _ = self.pos.compute_stake_back(no_price)
        assert stake_back_h < full_green_h, (
            f"Stake-back ({stake_back_h}c) should be < full-green ({full_green_h}c)"
        )

    def test_odds_net_less_than_one_returns_zero(self):
        """If NO price >= 50c (odds <= 2.00), odds_net = 1.00, hedge = entry_stake."""
        self.setup()
        no_price = 50   # 2.00 odds, odds_net = 1.0
        hedge_c, _ = self.pos.compute_stake_back(no_price)
        # hedge = 10_000 / 1.0 = 10_000c (fine — exactly $100)
        assert hedge_c == 10_000, f"Expected 10_000c, got {hedge_c}"

    def test_no_price_99_returns_tiny_hedge(self):
        """If NO is very expensive (99c), odds_net is tiny → hedge is huge."""
        self.setup()
        # no_price=99 → odds=1.01 → odds_net=0.01 → hedge = 10_000 / 0.01 = 1_000_000c
        # This would exceed MAX_POSITION_CENTS so it gets capped in strategy
        # But the formula itself should produce a large number
        hedge_c, _ = self.pos.compute_stake_back(99)
        assert hedge_c > self.pos.entry_stake_cents, (
            "Expensive NO hedge should exceed entry stake"
        )


class TestPartialHedge:
    """Partial hedge at fraction=0.5 should give exactly half of full-green."""

    def setup(self):
        self.pos = GreenUpPosition(
            ticker="TEST",
            entry_price_cents=20,
            entry_stake_cents=10_000,
        )

    def test_half_hedge(self):
        self.setup()
        no_price = 40
        full_c, full_profit   = self.pos.compute_full_green(no_price)
        partial_c, floor_c    = self.pos.compute_partial(no_price, fraction=0.5)
        assert_close(partial_c, full_c // 2, tolerance=1, label="partial hedge 50%")

    def test_full_fraction_equals_full_green(self):
        self.setup()
        no_price = 40
        full_c, full_profit  = self.pos.compute_full_green(no_price)
        partial_c, floor_c   = self.pos.compute_partial(no_price, fraction=1.0)
        assert_close(partial_c, full_c, tolerance=1, label="partial at 100% == full_green")

    def test_zero_fraction_returns_zero(self):
        self.setup()
        partial_c, _ = self.pos.compute_partial(40, fraction=0.0)
        assert partial_c == 0


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_zero_entry_price(self):
        pos = GreenUpPosition(ticker="T", entry_price_cents=0, entry_stake_cents=10_000)
        assert pos.potential_return_cents == 0
        hedge_c, locked = pos.compute_full_green(40)
        assert hedge_c == 0 and locked == 0

    def test_one_cent_entry(self):
        """1c entry = 100x odds — extreme underdog."""
        pos = GreenUpPosition(ticker="T", entry_price_cents=1, entry_stake_cents=10_000)
        assert pos.entry_decimal_odds == 100.0
        assert pos.potential_return_cents == 1_000_000  # $10,000 payout
        hedge_c, locked = pos.compute_full_green(40)
        # hedge = 1_000_000 / 2.5 = 400_000c
        assert_close(hedge_c, 400_000, tolerance=5, label="1c_entry hedge")

    def test_99_cent_entry(self):
        """99c entry = 1.01x odds — heavy favourite."""
        pos = GreenUpPosition(ticker="T", entry_price_cents=99, entry_stake_cents=10_000)
        assert abs(pos.entry_decimal_odds - 100/99) < 0.01
        hedge_c, locked = pos.compute_full_green(40)
        # potential ≈ 10_101c; hedge = 10_101 / 2.5 ≈ 4_040c
        assert hedge_c > 0

    def test_full_green_preserves_equal_profit_at_many_prices(self):
        """For a range of NO prices, verify equal-profit property holds."""
        entry_stake = 10_000
        entry_price = 20
        pos = GreenUpPosition(
            ticker="T",
            entry_price_cents=entry_price,
            entry_stake_cents=entry_stake,
        )
        yes_payout = pos.potential_return_cents

        for no_price in [20, 30, 40, 50, 60, 70, 80]:
            hedge_c, _ = pos.compute_full_green(no_price)
            if hedge_c == 0:
                continue
            no_odds = 100.0 / no_price
            profit_if_yes = yes_payout - entry_stake - hedge_c
            profit_if_no  = int(hedge_c * no_odds) - entry_stake - hedge_c
            # Allow 2c rounding tolerance
            diff = abs(profit_if_yes - profit_if_no)
            assert diff <= 2, (
                f"no_price={no_price}: profit_yes={profit_if_yes} "
                f"profit_no={profit_if_no} diff={diff} (should be ≤2c)"
            )


# ── Simple test runner (no pytest needed) ─────────────────────────────────────

def run_all_tests():
    test_classes = [
        TestFullGreenFormula,
        TestStakeBackFormula,
        TestPartialHedge,
        TestEdgeCases,
    ]

    total = passed = failed = 0
    failures = []

    for cls in test_classes:
        instance = cls()
        methods  = [m for m in dir(cls) if m.startswith("test_")]
        print(f"\n  {cls.__name__} ({len(methods)} tests)")

        for method_name in methods:
            total += 1
            try:
                if hasattr(instance, "setup"):
                    instance.setup()
                getattr(instance, method_name)()
                print(f"    ✓  {method_name}")
                passed += 1
            except AssertionError as e:
                print(f"    ✗  {method_name}  —  {e}")
                failures.append(f"{cls.__name__}.{method_name}: {e}")
                failed += 1
            except Exception as e:
                print(f"    ✗  {method_name}  —  UNEXPECTED: {e}")
                failures.append(f"{cls.__name__}.{method_name}: UNEXPECTED {e}")
                failed += 1

    print(f"\n{'═'*55}")
    print(f"  Results: {passed}/{total} passed  |  {failed} failed")
    if failures:
        print(f"\n  Failures:")
        for f in failures:
            print(f"    • {f}")
    print(f"{'═'*55}\n")
    return failed == 0


if __name__ == "__main__":
    ok = run_all_tests()
    sys.exit(0 if ok else 1)
