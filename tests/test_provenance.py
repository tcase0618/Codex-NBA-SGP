"""Independent point-in-time and persistence contract tests."""
import copy
import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from nba_quant.model import prepare
from nba_quant.schemas import Dataset, ForecastRequest, Game, Rotation, utc
from nba_quant.store import Store, canonical


def raw_dataset():
    players, rotations, boxes = [], [], []
    for team in ("home", "away"):
        for i in range(6):
            pid = f"{team}-{i}"
            players.append({"id": pid, "name": pid, "position": "PG"})
            rotations.append({"player_id": pid, "team_id": team,
                              "known_at": "2025-01-09T00:00:00Z", "expected_minutes": 40})
            boxes.append({"player_id": pid, "team_id": team, "minutes": 40,
                          "pts": 11 if team == "home" else 10,
                          "fga": 10, "fgm": 4, "fg3a": 3, "fg3m": 1,
                          "fta": 2, "ftm": 2 if team == "home" else 1,
                          "oreb": 1, "dreb": 4, "ast": 2, "stl": 1,
                          "blk": 1, "tov": 2})
    game = {"id": "prior", "home_id": "home", "away_id": "away",
            "tipoff": "2025-01-01T00:00:00Z", "completed_at": "2025-01-01T03:00:00Z",
            "known_at": "2025-01-02T00:00:00Z", "home_score": 66,
            "away_score": 60, "boxes": boxes}
    future = copy.deepcopy(game)
    future.update(id="future", tipoff="2025-01-10T00:00:00Z",
                  completed_at="2025-01-10T03:00:00Z", known_at="2025-01-11T00:00:00Z")
    delayed = copy.deepcopy(game)
    delayed.update(id="late_revision", tipoff="2025-01-03T00:00:00Z",
                   completed_at="2025-01-03T03:00:00Z", known_at="2025-01-12T00:00:00Z")
    future_rotation = {**rotations[0], "known_at": "2025-01-11T00:00:00Z",
                       "active_probability": 0, "minutes_cap": 0}
    return {"source": "independent test fixture", "source_url": "fixture://provenance",
            "retrieved_at": "2025-01-20T00:00:00Z", "synthetic": True,
            "teams": [{"id": x, "name": x} for x in ("home", "away")],
            "players": players, "rotations": rotations + [future_rotation],
            "games": [game, future, delayed]}


def request(**changes):
    return ForecastRequest(**{"dataset_id": "test", "home_id": "home", "away_id": "away",
                              "tipoff": "2025-01-10T00:00:00Z",
                              "cutoff": "2025-01-09T12:00:00Z", **changes})


def test_timezone_is_required_and_normalized():
    with pytest.raises(ValidationError, match="Timezone-aware"):
        Rotation(player_id="p", team_id="t", known_at="2025-01-01T12:00:00", expected_minutes=30)
    assert utc("2025-01-01T07:00:00-05:00") == utc("2025-01-01T12:00:00Z")


@pytest.mark.parametrize("mutation,message", [
    (lambda x: x["players"].append(x["players"][0]), "Duplicate team/player"),
    (lambda x: x["games"].append(x["games"][0]), "Duplicate game"),
    (lambda x: x["rotations"].append(x["rotations"][0]), "Duplicate rotation"),
    (lambda x: x["rotations"][0].update(player_id="missing"), "Unknown rotation"),
    (lambda x: x["games"][0]["boxes"][0].update(player_id="missing"), "Unknown game"),
    (lambda x: x.update(retrieved_at="2025-01-10T00:00:00Z"), "known_at cannot follow"),
    (lambda x: x.update(retrieved_at="2999-01-01T00:00:00Z"), "cannot be in the future"),
])
def test_reject_invalid_source_contracts(mutation, message):
    raw = raw_dataset()
    mutation(raw)
    with pytest.raises(ValidationError, match=message):
        Dataset.model_validate(raw)


@pytest.mark.parametrize("mutation,message", [
    (lambda g: g["boxes"][0].update(pts=999), "Points do not reconcile"),
    (lambda g: g["boxes"][0].update(minutes=49), "Individual minutes exceed"),
    (lambda g: (g["boxes"][0].update(ast=21), [b.update(ast=0) for b in g["boxes"][1:6]]), "player's assists exceed"),
    (lambda g: g["boxes"][0].update(stl=20), "Steals exceed"),
    (lambda g: g["boxes"][0].update(blk=100), "Blocks exceed"),
    (lambda g: g.update(known_at="2024-12-31T00:00:00Z"), "completion <= known_at"),
])
def test_reject_incoherent_historical_box_scores(mutation, message):
    g = raw_dataset()["games"][0]
    mutation(g)
    with pytest.raises(ValidationError, match=message):
        Game.model_validate(g)


def test_cutoff_excludes_future_results_late_known_revisions_and_rotations():
    data = Dataset.model_validate(raw_dataset()).model_dump(mode="json")
    result = prepare(data, request())
    assert result["games_used"] == 1
    player = result["teams"][0]["players"][0]
    assert player["historical_minutes"] == 40
    assert player["active_probability"] == 1
    assert player["minutes_cap"] == 48
    altered = copy.deepcopy(data)
    altered["games"][1]["boxes"][0]["fg3m"] = 999
    altered["games"][2]["boxes"][0]["minutes"] = 999
    altered["rotations"][-1]["expected_minutes"] = 0
    assert prepare(altered, request())["feature_hash"] == result["feature_hash"]


def test_cutoff_includes_only_timestamp_eligible_record_versions():
    data = Dataset.model_validate(raw_dataset()).model_dump(mode="json")
    result = prepare(data, request(cutoff="2025-01-11T12:00:00Z", tipoff="2025-01-12T00:00:00Z"))
    assert result["games_used"] == 2
    assert result["teams"][0]["players"][0]["active_probability"] == 0


def test_actual_import_time_marks_retrospective_replay_even_when_claimed_retrieval_precedes_cutoff():
    data = raw_dataset()
    data["games"] = data["games"][:1]
    data["rotations"] = data["rotations"][:-1]
    data["retrieved_at"] = "2025-01-09T00:00:00Z"
    data = Dataset.model_validate(data).model_dump(mode="json")
    assert not prepare(data, request())["retrospective_source"]
    data["imported_at"] = "2025-01-10T00:00:00Z"
    result = prepare(data, request())
    assert result["retrospective_source"]
    assert any("not independently verified" in warning for warning in result["warnings"])


def test_scenario_overrides_are_applied_without_mutating_source():
    data = Dataset.model_validate(raw_dataset()).model_dump(mode="json")
    before = copy.deepcopy(data)
    result = prepare(data, request(scenarios=[{"player_id": "home-0", "minutes_cap": 20}]))
    assert result["teams"][0]["players"][0]["minutes_cap"] == 20
    assert data == before


def test_unknown_scenario_and_impossible_rotation_fail_closed():
    data = Dataset.model_validate(raw_dataset()).model_dump(mode="json")
    with pytest.raises(ValueError, match="no eligible rotation"):
        prepare(data, request(scenarios=[{"player_id": "unknown", "minutes_cap": 20}]))
    with pytest.raises(ValueError, match="240 minutes"):
        prepare(data, request(scenarios=[{"player_id": f"home-{i}", "minutes_cap": 20} for i in range(6)]))


def test_dataset_content_hash_and_idempotent_import(tmp_path):
    store = Store(tmp_path / "evidence.sqlite")
    dataset = Dataset.model_validate(raw_dataset())
    key = store.import_dataset(dataset)
    assert store.import_dataset(dataset) == key
    assert len(store.datasets()) == 1
    assert store.datasets()[0]["id"] == key
    assert utc(store.datasets()[0]["imported_at"]) <= datetime.now(timezone.utc)


@pytest.mark.parametrize("table", ["datasets", "forecasts", "outcomes"])
@pytest.mark.parametrize("operation", ["update", "delete", "replace"])
def test_frozen_persistence_blocks_alternate_mutation_paths(tmp_path, table, operation):
    store = Store(tmp_path / "frozen.sqlite")
    store.import_dataset(Dataset.model_validate(raw_dataset()))
    store.save_forecast({"id": "frozen", "created_at": "2025-01-01T00:00:00Z", "score": 100})
    store.record_outcome("frozen", {"score": 101})
    with pytest.raises(sqlite3.IntegrityError, match="Frozen|Immutable"):
        with store.connect() as db:
            if operation == "update":
                db.execute(f"UPDATE {table} SET payload='{{}}'")
            elif operation == "delete":
                db.execute(f"DELETE FROM {table}")
            else:
                row = db.execute(f"SELECT * FROM {table}").fetchone()
                db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?,?)", row)


def test_foreign_key_rejects_outcome_without_frozen_forecast(tmp_path):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        Store(tmp_path / "orphan.sqlite").record_outcome("missing", {"home_score": 10})


def test_source_hash_detects_corrupted_payload_on_read(tmp_path):
    store = Store(tmp_path / "corrupt.sqlite")
    store.import_dataset(Dataset.model_validate(raw_dataset()))
    with store.connect() as db:
        db.execute("DROP TRIGGER immutable_datasets")
        db.execute("UPDATE datasets SET payload=?", (canonical({"source": "corrupt"}),))
    with pytest.raises(ValueError, match="content hash mismatch"):
        store.datasets()
