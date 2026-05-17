"""Circuit breaker portfolio sync tests."""

import asyncio

import config
import pytest

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


@pytest.mark.asyncio
async def test_sync_trips_on_session_loss(cb: CircuitBreaker, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_CENTS", 5_000)
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
    await asyncio.sleep(0)
    assert cb.is_tripped
