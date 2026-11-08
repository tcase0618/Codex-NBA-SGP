import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from nba_quant import ml_engine as engine


def fixture(target="pts", groups=120, entities=3):
    rows = []
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(groups):
        cutoff = start + timedelta(days=i, hours=12)
        for j in range(entities):
            binary = target == "win"
            label = (i + j) % 2 if binary else 10.
            features = {"signal": float(i), "team_win_rate": .5, "team_win_recent3_rate": .5} if binary else {
                "signal": float(i), "player_pts_mean": 9., "player_pts_recent3_mean": 8.}
            rows.append({"game_id": str(i), "scope": "team" if binary else "player", "entity_id": f"p{j}",
                "team_id": "home" if j % 2 else "away", "opponent_id": "other",
                "tipoff": (cutoff + timedelta(hours=1)).isoformat(), "cutoff": cutoff.isoformat(),
                "label_completed_at": (cutoff + timedelta(hours=4)).isoformat(),
                "label_known_at": (cutoff + timedelta(hours=5)).isoformat(),
                "features": features, "targets": {target: label}, "production_eligible": False,
                "point_in_time_mode": "strict", "provenance": {"synthetic": True}})
    return {"rows": rows, "audit": {"mode": "synthetic_software_fixture"}}


def test_baseline_training_persists_and_reloads_reproducible_predictions(tmp_path):
    run = engine.train_run(fixture(), "pts", tmp_path, backends=())
    assert run["status"] == "SHADOW" and not run["production_eligible"]
    assert run["champion"] == "historical_baseline"
    assert run["selection_scope"] == "TUNING_ONLY"
    assert run["evaluation_cohort"] == "synthetic_software_fixture"
    assert run["metrics"]["mae"] == 1
    assert run["calibration"]["radius"] == 1
    assert run["metrics"]["interval_coverage"] == 1
    rows = [{k: v for k, v in row.items() if k not in {"targets", "label"}} for row in fixture()["rows"][-3:]]
    result = engine.predict_run(tmp_path, run["id"], rows)
    assert [r["prediction"] for r in result["predictions"]] == [9, 9, 9]
    assert result["predictions"][0]["interval"] == [8, 10]
    assert engine.load_manifest(tmp_path, run["id"])["id"] == run["id"]
    assert engine.list_runs(tmp_path)[0]["id"] == run["id"]
    assert engine.registry_events(tmp_path)[0]["action"] == "shadow"
    assert run["feature_training_summary"]["signal"]["max"] < 100
    assert run["training_source_code_sha256"]["ml_engine.py"]


def test_candidate_override_does_not_reuse_champion_calibration(tmp_path):
    run = engine.train_run(fixture(), "pts", tmp_path, backends=())
    response = engine.predict_run(tmp_path, run["id"], fixture()["rows"][:2], candidate="recent_baseline")
    assert response["candidate"] == "recent_baseline"
    assert response["calibration"]["status"] == "UNAVAILABLE_CANDIDATE_NOT_CALIBRATED"
    assert all(r["interval"] is None and r["prediction"] == 8 for r in response["predictions"])
    with pytest.raises(ValueError, match="candidate is unavailable"):
        engine.predict_run(tmp_path, run["id"], fixture()["rows"][:1], candidate="xgboost")


def test_champion_selection_never_uses_final_test_even_when_challenger_wins_it(tmp_path, monkeypatch):
    def fake_fit(backend, directory, columns, train, tune, kind, *_args):
        (directory / f"{backend}.txt").write_text("synthetic adapter fixture")
        def predict(rows):
            return np.asarray([10 if backend == "autogluon" and r["features"]["signal"] < 102 or backend == "xgboost" and r["features"]["signal"] >= 102 else 9 if backend == "xgboost" else 0 for r in rows])
        return {"backend": backend, "path": f"{backend}.txt"}, predict
    monkeypatch.setattr(engine, "_external_fit", fake_fit)
    run = engine.train_run(fixture(), "pts", tmp_path)
    assert run["champion"] == "autogluon"
    assert run["candidates"]["autogluon"]["tuning_metrics"]["mae"] == 0
    assert run["candidates"]["xgboost"]["raw_final_test_metrics"]["mae"] == 0
    assert run["metrics"]["mae"] == 10


def test_missing_optional_backends_are_explicit_and_baselines_remain_usable(tmp_path, monkeypatch):
    def missing(*_args):
        raise ModuleNotFoundError("Optional package is unavailable in this interpreter")
    monkeypatch.setattr(engine, "_external_fit", missing)
    run = engine.train_run(fixture(), "pts", tmp_path)
    assert run["champion"] == "historical_baseline"
    assert len(run["errors"]) == 2
    assert all(run["candidates"][b]["status"] == "UNAVAILABLE_OR_FAILED" for b in ("autogluon", "xgboost"))


def test_conformal_uses_finite_sample_order_statistic_and_no_silent_infinite_interval():
    fitted = engine._conformal(np.zeros(9), np.arange(9), .1)
    assert fitted["radius"] == 8 and fitted["order_statistic"] == 9
    assert engine._conformal([0], [1], .1)["status"] == "INSUFFICIENT_FINITE_SAMPLE_SUPPORT"


def test_binary_platt_calibration_and_reliability_bins_are_dedicated_and_bounded(tmp_path):
    run = engine.train_run(fixture("win", groups=160, entities=4), "win", tmp_path, backends=())
    assert run["calibration"]["status"] == "FIT_DEDICATED_CALIBRATION"
    assert run["kind"] == "binary"
    assert sum(b["count"] for b in run["reliability_bins"]) == run["splits"]["test"]["rows"]
    assert run["metrics"]["independent_games"] == run["splits"]["test"]["games"]
    predicted = engine.predict_run(tmp_path, run["id"], fixture("win")["rows"][:3])
    assert all(0 <= r["probability"] <= 1 for r in predicted["predictions"])
    assert not run["training_point_in_time_eligible"]


def test_platt_does_not_fake_calibration_with_one_class():
    fitted = engine._platt(np.full(20, .8), np.ones(20))
    assert fitted["status"] == "INSUFFICIENT_CALIBRATION_CLASS_SUPPORT"
    assert engine._calibrate([.8], fitted)[0] == .8


def test_clipping_preserves_margin_sign_and_nonnegative_count_domains():
    assert engine._domain([-2, 4], "pts", "regression").tolist() == [0, 4]
    assert engine._domain([-2, 4], "margin", "regression").tolist() == [-2, 4]
    assert engine._domain([-2, 140], "minutes", "regression").tolist() == [0, 100]
    lower, upper = engine._intervals(np.asarray([1, 99]), 4, "minutes")
    assert lower.tolist() == [0, 95] and upper.tolist() == [5, 100]


def test_tampered_artifact_or_manifest_is_rejected_before_model_load(tmp_path):
    run = engine.train_run(fixture(), "pts", tmp_path, backends=())
    directory = tmp_path / "models" / run["id"]
    (directory / "baselines.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        engine.load_manifest(tmp_path, run["id"])
    (directory / "manifest.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest checksum"):
        engine.load_manifest(tmp_path, run["id"])


def test_status_survives_incomplete_fit_and_reports_corrupt_records_without_trusting_metadata(tmp_path):
    run = engine.train_run(fixture(), "pts", tmp_path, backends=())
    unfinished = tmp_path / "models" / "20250101T000000-aaaaaaaaaaaa"
    unfinished.mkdir(); (unfinished / "partial-model.txt").write_text("interrupted")
    corrupt = tmp_path / "models" / "20250101T000000-bbbbbbbbbbbb"
    corrupt.mkdir(); (corrupt / "manifest.json").write_text('{"target":"fake-target"}')
    runs = engine.list_runs(tmp_path)
    assert len(runs) == 2
    assert next(r for r in runs if r["id"] == run["id"])["status"] == "SHADOW"
    broken = next(r for r in runs if r["id"] == corrupt.name)
    assert broken["status"] == "CORRUPT" and "target" not in broken
    assert engine.list_run_failures(tmp_path)[0]["id"] == unfinished.name
    with pytest.raises((ValueError, OSError)):
        engine.load_manifest(tmp_path, corrupt.name)


def test_traversal_and_unknown_features_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="version ID"):
        engine.load_manifest(tmp_path, "../downloaded-model")
    run = engine.train_run(fixture(), "pts", tmp_path, backends=())
    row = copy.deepcopy(fixture()["rows"][0]); row["features"]["new_feature"] = 1
    with pytest.raises(ValueError, match="Unexpected feature"):
        engine.predict_run(tmp_path, run["id"], [row])


def test_too_few_independent_games_or_leaked_feature_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="independent game groups"):
        engine.train_run(fixture(groups=5), "pts", tmp_path, backends=())
    data = fixture()
    data["rows"][0]["features"]["actual_points"] = 10
    with pytest.raises(ValueError, match="allowlist"):
        engine.train_run(data, "pts", tmp_path, backends=())


def test_nonfinite_feature_or_incoherent_label_timestamps_are_rejected(tmp_path):
    data = fixture(); data["rows"][0]["features"]["signal"] = float("nan")
    with pytest.raises(ValueError, match="numeric"):
        engine.train_run(data, "pts", tmp_path, backends=())
    data = fixture(); data["rows"][0]["label_known_at"] = data["rows"][0]["cutoff"]
    with pytest.raises(ValueError, match="label completion"):
        engine.train_run(data, "pts", tmp_path, backends=())


def test_shadow_runs_cannot_be_promoted_from_retrospective_scores_or_rolled_back_arbitrarily(tmp_path):
    run = engine.train_run(fixture(), "pts", tmp_path, backends=())
    with pytest.raises(ValueError, match="Production promotion"):
        engine.registry_event(tmp_path, "promote", run["id"], {"records": []})
    with pytest.raises(ValueError, match="previously promoted"):
        engine.registry_event(tmp_path, "rollback", run["id"])
    assert len(engine.registry_events(tmp_path)) == 1


def test_append_only_registry_hash_chain_detects_corruption(tmp_path):
    run = engine.train_run(fixture(), "pts", tmp_path, backends=())
    engine.registry_event(tmp_path, "shadow", run["id"])
    assert len(engine.registry_events(tmp_path)) == 2
    path = tmp_path / "registry.jsonl"
    events = path.read_text().splitlines()
    event = json.loads(events[0]); event["action"] = "promote"
    events[0] = json.dumps(event); path.write_text("\n".join(events) + "\n")
    with pytest.raises(ValueError, match="hash chain"):
        engine.registry_events(tmp_path)


def promotion_fixture(days=20, variable=False):
    start = datetime(2025, 1, 2, 20, tzinfo=timezone.utc)
    records = []
    for i in range(100):
        day = i % days
        tipoff = start + timedelta(days=day)
        records.append({"game_id": str(i), "model_id": "software-fixture", "production_eligible": True,
            "prediction_created_at": (tipoff - timedelta(hours=1)).isoformat(), "tipoff": tipoff.isoformat(),
            "outcome_known_at": (tipoff + timedelta(hours=4)).isoformat(),
            "model_loss": (0 if day % 2 else 1.8) if variable else .8, "baseline_loss": 1.})
    return {"id": "software-fixture", "created_at": "2025-01-01T00:00:00Z"}, records


def test_promotion_uncertainty_uses_day_blocks_and_records_exact_interval():
    manifest, records = promotion_fixture()
    summary = engine.promotion_evidence_summary(manifest, records)
    assert summary["distinct_game_groups"] == 100
    assert summary["eastern_game_day_cohorts"] == 20
    assert summary["paired_loss_improvement_interval_95"] == pytest.approx([.2, .2])
    assert summary["source_trust"] == "LOCAL_OPERATOR_INPUT_NOT_INDEPENDENTLY_AUTHENTICATED"


def test_promotion_rejects_too_few_days_even_with_one_hundred_games():
    manifest, records = promotion_fixture(days=10)
    with pytest.raises(ValueError, match="20 independent Eastern"):
        engine.promotion_evidence_summary(manifest, records)


def test_promotion_rejects_mean_gain_without_paired_uncertainty_support():
    manifest, records = promotion_fixture(variable=True)
    assert np.mean([r["model_loss"] for r in records]) < .95
    with pytest.raises(ValueError, match="bootstrap uncertainty gate"):
        engine.promotion_evidence_summary(manifest, records)
