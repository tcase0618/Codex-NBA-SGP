"""Fixture-only source-bound draft construction and market import checks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from nba_quant.api import create_app
from nba_quant.schemas import Dataset
from test_provenance import raw_dataset


@pytest.fixture
def board(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    game = {"id": "401-fixture", "home": {"id": "home", "name": "Fixture Home"},
            "away": {"id": "away", "name": "Fixture Away"},
            "tipoff": (now + timedelta(hours=1)).isoformat(), "phase": "pre", "completed": False}
    slate = {"date": now.strftime("%Y-%m-%d"), "timezone": "America/New_York", "status": "AVAILABLE",
             "stale": False, "retrieved_at": now.isoformat(), "source_url": "fixture://slate", "games": [game]}
    raw = raw_dataset()
    raw["synthetic"] = False
    players = [{**p, "team_id": "home" if p["id"].startswith("home") else "away",
                "availability": "UNVERIFIED", "roster_status": "active", "injury_status": "not verified",
                "source": {"retrieved_at": (now - timedelta(seconds=1)).isoformat(), "url": "fixture://roster"}}
               for p in raw["players"]]
    context = {"date": slate["date"], "status": "AVAILABLE", "hash": "fixture-context-hash",
        "retrieved_at": now.isoformat(), "stale": False, "errors": [], "counts": {"players": len(players)},
        "games": {game["id"]: {"id": game["id"], "home_id": "home", "away_id": "away",
                                    "tipoff": game["tipoff"], "status": "AVAILABLE", "players": players}}}
    monkeypatch.setattr("nba_quant.api.load_slate", lambda *a, **k: deepcopy(slate))
    monkeypatch.setattr("nba_quant.api.load_context", lambda *a, **k: deepcopy(context))
    monkeypatch.setattr("nba_quant.api.refresh_context", lambda *a, **k: deepcopy(context))
    # Fixture jobs must not parse the user's growing live exchange archive.
    # Both the API and lazy construction reader are isolated from real quotes.
    no_exchange_quotes = lambda *a, **k: {"status": "NO_FIXTURE_QUOTES", "offers": [], "markets": []}
    monkeypatch.setattr("nba_quant.api.load_polymarket_odds", no_exchange_quotes)
    monkeypatch.setattr("nba_quant.polymarket.load_polymarket_odds", no_exchange_quotes)
    app = create_app(tmp_path / "board-fixture.sqlite")
    app.state.store.import_dataset(Dataset.model_validate(raw))
    with TestClient(app) as client:
        yield client, game, context, slate, now


def market_payload(game, now):
    return {"game_id": game["id"], "home_id": "home", "away_id": "away", "tipoff": game["tipoff"],
            "sportsbook": "fanduel", "observed_at": (now - timedelta(seconds=2)).isoformat(),
            "source_reference": "Explicit fictional source, unit test only",
            "legs": [{"player_id": "home-0", "team_id": "home", "stat": "pts",
                      "side": "over", "line": 10.5, "decimal_odds": 1.8}]}


def test_draft_build_is_persisted_immutable_and_visible_in_tonight(board):
    client, game, _, _, _ = board
    response = client.post(f"/api/games/{game['id']}/drafts")
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert [len(f["legs"]) for f in bundle["families"]] == [3, 5, 8]
    assert all(f["status"] == "DRAFT_UNPRICED" and f["joint_probability"] is None for f in bundle["families"])
    assert bundle["actionable"] is False
    assert bundle["context_hash"] == "fixture-context-hash"
    stored = client.app.state.store.latest_drafts(game["id"], game["tipoff"])
    assert stored == bundle
    with client.app.state.store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE draft_bundles SET payload='{}' WHERE id=?", (bundle["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM draft_bundles WHERE id=?", (bundle["id"],))
    tonight = client.get("/api/tonight").json()["games"][0]
    assert tonight["sgp"]["draft_bundle_id"] == bundle["id"]
    assert tonight["sgp"]["draft_families"] == bundle["families"]
    assert tonight["sgp"]["draft_stale"] is False


def test_exact_manual_market_is_caller_assertion_and_constructor_preserves_actual_halfline(board):
    client, game, _, _, now = board
    response = client.post("/api/markets/import", json=market_payload(game, now))
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["authenticity"] == "CALLER_ASSERTION_UNVERIFIED" and receipt["actionable"] is False
    stored = client.app.state.store.latest_market(game["id"], game["tipoff"])
    assert stored["offered_rows"][0]["authenticity"] == "CALLER_ASSERTION_UNVERIFIED"
    assert stored["offered_rows"][0]["source_sha256"]
    assert stored["offered_rows"][0]["line"] == 10.5
    built = client.post(f"/api/games/{game['id']}/drafts")
    assert built.status_code == 200, built.text
    candidate = next(c for c in built.json()["candidates"] if c["player_id"] == "home-0" and c["stat"] == "pts")
    assert candidate["threshold_kind"] == "OFFERED_LINE"
    assert candidate["side"] == "over" and candidate["line"] == 10.5
    assert candidate["offer"]["decimal_odds"] == 1.8
    assert built.json()["joint_probability"] is None


@pytest.mark.parametrize("mutation", [
    {"home_id": "wrong-home"}, {"away_id": "wrong-away"},
    {"tipoff": "2099-01-01T00:00:00+00:00"},
    {"observed_at": "2025-01-01T00:00:00+00:00"},
    {"observed_at": "2099-01-01T00:00:00+00:00"},
    {"source_reference": "     "}, {"sportsbook": "different-book"},
])
def test_market_event_time_and_source_failures_do_not_persist(board, mutation):
    client, game, _, _, now = board
    response = client.post("/api/markets/import", json={**market_payload(game, now), **mutation})
    assert response.status_code == 422, response.text
    assert client.app.state.store.latest_market(game["id"], game["tipoff"]) is None


@pytest.mark.parametrize("mutation", [
    {"decimal_odds": True}, {"decimal_odds": 1}, {"line": True}, {"line": 10},
    {"player_id": "not-current-roster"}, {"team_id": "away"}, {"team_id": "foreign"},
])
def test_invalid_price_push_or_player_assignment_is_rejected(board, mutation):
    client, game, _, _, now = board
    payload = market_payload(game, now)
    payload["legs"][0].update(mutation)
    response = client.post("/api/markets/import", json=payload)
    assert response.status_code == 422, response.text
    assert client.app.state.store.latest_market(game["id"], game["tipoff"]) is None


@pytest.mark.parametrize("change", ["stale", "missing_game", "wrong_tipoff", "wrong_home", "partial_game"])
def test_current_context_must_be_fresh_complete_and_bound_to_exact_event(board, change):
    client, game, context, _, now = board
    if change == "stale":
        context["stale"] = True
    elif change == "missing_game":
        context["games"] = {}
    elif change == "wrong_tipoff":
        context["games"][game["id"]]["tipoff"] = (now + timedelta(hours=2)).isoformat()
    elif change == "wrong_home":
        context["games"][game["id"]]["home_id"] = "wrong-home"
    else:
        context["games"][game["id"]]["status"] = "PARTIAL"
    response = client.post(f"/api/games/{game['id']}/drafts")
    assert response.status_code == 422, response.text
    imported = client.post("/api/markets/import", json=market_payload(game, now))
    assert imported.status_code == 422, imported.text
    assert client.app.state.store.latest_drafts(game["id"], game["tipoff"]) is None


def test_background_build_completes_against_mock_sources_and_persists_family(board):
    client, game, _, _, _ = board
    response = client.post("/api/tonight/build")
    assert response.status_code == 202, response.text
    key = response.json()["job_id"]
    deadline = time.monotonic() + 3
    job = response.json()
    while job["status"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(.01)
        job = client.get(f"/api/build-jobs/{key}").json()
    assert job["status"] == "COMPLETE", job
    assert job["games_built"] == 1 and job["errors"] == []
    assert client.app.state.store.latest_drafts(game["id"], game["tipoff"]) is not None


def test_stale_schedule_and_cross_origin_build_are_rejected(board):
    client, game, _, slate, now = board
    slate["stale"] = True
    assert client.post(f"/api/games/{game['id']}/drafts").status_code == 422
    assert client.post("/api/markets/import", json=market_payload(game, now)).status_code == 422
    assert client.post("/api/tonight/build", headers={"Origin": "https://foreign.example"}).status_code == 403
