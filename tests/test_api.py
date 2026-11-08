from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nba_quant.api import create_app


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    app = create_app(tmp_path_factory.mktemp("api") / "research.sqlite")
    with TestClient(app) as client:
        response = client.post("/api/demo")
        assert response.status_code == 200, response.text
        key = response.json()["dataset_id"]
        exported = client.get(f"/api/datasets/{key}/export")
        assert exported.status_code == 200
        yield client, key, exported.json()


def request_for(key, dataset, game=None):
    now = datetime.now(timezone.utc)
    return {"dataset_id": key, "home_id": game["home_id"] if game else dataset["teams"][0]["id"],
            "away_id": game["away_id"] if game else dataset["teams"][1]["id"],
            "tipoff": game["tipoff"] if game else (now + timedelta(days=1)).isoformat(),
            "cutoff": (datetime.fromisoformat(game["tipoff"]) - timedelta(hours=1)).isoformat() if game else now.isoformat(),
            "simulations": 20, "seed": 77}


def test_empty_database_reports_no_data_and_no_measurable_accuracy(tmp_path):
    with TestClient(create_app(tmp_path / "empty.sqlite")) as client:
        response = client.get("/api/status")
        assert response.status_code == 200
        assert response.json()["status"] == "NO DATA"
        assert response.json()["forecasts_count"] == 0
        assert client.get("/api/evaluation").json()["status"] == "NOT MEASURABLE"
        catalog = client.get("/api/metrics?scope=player").json()
        assert catalog["coverage"] == {"available": 0, "total": 100}
        assert all(m["value"] is None and m["status"] == "UNAVAILABLE" for m in catalog["metrics"])


def test_demo_import_status_export_reimport_is_idempotent(api):
    client, key, dataset = api
    status = client.get("/api/status").json()
    assert status["live"] == status["tracking"] == "UNAVAILABLE"
    assert status["execution"] == "NONE"
    assert sum(status["catalog_counts"].values()) == 440
    assert dataset["synthetic"] is True and len(dataset["games"]) == 30
    assert len(dataset["players"]) == 20
    response = client.post("/api/datasets", json=dataset)
    assert response.status_code == 200 and response.json()["dataset_id"] == key
    assert len(client.get("/api/status").json()["datasets"]) == 1


def test_forecast_persists_and_export_roundtrips_immutable_snapshot(api):
    client, key, dataset = api
    request = request_for(key, dataset)
    response = client.post("/api/forecasts", json=request)
    assert response.status_code == 200, response.text
    forecast = response.json()
    fid = forecast["id"]
    assert forecast["synthetic"] is True
    assert len(forecast["input_hash"]) == 64
    assert len(forecast["result"]["score_samples"]) == 20
    assert len(forecast["result"]["players"]) == 20
    assert forecast["result"]["representative_game"]["events"]
    assert client.get(f"/api/forecasts/{fid}").json() == forecast
    exported = client.get(f"/api/forecasts/{fid}/export")
    assert exported.status_code == 200 and exported.json() == forecast
    assert "attachment" in exported.headers["content-disposition"]
    assert fid in {f["id"] for f in client.get("/api/forecasts").json()}
    assert client.put(f"/api/forecasts/{fid}", json={"id": fid}).status_code == 405
    assert client.get(f"/api/forecasts/{fid}").json() == forecast


def test_matching_outcome_is_separate_immutable_synthetic_evaluation(api):
    client, key, dataset = api
    game = dataset["games"][-1]
    response = client.post("/api/forecasts", json=request_for(key, dataset, game))
    assert response.status_code == 200, response.text
    frozen = response.json()
    fid = frozen["id"]
    payload = {"dataset_id": key, "game_id": game["id"]}
    scored = client.post(f"/api/forecasts/{fid}/outcome", json=payload)
    assert scored.status_code == 200, scored.text
    assert scored.json()["cohort"] == "synthetic"
    assert client.post(f"/api/forecasts/{fid}/outcome", json=payload).status_code == 409
    assert client.get(f"/api/forecasts/{fid}").json() == frozen
    evaluation = client.get("/api/evaluation").json()
    assert evaluation["cohorts"]["synthetic"]["count"] >= 1
    assert "prospective" not in evaluation["cohorts"]
    assert evaluation["cohorts"]["synthetic"]["metrics"]["brier"] >= 0


def test_profiles_catalog_and_unavailable_tracking(api):
    client, key, dataset = api
    profiles = client.get("/api/profiles", params={"dataset_id": key})
    assert profiles.status_code == 200
    assert len(profiles.json()["players"]) == 20 and len(profiles.json()["teams"]) == 2
    metrics = client.get("/api/metrics", params={"dataset_id": key, "scope": "player",
                                               "entity_id": dataset["players"][0]["id"]}).json()
    assert metrics["coverage"]["total"] == 100 and metrics["coverage"]["available"] > 0
    gravity = [m for m in client.get("/api/metrics?scope=SG").json()["metrics"] if "gravity" in m["label"]]
    assert gravity and all(m["status"] == "UNAVAILABLE" and m["value"] is None for m in gravity)
    unknown = client.get("/api/metrics", params={"dataset_id": key, "entity_id": "unknown-player"}).json()
    assert unknown["coverage"]["available"] == 0
    assert client.get("/api/metrics?scope=not-a-scope").status_code == 422


@pytest.mark.parametrize("mutation", [
    {"dataset_id": "missing-dataset"},
    {"home_id": "unknown-team"},
    {"simulations": 0},
    {"cutoff": "2099-01-01T00:00:00+00:00"},
    {"cutoff": "2026-01-01T00:00:00"},
    {"scenarios": [{"player_id": "unknown-player", "active_probability": 0}]},
])
def test_invalid_forecasts_fail_without_persisting(api, mutation):
    client, key, dataset = api
    before = client.get("/api/status").json()["forecasts_count"]
    request = {**request_for(key, dataset), **mutation}
    response = client.post("/api/forecasts", json=request)
    assert response.status_code == 422, response.text
    assert client.get("/api/status").json()["forecasts_count"] == before


def test_invalid_box_scores_are_rejected_atomically(api):
    client, _, dataset = api
    tampered = deepcopy(dataset)
    tampered["games"][0]["boxes"][0]["pts"] += 1
    before = len(client.get("/api/status").json()["datasets"])
    assert client.post("/api/datasets", json=tampered).status_code == 422
    assert len(client.get("/api/status").json()["datasets"]) == before


def test_malicious_labels_remain_json_data_and_response_has_security_headers(api):
    client, _, dataset = api
    imported = deepcopy(dataset)
    label = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    imported["source"] = label
    imported["teams"][0]["name"] = label
    imported["players"][0]["name"] = label
    response = client.post("/api/datasets", json=imported)
    assert response.status_code == 200
    key = response.json()["dataset_id"]
    exported = client.get(f"/api/datasets/{key}/export")
    assert exported.json()["players"][0]["name"] == label
    assert exported.headers["content-type"].startswith("application/json")
    assert exported.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in exported.headers["content-security-policy"]


@pytest.mark.parametrize("headers", [
    {"Origin": "https://untrusted.example"},
    {"Origin": "null"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
])
def test_cross_origin_writes_are_blocked(api, headers):
    client, _, dataset = api
    response = client.post("/api/datasets", json=dataset, headers=headers)
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_same_origin_write_is_allowed_and_unknown_forecast_is_404(api):
    client, _, dataset = api
    assert client.post("/api/datasets", json=dataset, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/forecasts/unknown-id").status_code == 404
