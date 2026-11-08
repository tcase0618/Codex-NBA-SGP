from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from nba_quant.connected_odds import refresh_connected_odds


class Store:
    def __init__(self):
        self.saved = []

    def save_market(self, record):
        self.saved.append(record)


@pytest.fixture
def bundle():
    now = datetime.now(timezone.utc)
    tip = (now + timedelta(hours=2)).isoformat()
    game = {"id": "nba1", "home": {"id": "4", "name": "LA Clippers"},
            "away": {"id": "2", "name": "Boston Celtics"}, "tipoff": tip, "phase": "pre", "completed": False}
    context = {"status": "AVAILABLE", "stale": False, "games": {"nba1": {"status": "AVAILABLE",
        "home_id": "4", "away_id": "2", "tipoff": tip,
        "players": [{"id": "player1", "name": "Exact Player", "team_id": "4"}]}}}
    event = {"id": "book1", "home_team": "Los Angeles Clippers", "away_team": "Boston Celtics", "commence_time": tip}
    line = {"sportsbook": "fanduel", "source_player_name": "Exact Player", "player_id": None,
        "stat": "pts", "side": "over", "line": 10.5, "decimal_odds": 1.91,
        "observed_at": (now - timedelta(seconds=10)).isoformat(), "retrieved_at": now.isoformat(),
        "source_url": "https://api.the-odds-api.com/v4/sports/basketball_nba/events/book1/odds",
        "source_sha256": "a" * 64}
    class Client:
        configured = True
        calls = 0
        def events(self):
            self.calls += 1
            return {"events": [event]}
        def props(self, key, bookmakers):
            assert key == "book1" and bookmakers == "fanduel"
            self.calls += 1
            return {"event": deepcopy(event), "lines": [deepcopy(line)], "combined_sgp_quote": None}
    return Store(), {"status": "AVAILABLE", "games": [game]}, context, Client(), event, line


def test_no_subscription_means_no_network(bundle):
    store, slate, context, client, _, _ = bundle
    client.configured = False
    report = refresh_connected_odds(store, slate, context, client)
    assert report["status"] == "NOT_CONFIGURED" and client.calls == 0 and not store.saved


def test_automatic_fanduel_exact_identity_preserved(bundle):
    store, slate, context, client, _, line = bundle
    result = refresh_connected_odds(store, slate, context, client)
    assert result["status"] == "ACQUIRED" and result["lines_imported"] == 1
    row = store.saved[0]["offered_rows"][0]
    assert row["player_id"] == "player1" and row["provider_event_id"] == "book1"
    assert row["line"] == 10.5 and row["decimal_odds"] == 1.91
    assert row["observed_at"] == line["observed_at"]
    assert row["authenticity"] == "PROVIDER_REPORTED_NOT_INDEPENDENTLY_VERIFIED"
    assert result["combined_sgp_quote"] is None


@pytest.mark.parametrize("problem", ["different_player", "suffix_mismatch", "stale", "future", "wrong_book", "missing_hash", "bad_price", "wrong_event"])
def test_unsafe_or_unmapped_quote_is_never_saved(bundle, problem):
    store, slate, context, client, event, line = bundle
    if problem == "different_player": line["source_player_name"] = "Other Player"
    if problem == "suffix_mismatch": line["source_player_name"] = "Exact Player Jr."
    if problem == "stale": line["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    if problem == "future": line["observed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    if problem == "wrong_book": line["sportsbook"] = "draftkings"
    if problem == "missing_hash": line["source_sha256"] = ""
    if problem == "bad_price": line["decimal_odds"] = True
    if problem == "wrong_event": event["away_team"] = "New York Knicks"
    result = refresh_connected_odds(store, slate, context, client)
    assert not store.saved and result["lines_imported"] == 0


def test_conflicting_or_stale_game_context_never_binds(bundle):
    store, slate, context, client, _, _ = bundle
    context["stale"] = True
    result = refresh_connected_odds(store, slate, context, client)
    assert result["errors"] and not store.saved


def test_provider_unavailable_never_falls_back_to_invented_price(bundle):
    store, slate, context, client, _, _ = bundle
    def fail():
        raise ValueError("Provider rejected request")
    client.events = fail
    result = refresh_connected_odds(store, slate, context, client)
    assert result["status"] == "PROVIDER_UNAVAILABLE" and not store.saved
