"""Tests for category tags and sports competition filters."""

from discovery.market_client import MarketClient


def test_event_matches_competition_filters_no_filter():
    event = {"product_metadata": {"competition": "EPL", "competition_scope": "Game"}}
    assert MarketClient._event_matches_competition_filters(event, None, None)


def test_event_matches_competition_filters_epl():
    event = {"product_metadata": {"competition": "EPL", "competition_scope": "Game"}}
    assert MarketClient._event_matches_competition_filters(event, "epl", None)
    assert not MarketClient._event_matches_competition_filters(event, "nba", None)


def test_event_matches_competition_filters_scope():
    event = {"product_metadata": {"competition": "EPL", "competition_scope": "Game"}}
    assert MarketClient._event_matches_competition_filters(event, "epl", "Games")
    assert MarketClient._event_matches_competition_filters(event, "epl", "game")
    assert not MarketClient._event_matches_competition_filters(event, "epl", "futures")


def test_normalize_scope():
    assert MarketClient._normalize_scope("Games") == "game"
    assert MarketClient._normalize_scope("Futures") == "future"


def test_event_matches_competition_filters_missing_meta():
    event = {"product_metadata": {}}
    assert not MarketClient._event_matches_competition_filters(event, "epl", None)
