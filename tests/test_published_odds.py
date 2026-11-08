from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from nba_quant.published_odds import bind_published_offers


@pytest.fixture
def data():
    tip = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)
    game = {"id": "401", "home": {"id": "8", "name": "Detroit Pistons", "abbreviation": "DET"},
        "away": {"id": "20", "name": "Philadelphia 76ers", "abbreviation": "PHI"}, "tipoff": tip.isoformat()}
    players = [{"id": "player1", "name": "Tyrese Maxey", "team_id": "20"}]
    offer = {"player_name": "Tyrese Maxey", "event_name": "Philadelphia 76ers @ Detroit Pistons",
        "event_tipoff": (tip + timedelta(minutes=10)).isoformat(), "american_odds": 116,
        "quote_updated_at": None, "price_freshness": "UNKNOWN"}
    return game, players, offer


def test_primary_quote_binding_preserves_time_difference_and_unknown_freshness(data):
    game, players, offer = data
    original = deepcopy(offer)
    result = bind_published_offers({"offers": [offer]}, game, players)
    assert len(result) == 1
    assert result[0]["player_id"] == "player1"
    assert result[0]["event_time_discrepancy_seconds"] == 600
    assert result[0]["price_freshness"] == "UNKNOWN"
    assert result[0]["eligible_for_current_pricing"] is False
    assert result[0]["actionable"] is False
    assert offer == original


@pytest.mark.parametrize("kind", ["player", "suffix", "ambiguous", "team", "other_event", "far_time", "missing_time"])
def test_primary_quote_cannot_migrate_to_unrelated_player_or_game(data, kind):
    game, players, offer = data
    if kind == "player": offer["player_name"] = "Someone Else"
    if kind == "suffix": offer["player_name"] = "Tyrese Maxey Jr."
    if kind == "ambiguous": players.append({"id": "other", "name": "Tyrese Maxey", "team_id": "8"})
    if kind == "team": players[0]["team_id"] = "99"
    if kind == "other_event": offer["event_name"] = "Philadelphia 76ers @ Boston Celtics"
    if kind == "far_time": offer["event_tipoff"] = (datetime.fromisoformat(game["tipoff"]) + timedelta(hours=2)).isoformat()
    if kind == "missing_time": offer.pop("event_tipoff")
    assert bind_published_offers({"offers": [offer]}, game, players) == []
