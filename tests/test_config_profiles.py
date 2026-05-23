"""Tests for per-environment config resolution (KALSHI_DEMO_* / KALSHI_PROD_*)."""

import importlib
import os
import sys

import pytest


def _reload_config(monkeypatch, **env):
    import dotenv

    for key in list(os.environ):
        if key.startswith("KALSHI_"):
            monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    # Ignore local .env so tests only see explicit env above
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: True)
    if "config" in sys.modules:
        del sys.modules["config"]
    return importlib.import_module("config")


def test_demo_profile_defaults(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        KALSHI_ENV="demo",
        KALSHI_DEMO_API_KEY_ID="demo-id",
        KALSHI_DEMO_PRIVATE_KEY_B64="dGVzdA==",
    )
    assert cfg.ENV == "demo"
    assert cfg.IS_PRODUCTION is False
    assert cfg.MAX_POSITION_CENTS == 10_000
    assert cfg.DAILY_LOSS_LIMIT_CENTS == 50_000
    assert cfg.DB_PATH == "kalshi_bot_demo.db"
    assert cfg.LOG_FILE == "kalshi_bot_demo.jsonl"
    assert "demo-api.kalshi.co" in cfg.BASE_URL


def test_prod_profile_defaults(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        KALSHI_ENV="production",
        KALSHI_PROD_API_KEY_ID="prod-id",
        KALSHI_PROD_PRIVATE_KEY_B64="dGVzdA==",
    )
    assert cfg.IS_PRODUCTION is True
    assert cfg.MAX_POSITION_CENTS == 100
    assert cfg.DAILY_LOSS_LIMIT_CENTS == 500
    assert cfg.MAX_CONCURRENT_POSITIONS == 1
    assert cfg.DB_PATH == "kalshi_bot_prod.db"
    assert cfg.LOG_FILE == "kalshi_bot_prod.jsonl"
    assert "external-api.kalshi.com" in cfg.BASE_URL


def test_prefixed_overrides_generic(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        KALSHI_ENV="demo",
        KALSHI_DEMO_API_KEY_ID="demo-id",
        KALSHI_DEMO_PRIVATE_KEY_B64="dGVzdA==",
        KALSHI_DEMO_MAX_POSITION_CENTS="2500",
        KALSHI_MAX_POSITION_CENTS="9999",
    )
    assert cfg.MAX_POSITION_CENTS == 2500


def test_hp_stake_capped_by_max_position(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        KALSHI_ENV="production",
        KALSHI_PROD_API_KEY_ID="prod-id",
        KALSHI_PROD_PRIVATE_KEY_B64="dGVzdA==",
        KALSHI_HP_STAKE_CENTS="5000",
    )
    assert cfg.HP_STAKE_CENTS == cfg.MAX_POSITION_CENTS == 100
