"""Tests for StrategyInstance schema, loader, adapter, and universe diff."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.strategy_instance import (
    StrategyInstanceError,
    load_strategy_instance,
    parse_strategy_instance,
)
from orchestration.instance_adapter import (
    build_strategy_kwargs,
    criteria_from_instance,
)
from orchestration.universe_manager import UniverseManager
from strategy.green_up_strategy import GreenUpStrategy, PositionState
from strategy.position_limits import ticker_is_protected


def _minimal_gu(**overrides):
    base = {
        "id": "gu_test",
        "strategy": "green_up",
        "universe": {
            "mode": "discover",
            "refresh_seconds": 60,
            "max_watchlist": 3,
            "discover": {
                "category": "Sports",
                "preset": "green_up",
                "top_n": 3,
                "max_yes_ask": 30,
            },
        },
        "strategy_params": {
            "entry_max": 30,
            "hedge_trigger": 44,
            "hedge_mode": "stake_back",
            "gu_entry_mode": "limit_offset",
            "gu_limit_offset": -2,
            "gu_max_cycles": 0,
        },
        "risk": {"max_concurrent_positions": 2},
        "persistence": {
            "db_path": "test_gu.db",
            "log_file": "test_gu.jsonl",
        },
        "lifecycle": {"on_flat": "keep_running"},
        "runtime": {"quiet": True, "monitor_interval": 30},
    }
    base.update(overrides)
    return base


def test_parse_green_up_instance():
    inst = parse_strategy_instance(_minimal_gu())
    assert inst.id == "gu_test"
    assert inst.strategy == "green_up"
    assert inst.universe.refresh_seconds == 60
    assert inst.universe.max_watchlist == 3
    assert inst.strategy_params["entry_max"] == 30
    assert inst.lifecycle.on_flat == "keep_running"


def test_unknown_strategy_params_rejected():
    data = _minimal_gu()
    data["strategy_params"]["hp_stake_cents"] = 100
    try:
        parse_strategy_instance(data)
        assert False, "expected StrategyInstanceError"
    except StrategyInstanceError as exc:
        assert "hp_stake_cents" in str(exc)


def test_static_requires_tickers():
    data = _minimal_gu()
    data["universe"] = {"mode": "static", "static_tickers": []}
    try:
        parse_strategy_instance(data)
        assert False, "expected StrategyInstanceError"
    except StrategyInstanceError as exc:
        assert "static_tickers" in str(exc)


def test_load_yaml_and_json_roundtrip():
    data = _minimal_gu()
    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / "inst.yaml"
        json_path = Path(tmp) / "inst.json"
        try:
            import yaml
        except ImportError:
            yaml = None
        if yaml is not None:
            yaml_path.write_text(yaml.dump(data), encoding="utf-8")
            loaded = load_strategy_instance(yaml_path)
            assert loaded.id == "gu_test"
            assert loaded.strategy_params["hedge_mode"] == "stake_back"
        json_path.write_text(json.dumps(data), encoding="utf-8")
        loaded_j = load_strategy_instance(json_path)
        assert loaded_j.id == "gu_test"


def test_portfolio_select_by_id():
    portfolio = {
        "instances": [
            _minimal_gu(id="a", enabled=False),
            _minimal_gu(id="b", strategy="high_prob", strategy_params={
                "hp_min_yes_ask": 70,
                "hp_entry_mode": "passive",
            }),
        ]
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "portfolio.json"
        path.write_text(json.dumps(portfolio), encoding="utf-8")
        inst = load_strategy_instance(path, instance_id="b")
        assert inst.id == "b"
        assert inst.strategy == "high_prob"
        # First enabled when no id
        inst2 = load_strategy_instance(path)
        assert inst2.id == "b"


def test_build_strategy_kwargs_and_criteria():
    inst = parse_strategy_instance(_minimal_gu())
    kwargs = build_strategy_kwargs(inst)
    assert kwargs["entry_max"] == 30
    assert kwargs["hedge_mode"] == "stake_back"
    assert kwargs["gu_limit_offset"] == -2

    criteria = criteria_from_instance(inst)
    assert criteria is not None
    assert criteria.category  # resolved
    assert criteria.max_yes_ask == 30
    assert criteria.screener_strategy == "green_up"


def test_example_demo_yaml_loads():
    root = Path(__file__).resolve().parents[1]
    path = root / "config" / "instances" / "gu_sports_underdog.demo.yaml"
    if not path.exists():
        return
    try:
        import yaml  # noqa: F401
    except ImportError:
        return
    inst = load_strategy_instance(path)
    assert inst.id == "gu_sports_underdog"
    assert inst.universe.refresh_seconds == 900
    assert inst.strategy_params["hedge_mode"] == "stake_back"


def test_ticker_is_protected_green_up():
    strat = GreenUpStrategy(entry_max_price=30)
    strat.add_watch_ticker("AAA")
    assert not ticker_is_protected(strat, "AAA")
    pos = strat.get_position("AAA")
    assert pos is not None
    pos.state = PositionState.ENTERED
    assert ticker_is_protected(strat, "AAA")
    pos.state = PositionState.HEDGED
    assert ticker_is_protected(strat, "AAA")
    pos.state = PositionState.SCANNING
    assert not ticker_is_protected(strat, "AAA")
    assert ticker_is_protected(
        strat, "AAA", blotter_open_tickers={"AAA"}
    )


def test_remove_watch_ticker_refuses_entered():
    strat = GreenUpStrategy(entry_max_price=30)
    strat.add_watch_ticker("AAA")
    strat.get_position("AAA").state = PositionState.ENTERED
    assert strat.remove_watch_ticker("AAA") is False
    strat.get_position("AAA").state = PositionState.SCANNING
    assert strat.remove_watch_ticker("AAA") is True
    assert strat.get_position("AAA") is None


def test_universe_diff_drop_if_flat_protects_open():
    inst = parse_strategy_instance(_minimal_gu())
    strat = GreenUpStrategy(entry_max_price=30)
    for t in ("OLD1", "OLD2", "KEEP"):
        strat.add_watch_ticker(t)
    strat.get_position("OLD1").state = PositionState.ENTERED

    ingestor = SimpleNamespace(
        tickers=["OLD1", "OLD2", "KEEP"],
        add_tickers=lambda xs: xs,
        remove_tickers=lambda xs: xs,
    )
    mgr = UniverseManager(
        instance=inst,
        criteria=None,
        strategy=strat,
        ingestor=ingestor,
        watching=["OLD1", "OLD2", "KEEP"],
    )
    diff = mgr.compute_diff(["KEEP", "NEW1", "NEW2"])
    assert "NEW1" in diff.added or "NEW2" in diff.added
    assert "OLD1" in diff.protected
    assert "OLD1" in diff.desired
    assert "OLD2" in diff.dropped


def test_merge_args_sets_discover_flags():
    from orchestration.instance_adapter import _merge_args_from_instance

    inst = parse_strategy_instance(_minimal_gu())
    args = argparse.Namespace(
        strategy="high_prob",
        monitor_interval=None,
        quiet=False,
        auto_take_profit=False,
        exit_when_flat=False,
        exit_on_settle=False,
        max_concurrent_positions=None,
        discover=False,
        discover_category=None,
        discover_top=None,
        discover_min_volume=None,
        discover_min_yes_ask=None,
        discover_max_yes_ask=None,
        discover_max_spread=None,
        discover_activity_hours=None,
        discover_max_minutes_to_close=None,
        discover_rank_by=None,
        discover_min_fee_roi=None,
        discover_tag=None,
        discover_sport=None,
        discover_competition=None,
        discover_scope=None,
        discover_series=None,
        discover_preset=None,
        discover_full_scan=False,
        discover_no_tradeable_filter=False,
        no_live_only=False,
        tickers=None,
        entry_max=None,
        hedge_trigger=None,
        hedge_offset=None,
        hedge_mode=None,
        stop_loss=None,
        gu_no_entry_max=False,
        gu_entry_mode=None,
        gu_exit_mode=None,
        gu_limit_offset=None,
        gu_max_spread=None,
        gu_hedge_style=None,
        gu_max_cycles=None,
        model_prob=None,
        comp_pairs=None,
        hp_min_yes_ask=None,
        hp_max_yes_ask=None,
        hp_entry_mode=None,
        hp_exit_mode=None,
        hp_post_fill=None,
        hp_stake_cents=None,
        hp_take_profit_pct=None,
        hp_take_profit_offset=None,
        hp_stop_loss=None,
        hp_stop_loss_cents=None,
        hp_max_spread=None,
        hp_tp_style=None,
        hp_max_cycles=None,
        mr_lookback=None,
        mr_min_samples=None,
        mr_entry_deviation=None,
        mr_take_profit_offset=None,
        mr_stop_loss_cents=None,
        mr_min_volatility=None,
        mr_entry_max=None,
        mr_entry_min=None,
        mr_short_min_yes_ask=None,
        mr_short_max_yes_ask=None,
        mr_stake_cents=None,
        mr_max_spread=None,
        mr_trade_direction=None,
        mr_exit_target=None,
        mr_entry_mode=None,
        mr_exit_mode=None,
        mr_post_fill=None,
        mr_max_cycles=None,
        mr_limit_offset=None,
    )
    _merge_args_from_instance(inst, args)
    assert args.strategy == "green_up"
    assert args.discover is True
    assert args.discover_category == "Sports"
    assert args.entry_max == 30
    assert args._instance_id == "gu_test"
    assert args.quiet is True


if __name__ == "__main__":
    test_parse_green_up_instance()
    test_unknown_strategy_params_rejected()
    test_static_requires_tickers()
    test_load_yaml_and_json_roundtrip()
    test_portfolio_select_by_id()
    test_build_strategy_kwargs_and_criteria()
    test_example_demo_yaml_loads()
    test_ticker_is_protected_green_up()
    test_remove_watch_ticker_refuses_entered()
    test_universe_diff_drop_if_flat_protects_open()
    test_merge_args_sets_discover_flags()
    print("ok")
