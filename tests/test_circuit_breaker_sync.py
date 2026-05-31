"""Circuit breaker portfolio sync tests."""

import asyncio

import pytest

import risk.circuit_breaker as cb_mod
from risk.circuit_breaker import CircuitBreaker
from trading.portfolio_monitor import PortfolioSnapshot, Position


async def _noop_kill():
    pass


@pytest.fixture
def cb():
    return CircuitBreaker(kill_switch=_noop_kill)


def test_sync_from_portfolio_sets_peak_and_positions(cb: CircuitBreaker):
    snap = PortfolioSnapshot(
        positions=[
            Position(
                ticker="T1",
                side="yes",
                contracts=10,
                avg_entry_price=50,
            )
        ],
        cash_balance_cents=5_000,
        portfolio_value_cents=10_000,
        total_cost_basis_cents=5_000,
        total_unrealised_pnl_cents=0,
        total_max_payout_cents=1_000,
        session_realised_pnl_cents=0,
    )
    snap.positions[0].cost_basis = 500
    snap.positions[0].current_value = 500

    cb.sync_from_portfolio(snap)
    assert "T1" in cb._positions
    assert cb._peak_equity >= 10_000
    assert cb._last_portfolio_equity == 10_000


def test_sync_trips_on_session_loss(cb: CircuitBreaker, monkeypatch):
    # Patch the config object the breaker actually reads (another test module
    # may have swapped sys.modules["config"] for a stub).
    monkeypatch.setattr(cb_mod.config, "DAILY_LOSS_LIMIT_CENTS", 5_000)
    # _trip schedules kill_switch via create_task; no loop in plain pytest.
    def _noop_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(asyncio, "create_task", _noop_create_task)
    cb._session_start_equity = 100_000
    snap = PortfolioSnapshot(
        positions=[],
        cash_balance_cents=40_000,
        portfolio_value_cents=40_000,
        total_cost_basis_cents=0,
        total_unrealised_pnl_cents=0,
        total_max_payout_cents=0,
        session_realised_pnl_cents=0,
    )
    cb.sync_from_portfolio(snap)
    assert cb.is_tripped


def test_breaker_baseline_adopts_snapshot_session_total(cb: CircuitBreaker, monkeypatch):
    # On first sync the breaker must adopt the monitor's session baseline so its
    # session-loss metric is identical to the snapshot's session_total_pnl_cents
    # shown on the live monitor / dashboard. Use a generous limit so the loss
    # does not trip the kill switch — this test only checks the baseline math.
    monkeypatch.setattr(cb_mod.config, "DAILY_LOSS_LIMIT_CENTS", 1_000_000)
    snap = PortfolioSnapshot(
        positions=[],
        cash_balance_cents=9_478,
        portfolio_value_cents=29_215,   # cash + open position value
        total_cost_basis_cents=14_849,
        total_unrealised_pnl_cents=-894,
        total_max_payout_cents=0,
        session_realised_pnl_cents=0,   # nothing closed this session
        session_total_pnl_cents=-522,   # equity is down $5.22 since start
    )
    cb.sync_from_portfolio(snap)

    # Baseline implied by the snapshot: value - session_total.
    assert cb._session_start_equity == 29_215 - (-522)
    # Breaker's session-loss metric now equals the displayed figure.
    session_pnl = snap.portfolio_value_cents - cb._session_start_equity
    assert session_pnl == snap.session_total_pnl_cents == -522


def test_sync_trips_on_unrealised_session_loss(cb: CircuitBreaker, monkeypatch):
    # Reproduces the production run: realised P&L is $0 (nothing closed) but a
    # held position bleeds unrealised P&L past the limit. The kill switch must
    # trip on the total session loss even though "session realised" is zero.
    monkeypatch.setattr(cb_mod.config, "DAILY_LOSS_LIMIT_CENTS", 500)  # $5 cap

    def _noop_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(asyncio, "create_task", _noop_create_task)
    snap = PortfolioSnapshot(
        positions=[],
        cash_balance_cents=14_370,
        portfolio_value_cents=29_219,
        total_cost_basis_cents=14_849,
        total_unrealised_pnl_cents=-894,
        total_max_payout_cents=0,
        session_realised_pnl_cents=0,
        session_total_pnl_cents=-522,
    )
    cb.sync_from_portfolio(snap)
    assert cb.is_tripped
