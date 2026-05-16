"""Tests for demo/prod credential resolution."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from credentials.env_credentials import normalize_env, resolve_credentials


def test_normalize_env():
    assert normalize_env("production") == "production"
    assert normalize_env("prod") == "production"
    assert normalize_env("demo") == "demo"


def test_resolve_demo_specific():
    os.environ["KALSHI_ENV"] = "demo"
    os.environ["KALSHI_DEMO_API_KEY_ID"] = "demo-id"
    os.environ["KALSHI_DEMO_PRIVATE_KEY_B64"] = "ZGVtbw=="
    os.environ.pop("KALSHI_PROD_API_KEY_ID", None)
    kid, b64, src = resolve_credentials("demo")
    assert kid == "demo-id"
    assert "DEMO" in src


def test_resolve_prod_specific():
    os.environ["KALSHI_ENV"] = "production"
    os.environ["KALSHI_PROD_API_KEY_ID"] = "prod-id"
    os.environ["KALSHI_PROD_PRIVATE_KEY_B64"] = "cHJvZA=="
    kid, b64, src = resolve_credentials("production")
    assert kid == "prod-id"
    assert "PROD" in src


def test_legacy_fallback():
    os.environ["KALSHI_ENV"] = "demo"
    os.environ.pop("KALSHI_DEMO_API_KEY_ID", None)
    os.environ.pop("KALSHI_DEMO_PRIVATE_KEY_B64", None)
    os.environ["KALSHI_API_KEY_ID"] = "legacy-id"
    os.environ["KALSHI_PRIVATE_KEY_B64"] = "bGVnYWN5"
    kid, _, src = resolve_credentials("demo")
    assert kid == "legacy-id"
    assert "legacy" in src


if __name__ == "__main__":
    test_normalize_env()
    test_resolve_demo_specific()
    test_resolve_prod_specific()
    test_legacy_fallback()
    print("ok")
