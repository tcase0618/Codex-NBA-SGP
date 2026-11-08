"""Fixture-only verification that tonight's real-game cards cannot borrow tickets."""
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from nba_quant.api import create_app
from nba_quant.schemas import Dataset
from nba_quant.sgp import evaluate_sgp
from test_provenance import raw_dataset
from test_sgp import fixture, legs


def numeric_dataset(synthetic=False):
    raw = raw_dataset()
    mapping = {"home": "4", "away": "2"}
    raw["synthetic"] = synthetic
    raw["source"] = "Explicit test-only source receipt"
    raw["teams"] = [{"id": mapping[t["id"]], "name": t["name"]} for t in raw["teams"]]
    for r in raw["rotations"]:
        r["team_id"] = mapping[r["team_id"]]
    for g in raw["games"]:
        g["home_id"], g["away_id"] = mapping[g["home_id"]], mapping[g["away_id"]]
        for b in g["boxes"]:
            b["team_id"] = mapping[b["team_id"]]
    return raw


@pytest.fixture
def tonight(tmp_path, monkeypatch):
    app = create_app(tmp_path / "tonight.sqlite")
    store = app.state.store
    dataset = numeric_dataset()
    key = store.import_dataset(Dataset.model_validate(dataset))
    # A second snapshot of exactly the same history must not double counts.
    duplicate_history = deepcopy(dataset)
    duplicate_history["source"] = "Second test snapshot of the same games"
    store.import_dataset(Dataset.model_validate(duplicate_history))
    frozen, now = fixture(synthetic=False)
    frozen["id"] = "matching-real-label-forecast"
    frozen["request"].update(dataset_id=key, home_id="4", away_id="2")
    for p in frozen["result"]["players"]:
        p["team_id"] = {"H": "4", "A": "2"}[p["team_id"]]
    # Metadata is deliberately fictional; the test verifies joins, not accuracy.
    frozen["result"]["home_win_probability"] = .5
    frozen["result"]["home_score"] = {"mean": 100}
    frozen["result"]["away_score"] = {"mean": 90}
    store.save_forecast(frozen)
    variants = {}
    for name, changes in {
        "wrong-tipoff": {"tipoff": (now + timedelta(hours=2)).isoformat()},
        "wrong-home": {"home_id": "9"},
        "reversed-teams": {"home_id": "2", "away_id": "4"},
        "missing-dataset": {"dataset_id": "not-imported"},
    }.items():
        foreign = deepcopy(frozen)
        foreign["id"] = name
        foreign["request"].update(changes)
        store.save_forecast(foreign)
        variants[name] = foreign
    synthetic = deepcopy(frozen)
    synthetic["id"] = "synthetic-forecast"
    synthetic["synthetic"] = True
    store.save_forecast(synthetic)
    variants["synthetic-forecast"] = synthetic
    synthetic_source = numeric_dataset(synthetic=True)
    synthetic_key = store.import_dataset(Dataset.model_validate(synthetic_source))
    false_label = deepcopy(frozen)
    false_label["id"] = "synthetic-source-real-forecast-label"
    false_label["request"]["dataset_id"] = synthetic_key
    store.save_forecast(false_label)
    variants[false_label["id"]] = false_label
    valid_ticket = evaluate_sgp(frozen, legs(), as_of=now)
    valid_ticket.update(id="matching-ticket", created_at=now.isoformat())
    store.save_sgp(valid_ticket)
    for fid, foreign in variants.items():
        ticket = deepcopy(valid_ticket)
        ticket.update(id=f"ticket-{fid}", forecast_id=fid)
        store.save_sgp(ticket)
    tip = frozen["request"]["tipoff"]
    slate = {"date": now.strftime("%Y-%m-%d"), "timezone": "America/New_York",
             "status": "AVAILABLE", "retrieved_at": now.isoformat(),
             "source_url": "fixture://tonight-test-only", "games": [
                 {"id": "401-test-1", "home": {"id": "4", "name": "Fixture Chicago", "abbreviation": "CHI"},
                  "away": {"id": "2", "name": "Fixture Boston", "abbreviation": "BOS"},
                  "tipoff": tip, "status": "scheduled", "status_detail": "Fixture scheduled game", "completed": False},
                 {"id": "401-test-2", "home": {"id": "9", "name": "Fixture Golden State", "abbreviation": "GS"},
                  "away": {"id": "6", "name": "Fixture Dallas", "abbreviation": "DAL"},
                  "tipoff": tip, "status": "scheduled", "status_detail": "Fixture scheduled game", "completed": False}]}
    calls = []
    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return deepcopy(slate)
    monkeypatch.setattr("nba_quant.api.load_slate", load)
    with TestClient(app) as client:
        yield client, slate, frozen, valid_ticket, calls


def test_tonight_game_receives_only_exact_associated_real_source_forecast(tonight):
    client, slate, frozen, _, _ = tonight
    response = client.get("/api/tonight")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["date"] == slate["date"] and result["source_url"] == slate["source_url"]
    assert len(result["games"]) == 2
    game = next(g for g in result["games"] if g["id"] == "401-test-1")
    assert [f["id"] for f in game["sgp"]["forecasts"]] == [frozen["id"]]
    assert game["home"]["id"] == "4" and game["away"]["id"] == "2"
    assert game["tipoff"] == frozen["request"]["tipoff"]


def test_tonight_ticket_history_is_bound_to_matching_forecast_only(tonight):
    client, _, frozen, ticket, _ = tonight
    result = client.get("/api/tonight").json()
    first, second = result["games"]
    assert [t["id"] for t in first["sgp"]["tickets"]] == [ticket["id"]]
    assert first["sgp"]["tickets"][0]["forecast_id"] == frozen["id"]
    assert second["sgp"]["forecasts"] == second["sgp"]["tickets"] == []
    assert client.get(f"/api/forecasts/{frozen['id']}").json() == frozen


def test_tonight_deduplicates_history_and_does_not_invent_offered_prices(tonight):
    client, _, _, _, _ = tonight
    games = client.get("/api/tonight").json()["games"]
    assert games[0]["history"] == {"home_games": 3, "away_games": 3}
    assert games[1]["history"] == {"home_games": 0, "away_games": 0}
    for game in games:
        assert game["sgp"]["offered_props"] == []
        assert game["sgp"]["block_reasons"]
        assert game["sgp"]["status"] != "ACTIONABLE"


def test_tonight_refresh_reads_source_and_cross_origin_refresh_is_blocked(tonight):
    client, _, _, _, calls = tonight
    response = client.post("/api/tonight/refresh")
    assert response.status_code == 200, response.text
    assert calls
    before = len(calls)
    denied = client.post("/api/tonight/refresh", headers={"Origin": "https://foreign.example"})
    assert denied.status_code == 403
    assert len(calls) == before


def test_unavailable_schedule_has_no_historical_or_synthetic_game_fallback(tonight, monkeypatch):
    client, slate, _, _, _ = tonight
    unavailable = {**slate, "status": "UNAVAILABLE", "games": [], "reason": "Fixture source outage"}
    monkeypatch.setattr("nba_quant.api.load_slate", lambda *args, **kwargs: unavailable)
    response = client.get("/api/tonight")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "UNAVAILABLE"
    assert result["games"] == []
    assert result["saved_tickets_count"] == 0
    assert result["actionable"] is False


@pytest.mark.parametrize("status,completed", [("in", False), ("post", True)])
def test_in_progress_or_completed_game_explicitly_closes_pregame_window(tonight, monkeypatch, status, completed):
    client, slate, _, _, _ = tonight
    closed = deepcopy(slate)
    closed["games"][0].update(status=status, completed=completed)
    monkeypatch.setattr("nba_quant.api.load_slate", lambda *args, **kwargs: closed)
    response = client.get("/api/tonight")
    assert response.status_code == 200, response.text
    game = response.json()["games"][0]
    assert any("pregame window has closed" in reason for reason in game["sgp"]["block_reasons"])
    assert response.json()["actionable"] is False
