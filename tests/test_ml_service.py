from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

from nba_quant.api import create_app
from nba_quant.ml_service import MLRequest, MLService
from nba_quant.store import Store
from nba_quant.store import canonical, digest


def test_ml_audit_records_are_append_only(tmp_path):
    service = MLService(Store(tmp_path / "test.sqlite"), tmp_path / "models")
    item = service.append("ml_operations", {"status": "FAILED", "reason": "missing features"})
    with service.store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE ml_operations SET payload='{}' WHERE id=?", (item["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM ml_operations WHERE id=?", (item["id"],))
    assert service.history("ml_operations")[0]["reason"] == "missing features"


def test_request_rejects_future_cutoff_and_postgame():
    now = datetime.now(timezone.utc)
    fields = dict(model_id="test",game_id="g",home_id="a",away_id="b",team_id="a",target="pts")
    with pytest.raises(ValueError):
        MLRequest(**fields, tipoff=now-timedelta(hours=1), as_of=now-timedelta(hours=2))
    with pytest.raises(ValueError):
        MLRequest(**fields, tipoff=now+timedelta(hours=2), as_of=now+timedelta(hours=1))


def test_no_models_or_data_never_produces_forecasts(tmp_path, monkeypatch):
    monkeypatch.setenv("NBA_QUANT_ML_ROOT", str(tmp_path / "models"))
    with TestClient(create_app(tmp_path / "api.sqlite")) as client:
        result = client.get("/api/ml/v1/status")
        assert result.status_code == 200
        assert all(not t["production_eligible"] for t in result.json()["targets"])
        assert all(t["data_rows"] == 0 for t in result.json()["targets"])
        assert client.get("/api/ml/v1/models").json() == {"runs": [], "incomplete_runs": [], "api_version": "ml/v1"}
        assert client.get("/api/ml/v1/backtests").json() == {"reports": [], "failures": [], "api_version": "ml/v1"}
        assert client.get("/api/ml/v1/history").json()["predictions"] == []
        assert client.post("/api/ml/v1/reconcile").json()["recorded"] == 0


def test_rejected_inference_retains_failure_audit(tmp_path):
    service = MLService(Store(tmp_path / "test.sqlite"), tmp_path / "models")
    now = datetime.now(timezone.utc)
    request = MLRequest(model_id="missing", game_id="g",home_id="a",away_id="b",team_id="a",
                        target="pts", tipoff=now+timedelta(hours=2),as_of=now-timedelta(seconds=1))
    with pytest.raises((ValueError, FileNotFoundError)):
        service.predict(request)
    assert len(service.history("ml_operations")) == 1
    assert not service.history()


def test_backtest_report_corruption_is_isolated(tmp_path):
    service = MLService(Store(tmp_path / "test.sqlite"), tmp_path / "models")
    directory = tmp_path / "models" / "backtests"
    directory.mkdir(parents=True)
    report = {"target": "pts", "status": "RESEARCH_ONLY", "folds": []}
    good = directory / ("backtest-" + digest(report) + ".json")
    good.write_text(canonical(report))
    bad = directory / ("backtest-" + "0"*64 + ".json")
    bad.write_text(canonical({**report, "fabricated_accuracy": 1}))
    result = service.backtests()
    assert len(result["reports"]) == 1
    assert result["reports"][0]["target"] == "pts"
    assert len(result["failures"]) == 1
    assert result["failures"][0]["status"] == "CORRUPT"
    assert "fabricated_accuracy" not in canonical(result)


def test_reconciliation_checks_exact_identity_and_is_idempotent(tmp_path, monkeypatch):
    service = MLService(Store(tmp_path / "test.sqlite"), tmp_path / "models")
    now = datetime.now(timezone.utc)
    tipoff = now-timedelta(days=2)
    game = {"id": "g", "home_id": "a", "away_id": "b", "tipoff": tipoff.isoformat(),
        "completed_at": (tipoff+timedelta(hours=3)).isoformat(), "known_at": (tipoff+timedelta(hours=4)).isoformat(),
        "home_score": 100, "away_score": 90, "boxes": []}
    req = {"game_id": "g", "home_id": "b", "away_id": "a", "tipoff": tipoff.isoformat(),
           "team_id": "a", "target": "win"}
    pred = {"id": "frozen", "created_at": (tipoff-timedelta(hours=1)).isoformat(), "request": req}
    monkeypatch.setattr(service.store, "datasets", lambda: [{"synthetic": False, "games": [game]}])
    monkeypatch.setattr(service, "history", lambda *a, **kw: [pred])
    assert service.reconcile()["recorded"] == 0
    req.update(home_id="a", away_id="b")
    assert service.reconcile()["recorded"] == 1
    assert service.reconcile()["recorded"] == 0
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ml_outcomes").fetchone()[0] == 1


def test_future_outcomes_cannot_be_reconciled(tmp_path, monkeypatch):
    service = MLService(Store(tmp_path / "test.sqlite"), tmp_path / "models")
    now = datetime.now(timezone.utc)
    game = {"id": "g", "home_id": "a", "away_id": "b", "tipoff": (now-timedelta(hours=1)).isoformat(),
        "completed_at": (now+timedelta(hours=1)).isoformat(), "known_at": (now+timedelta(hours=2)).isoformat()}
    pred = {"id": "frozen", "created_at": (now-timedelta(hours=2)).isoformat(),
        "request": {"game_id": "g", "home_id": "a", "away_id": "b", "tipoff": game["tipoff"]}}
    monkeypatch.setattr(service.store, "datasets", lambda: [{"synthetic": False, "games": [game]}])
    monkeypatch.setattr(service, "history", lambda *a, **kw: [pred])
    assert service.reconcile()["recorded"] == 0


def test_schedule_observed_after_cutoff_cannot_leak_into_forecast(tmp_path, monkeypatch):
    from nba_quant import ml_engine, slate
    service = MLService(Store(tmp_path / "test.sqlite"), tmp_path / "models")
    now = datetime.now(timezone.utc)
    tipoff = now+timedelta(hours=2)
    request = MLRequest(model_id="fixture", game_id="g", home_id="a", away_id="b", team_id="a",
        target="win", tipoff=tipoff, as_of=now-timedelta(minutes=1))
    monkeypatch.setattr(ml_engine, "load_manifest", lambda *a: {"target": "win"})
    monkeypatch.setattr(slate, "load_slate", lambda: {"status": "AVAILABLE", "stale": False,
        "retrieved_at": now.isoformat(), "games": [{"id": "g", "home": {"id": "a"},
        "away": {"id": "b"}, "tipoff": tipoff.isoformat()}]})
    with pytest.raises(ValueError, match="Schedule observation follows"):
        service.predict(request)
    assert service.history("ml_operations")[0]["status"] == "FAILED"
    assert not service.history()
