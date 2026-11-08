"""PIT feature contracts and purged chronological NBA slate partitions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from nba_quant.ml_features import (
    TARGET_REGISTRY, build_training_rows, chronological_splits, features_for_game,
    rows_for_target, target_values,
)
from nba_quant.schemas import utc
from test_provenance import raw_dataset


AS_OF = datetime(2025, 3, 1, tzinfo=timezone.utc)


def history(count=20, delayed=False, rotations=True):
    data = raw_dataset()
    template = data["games"][0]
    data.update(id="explicit-ml-fixture", synthetic=False, source="Explicit fictional ML unit-test data",
                retrieved_at=AS_OF.isoformat())
    data["games"] = []
    for i in range(count):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        game = deepcopy(template)
        game.update(id=f"g{i:02d}", tipoff=start.isoformat(), completed_at=(start + timedelta(hours=3)).isoformat(),
                    known_at=(AS_OF - timedelta(hours=1)).isoformat() if delayed else (start + timedelta(hours=4)).isoformat())
        data["games"].append(game)
    data["rotations"] = data["rotations"][:12] if rotations else []
    for rotation in data["rotations"]:
        rotation["known_at"] = "2024-12-30T00:00:00Z"
    return data


def player_row(bundle, game="g04", pid="home-0"):
    return next(r for r in bundle["rows"] if r["game_id"] == game and r.get("player_id") == pid)


def test_registry_and_player_target_identities_cover_supported_nba_targets():
    assert set(TARGET_REGISTRY) == {"pts", "reb", "ast", "fg3m", "fg3a", "stl", "blk", "tov", "minutes", "fga", "fta", "pra",
                                    "team_points", "opponent_points", "margin", "total", "win", "pace_proxy", "offensive_rating_proxy", "defensive_rating_proxy"}
    box = history()["games"][0]["boxes"][0]
    values = target_values(box)
    assert values["reb"] == box["oreb"] + box["dreb"]
    assert values["pra"] == box["pts"] + values["reb"] + box["ast"]
    assert TARGET_REGISTRY["win"]["kind"] == "binary"
    assert TARGET_REGISTRY["pace_proxy"]["proxy"]


def test_strict_rows_use_only_observed_completed_history_and_fixed_pregame_horizon():
    data = history()
    original = deepcopy(data)
    result = build_training_rows(data, as_of=AS_OF)
    row = player_row(result)
    assert row["features"]["player_pts_mean"] == 11
    assert row["features"]["player_pts_per_minute"] == 11 / 40
    assert row["features"]["player_pra_mean"] == 18
    assert row["features"]["player_history_games"] == 4
    assert row["features"]["is_home"] == 1
    assert row["targets"]["pts"] == 11
    assert utc(row["cutoff"]) == utc(row["tipoff"]) - timedelta(hours=1)
    assert row["production_eligible"] is False  # Consolidated source itself was retrieved after this cutoff.
    assert row["provenance"]["retrospective_source_observation"] is True
    assert all(utc(stamp) <= utc(row["cutoff"]) for stamp in row["provenance"]["history_original_known_at"].values())
    assert all(np.isfinite(value) for r in result["rows"] for value in r["features"].values())
    assert data == original


def test_delayed_bulk_observations_are_not_backdated_in_strict_or_replay_mode():
    data = history(delayed=True)
    strict = build_training_rows(data, as_of=AS_OF)
    assert strict["rows"] == []
    assert strict["audit"]["status"] == "NO_ELIGIBLE_ROWS"
    replay = build_training_rows(data, mode="retrospective_event_time", as_of=AS_OF)
    assert replay["rows"]
    assert all(r["production_eligible"] is False for r in replay["rows"])
    row = player_row(replay)
    assert all(utc(stamp) > utc(row["cutoff"]) for stamp in row["provenance"]["player_history_original_known_at"].values())
    assert replay["audit"]["history_known_after_cutoff"] > 0
    assert data["games"][0]["known_at"] == (AS_OF - timedelta(hours=1)).isoformat()


def observed_snapshots():
    data = history()
    return [{**data, "id": f"snapshot-{g['id']}", "games": [g], "retrieved_at": g["known_at"]}
            for g in data["games"]]


def test_source_observed_before_cutoff_can_be_temporally_eligible_without_claiming_authentication():
    result = build_training_rows(observed_snapshots(), as_of=AS_OF)
    row = player_row(result)
    assert row["production_eligible"] is True
    assert row["provenance"]["retrospective_source_observation"] is False
    assert "not source authentication" in result["audit"]["production_note"]


def test_late_local_import_preserves_known_at_but_cannot_be_called_prospective():
    snapshots = observed_snapshots()
    for snapshot in snapshots:
        snapshot["imported_at"] = "2025-02-01T00:00:00Z"
    result = build_training_rows(snapshots, as_of=AS_OF)
    row = player_row(result)
    assert row["features"]["player_history_games"] == 4
    assert row["production_eligible"] is False
    assert row["provenance"]["retrospective_source_observation"] is True
    assert row["provenance"]["player_history_original_known_at"]["g00"] == snapshots[0]["games"][0]["known_at"]
    assert result["audit"]["source_observation_late_rows"] == len(result["rows"])


def test_strict_player_population_is_not_inferred_from_final_boxes():
    data = history(rotations=False)
    result = build_training_rows(data, as_of=AS_OF)
    assert result["audit"]["scope_rows"] == {"team": 34}
    assert rows_for_target(result, "pts") == []
    replay = build_training_rows(data, mode="retrospective_event_time", as_of=AS_OF)
    assert rows_for_target(replay, "pts")
    assert all(r["production_eligible"] is False for r in replay["rows"])


def test_missing_final_player_row_does_not_become_invented_zero_label():
    data = history()
    data["games"][4]["boxes"] = [b for b in data["games"][4]["boxes"] if b["player_id"] != "home-0"]
    result = build_training_rows(data, as_of=AS_OF)
    assert not any(r["game_id"] == "g04" and r.get("player_id") == "home-0" for r in result["rows"])
    assert result["audit"]["pregame_players_missing_outcome_row"] >= 1


def test_target_outcome_and_future_games_cannot_change_its_own_predictor_features():
    data = history()
    before = features_for_game(data, data["games"][4], cutoff=utc(data["games"][4]["tipoff"]) - timedelta(hours=1), as_of=AS_OF)
    changed = deepcopy(data)
    for game in changed["games"][4:]:
        game["boxes"][0]["pts"] = 999
        game["home_score"] = 9999
    after = features_for_game(changed, changed["games"][4], cutoff=utc(changed["games"][4]["tipoff"]) - timedelta(hours=1), as_of=AS_OF)
    assert player_row(before)["features"] == player_row(after)["features"]
    assert all("targets" not in r and "label" not in r for r in after["rows"])


def test_late_revision_cannot_replace_an_earlier_available_feature_record():
    data = history()
    revision = deepcopy(data)
    revision["id"] = "later-revision"
    revision["games"] = [deepcopy(data["games"][0])]
    revision["games"][0]["known_at"] = "2025-01-05T12:00:00Z"
    revision["games"][0]["boxes"][0]["pts"] = 99
    rows = build_training_rows([data, revision], as_of=AS_OF)
    early = player_row(rows, game="g03")
    late = player_row(rows, game="g06")
    assert early["features"]["player_pts_mean"] == 11
    assert early["provenance"]["player_history_original_known_at"]["g00"] == data["games"][0]["known_at"]
    assert late["features"]["player_pts_mean"] == pytest.approx((99 + 5 * 11) / 6)
    assert late["provenance"]["player_history_original_known_at"]["g00"] == "2025-01-05T12:00:00Z"


def test_identical_source_records_deduplicate_and_same_timestamp_conflicts_fail_closed():
    data = history()
    deduped = build_training_rows([data, deepcopy(data)], as_of=AS_OF)
    assert deduped["audit"]["retained_game_revisions"] == 20
    assert deduped["audit"]["duplicate_game_records_deduplicated"] == 20
    conflict = deepcopy(data)
    conflict["games"][0]["boxes"][0]["pts"] = 99
    with pytest.raises(ValueError, match="Conflicting game revisions"):
        build_training_rows([data, conflict], as_of=AS_OF)


def test_labels_are_separate_from_numeric_features_and_single_target_rows():
    bundle = build_training_rows(history(), as_of=AS_OF)
    selected = rows_for_target(bundle, "pts")
    assert selected and all(r["scope"] == "player" and r["label"] == 11 for r in selected if r["team_id"] == "home")
    assert all("targets" not in r for r in selected)
    assert all("label" not in r["features"] and "targets" not in r["features"] for r in selected)
    assert all(r["target_name"] == "pts" for r in selected)
    assert len({tuple(sorted(r["features"])) for r in selected}) == 1
    with pytest.raises(ValueError, match="Unsupported"):
        rows_for_target(bundle, "unimplemented_tracking_gravity")


def test_team_labels_reconcile_and_possession_ratings_are_explicit_proxies():
    result = build_training_rows(history(), as_of=AS_OF)
    home = next(r for r in result["rows"] if r["game_id"] == "g04" and r["scope"] == "team" and r["team_id"] == "home")
    away = next(r for r in result["rows"] if r["game_id"] == "g04" and r["scope"] == "team" and r["team_id"] == "away")
    assert home["targets"]["team_points"] == 66 and home["targets"]["opponent_points"] == 60
    assert home["targets"]["margin"] == -away["targets"]["margin"] == 6
    assert home["targets"]["total"] == away["targets"]["total"] == 126
    assert home["targets"]["win"] + away["targets"]["win"] == 1
    assert home["targets"]["offensive_rating_proxy"] == pytest.approx(100 * 66 / 71.28)
    assert home["features"]["team_rest_hours"] == 21


def test_missing_opportunity_inputs_never_become_fabricated_pace_or_ratings():
    data = history(rotations=False)
    for game in data["games"]:
        for box in game["boxes"]:
            del box["fga"]
    result = build_training_rows(data, as_of=AS_OF)
    assert result["rows"]
    assert rows_for_target(result, "pace_proxy") == []
    assert all("offensive_rating_proxy" not in r["targets"] for r in result["rows"])
    assert all(r["features"]["team_pace_proxy_mean_missing"] == 1 for r in result["rows"])


def test_raw_roster_without_minutes_projections_has_per_field_missing_flags():
    data = history(rotations=False)
    game = data["games"][4]
    cutoff = utc(game["tipoff"]) - timedelta(hours=1)
    roster = [{"id": "home-0", "name": "Observed roster only", "team_id": "home",
               "source": {"retrieved_at": (cutoff - timedelta(minutes=1)).isoformat()}}]
    result = features_for_game(data, game, cutoff=cutoff, roster=roster, as_of=AS_OF)
    f = player_row(result)["features"]
    assert f["rotation_snapshot_missing"] == 0
    for field in ("expected_minutes", "minutes_cap", "active_probability"):
        assert f[f"rotation_{field}"] == 0 and f[f"rotation_{field}_missing"] == 1
    roster[0]["expected_minutes"] = 0
    known_zero = player_row(features_for_game(data, game, cutoff=cutoff, roster=roster, as_of=AS_OF))["features"]
    assert known_zero["rotation_expected_minutes"] == 0 and known_zero["rotation_expected_minutes_missing"] == 0


def test_future_roster_and_unrelated_team_history_do_not_create_player_rows():
    data = history(rotations=False)
    game = data["games"][4]
    cutoff = utc(game["tipoff"]) - timedelta(hours=1)
    roster = [{"id": "home-0", "team_id": "home", "known_at": (cutoff + timedelta(seconds=1)).isoformat()}]
    result = features_for_game(data, game, cutoff=cutoff, roster=roster, as_of=AS_OF)
    assert not any(r["scope"] == "player" for r in result["rows"])


@pytest.mark.parametrize("field,value", [("expected_minutes", -1), ("minutes_cap", 49), ("active_probability", 1.1),
                                         ("active_probability", True)])
def test_unvalidated_rotation_projections_are_not_numeric_evidence(field, value):
    data = history(rotations=False)
    game = data["games"][4]
    cutoff = utc(game["tipoff"]) - timedelta(hours=1)
    roster = [{"id": "home-0", "team_id": "home", "known_at": (cutoff - timedelta(minutes=1)).isoformat(), field: value}]
    with pytest.raises(ValueError):
        features_for_game(data, game, cutoff=cutoff, roster=roster, as_of=AS_OF)


def test_same_timestamp_conflicting_roster_membership_is_not_silently_chosen():
    data = history()
    conflict = deepcopy(data)
    conflict["rotations"][0]["team_id"] = "away"
    with pytest.raises(ValueError, match="Conflicting rotation"):
        build_training_rows([data, conflict], as_of=AS_OF)


def test_synthetic_data_is_excluded_by_default_and_explicit_fixture_use_is_ineligible():
    data = history()
    data["synthetic"] = True
    assert build_training_rows(data, as_of=AS_OF)["rows"] == []
    result = build_training_rows(data, as_of=AS_OF, include_synthetic=True)
    assert result["rows"] and not any(r["production_eligible"] for r in result["rows"])


def slate_rows(days=17, replay=False):
    result = []
    for day in range(days):
        early = datetime(2025, 1, 1, 20, tzinfo=timezone.utc) + timedelta(days=day)
        for slot, tip in enumerate((early, early + timedelta(hours=7.5))):
            gid = f"day{day:02d}-{slot}"
            result.append({"game_id": gid, "entity_id": "one", "tipoff": tip.isoformat(),
                "cutoff": (tip - timedelta(hours=1)).isoformat(),
                "label_completed_at": (tip + timedelta(hours=3)).isoformat(),
                "label_known_at": "2025-03-01T00:00:00Z" if replay else (tip + timedelta(hours=4)).isoformat(),
                "point_in_time_mode": "retrospective_event_time" if replay else "strict",
                "production_eligible": not replay, "features": {}, "label": 1})
    return result


def test_entire_eastern_slates_stay_together_across_midnight_utc():
    result = chronological_splits(slate_rows())
    assert result["audit"]["status"] == "READY"
    assert result["audit"]["input_day_cohorts"] == 17
    assert all(result[name] for name in ("train", "tune", "calibration", "test"))
    assigned = {r["game_id"]: name for name in ("train", "tune", "calibration", "test") for r in result[name]}
    for day in range(17):
        assert assigned[f"day{day:02d}-0"] == assigned[f"day{day:02d}-1"]
    assert result["audit"]["purged_game_ids"] == {name: [] for name in ("train", "tune", "calibration", "test")}


def test_split_purges_whole_game_if_any_label_was_unavailable_before_next_fold():
    rows = slate_rows()
    target = rows[18]  # Last training day under default 17-day fractions.
    target["label_known_at"] = "2025-03-01T00:00:00Z"
    duplicate_entity = {**target, "entity_id": "second", "label_known_at": target["label_completed_at"]}
    rows.append(duplicate_entity)
    result = chronological_splits(rows)
    assert target["game_id"] in result["audit"]["purged_game_ids"]["train"]
    assert not any(r["game_id"] == target["game_id"] for r in result["train"])


def test_completion_overlap_is_purged_even_in_explicit_retrospective_replay():
    rows = slate_rows(replay=True)
    rows[18]["label_completed_at"] = "2025-03-01T00:00:00Z"
    result = chronological_splits(rows)
    assert rows[18]["game_id"] in result["audit"]["purged_game_ids"]["train"]
    assert result["tune"] and result["calibration"] and result["test"]
    assert result["audit"]["production_eligible"] is False
    assert result["audit"]["knowledge_purge_ignored_for_replay"] is True


def test_split_never_shuffles_and_is_deterministic_under_input_order():
    rows = slate_rows()
    a = chronological_splits(rows)
    b = chronological_splits(list(reversed(rows)))
    assert a == b
    fold_ids = [{r["game_id"] for r in a[name]} for name in ("train", "tune", "calibration", "test")]
    assert sum(len(ids) for ids in fold_ids) == len(set.union(*fold_ids))


def test_fewer_than_four_game_days_does_not_invent_holdout_evidence():
    result = chronological_splits(slate_rows(days=3))
    assert result["audit"]["status"] == "INSUFFICIENT_GROUPS"
    assert all(result[name] == [] for name in ("train", "tune", "calibration", "test"))


@pytest.mark.parametrize("mode", ["random", "event_time", "backdate"])
def test_unknown_pit_mode_is_not_silently_accepted(mode):
    with pytest.raises(ValueError, match="Mode"):
        build_training_rows(history(), mode=mode, as_of=AS_OF)


def test_invalid_or_future_timestamps_fail_closed():
    data = history()
    data["games"][0]["known_at"] = "2025-01-01T00:00:00"
    with pytest.raises(ValueError, match="Timezone-aware"):
        build_training_rows(data, as_of=AS_OF)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with pytest.raises(ValueError, match="future"):
        build_training_rows(history(), as_of=future)
