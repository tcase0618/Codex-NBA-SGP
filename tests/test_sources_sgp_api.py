"""Independent SGP API checks against immutable fictional sample snapshots."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from nba_quant.api import create_app
from nba_quant.sgp import leg_key
from nba_quant.schemas import Game
from pydantic import ValidationError
from test_provenance import raw_dataset
from test_sgp import fixture, legs, quote


@pytest.fixture
def client_and_forecast(tmp_path, monkeypatch):
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    app = create_app(tmp_path / "sgp-api.sqlite")
    frozen, now = fixture(synthetic=True)
    app.state.store.save_forecast(frozen)
    with TestClient(app) as client:
        yield client, frozen, now


def payload(forecast, now, selections=None, combined_quote=None):
    return {"forecast_id": forecast["id"], "legs": [leg.model_dump() for leg in (selections or legs())],
            "as_of": now.isoformat(), "quote": combined_quote}


def test_sgp_api_uses_shared_snapshot_without_mutating_forecast(client_and_forecast):
    client, f, now = client_and_forecast
    original = client.get(f"/api/forecasts/{f['id']}").json()
    response = client.post("/api/sgp/evaluate", json=payload(f, now))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["joint_probability"] == .5
    assert result["dependence"]["independence_product_diagnostic"] == .25
    assert result["dependence"]["lift_vs_independence"] == 2
    assert result["quote_status"] == "MISSING"
    assert result["expected_return_per_unit"] is None
    assert result["synthetic"] is True and result["actionable"] is False
    assert result["use_status"] == "RESEARCH_ONLY"
    assert client.get(f"/api/forecasts/{f['id']}").json() == original
    assert result["provenance"]["input_hash"] == f["input_hash"]
    assert result["scenario_robustness"]["status"] == "UNAVAILABLE"
    assert client.get(f"/api/sgp/{result['id']}").json() == result
    assert client.put(f"/api/sgp/{result['id']}", json={"actionable": True}).status_code == 405
    with client.app.state.store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE sgp_evaluations SET payload='{}' WHERE id=?", (result["id"],))


def test_synthetic_exact_fanduel_quote_does_not_enable_betting(client_and_forecast):
    client, f, now = client_and_forecast
    q = quote(f, legs(), now)
    q["sportsbook"] = "fanduel"
    response = client.post("/api/sgp/evaluate", json=payload(f, now, combined_quote=q))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["quote_status"] == "VALID"
    assert result["expected_return_per_unit"] is None
    assert result["pricing_status"] == "NOT_MEASURABLE"
    assert result["actionable"] is False
    assert any("Synthetic forecast" in reason for reason in result["betting_block_reasons"])


def test_mismatched_combined_quote_is_not_substituted_by_individual_prices(client_and_forecast):
    client, f, now = client_and_forecast
    q = quote(f, legs(), now)
    q["leg_keys"] = [leg_key(legs()[0]), leg_key(legs()[0])]
    response = client.post("/api/sgp/evaluate", json=payload(f, now, combined_quote=q))
    assert response.status_code == 200
    result = response.json()
    assert result["quote_status"] == "INVALID"
    assert result["expected_return_per_unit"] is None
    assert result["break_even_probability"] is None


@pytest.mark.parametrize("mutation", [
    {"forecast_id": "unknown-forecast"},
    {"as_of": "2099-01-01T00:00:00+00:00"},
    {"as_of": "2026-01-01T00:00:00"},
    {"legs": [{"player_id": "missing", "stat": "pts", "side": "over", "line": 20.5},
              {"player_id": "p1", "stat": "reb", "side": "over", "line": 5.5}]},
    {"legs": [{"player_id": "p1", "stat": "pts", "side": "over", "line": 20},
              {"player_id": "p1", "stat": "reb", "side": "over", "line": 5.5}]},
    {"legs": [{"player_id": "p1", "stat": "pts", "side": "over", "line": 20.5},
              {"player_id": "p1", "stat": "pra", "side": "over", "line": 30.5}]},
])
def test_invalid_sgp_requests_fail_and_leave_frozen_history_unchanged(client_and_forecast, mutation):
    client, f, now = client_and_forecast
    request = {**payload(f, now), **mutation}
    before = client.get(f"/api/forecasts/{f['id']}").json()
    response = client.post("/api/sgp/evaluate", json=request)
    assert response.status_code in {404, 422}, response.text
    assert client.get(f"/api/forecasts/{f['id']}").json() == before


def test_cross_origin_sgp_evaluation_is_blocked(client_and_forecast):
    client, f, now = client_and_forecast
    response = client.post("/api/sgp/evaluate", json=payload(f, now), headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403


def test_source_readiness_without_subscription_or_authentic_history_is_explicit(client_and_forecast):
    client, _, _ = client_and_forecast
    response = client.get("/api/sources")
    assert response.status_code == 200, response.text
    sources = response.json()
    assert sources["target_sportsbook"] == "FanDuel"
    assert sources["history"] == {"snapshots": 0, "games": 0, "teams": 0, "players": 0, "latest_game_at": None}
    assert sources["odds"]["configured"] is False
    assert sources["odds"]["status"] == sources["odds"]["combined_sgp_quote"] == "UNAVAILABLE"
    assert sources["readiness"]["actionable"] is False
    assert sources["readiness"]["status"] == "RESEARCH_ONLY"
    assert any("subscription" in r for r in sources["readiness"]["reasons"])
    assert any("historical" in r for r in sources["readiness"]["reasons"])


def test_source_readiness_never_exposes_configured_credential(client_and_forecast, monkeypatch):
    client, _, _ = client_and_forecast
    fake_secret = "fictional-credential-test-only-12345"
    monkeypatch.setenv("THE_ODDS_API_KEY", fake_secret)
    response = client.get("/api/sources")
    assert response.status_code == 200
    assert fake_secret not in response.text
    assert response.json()["odds"]["configured"] is True
    assert response.json()["odds"]["status"] == "CONFIGURED_NOT_VERIFIED"
    assert response.json()["readiness"]["actionable"] is False


def test_declared_source_minute_precision_preserves_values_without_faking_reconciliation():
    raw = raw_dataset()["games"][0]
    raw["boxes"][0]["minutes"] = 39
    with pytest.raises(ValidationError, match="Team minutes"):
        Game.model_validate(raw)
    raw["minutes_precision_seconds"] = 60
    parsed = Game.model_validate(raw)
    assert parsed.boxes[0].minutes == 39
    assert sum(b.minutes for b in parsed.boxes if b.team_id == "home") == 239
    raw["boxes"][0]["minutes"] = 39.5
    with pytest.raises(ValidationError, match="integer source minutes"):
        Game.model_validate(raw)


def test_declared_minute_precision_does_not_permit_arbitrary_deficits():
    raw = raw_dataset()["games"][0]
    raw["minutes_precision_seconds"] = 60
    raw["boxes"][0]["minutes"] = 30
    with pytest.raises(ValidationError, match="Team minutes"):
        Game.model_validate(raw)
