from datetime import datetime, timedelta, timezone

import pytest

from nba_quant.ml_backtest import walk_forward_plan, game_day, run_walk_forward
from nba_quant.ml_features import chronological_splits, rows_for_target


def bundle():
    start = datetime(2025, 1, 1, 23, tzinfo=timezone.utc)
    rows = []
    for day in range(40):
        for team in range(2):
            tip = start+timedelta(days=day)
            rows.append({"game_id": str(day), "entity_id": str(team), "scope": "team",
                "tipoff": tip.isoformat(), "cutoff": (tip-timedelta(hours=1)).isoformat(),
                "label_completed_at": (tip+timedelta(hours=3)).isoformat(),
                "label_known_at": (tip+timedelta(hours=4)).isoformat(),
                "point_in_time_mode": "strict", "production_eligible": False,
                "synthetic": True, "features": {"team_win_rate": .5}, "targets": {"win": team}})
    return {"rows": rows, "audit": {"synthetic": True}}


def test_disjoint_holdouts_and_expanding_history():
    rows = rows_for_target(bundle(), "win")
    folds = walk_forward_plan(rows, initial_days=20, test_days=5)
    assert len(folds) == 4
    seen = set()
    for fold in folds:
        split = chronological_splits([r for r in rows if game_day(r) in fold["days"]], fold["fractions"])
        days = {game_day(r) for r in split["test"]}
        assert days == set(fold["test_days"])
        assert not seen & days
        seen |= days
    assert folds[0]["days"][0] == folds[-1]["days"][0]


def test_rolling_history_and_overlap_rejection():
    rows = bundle()["rows"]
    folds = walk_forward_plan(rows, initial_days=20, test_days=5, window_days=10)
    assert len(folds[-1]["days"]) == 15
    assert folds[0]["days"][0] != folds[-1]["days"][0]
    with pytest.raises(ValueError, match="non-overlapping"):
        walk_forward_plan(rows, initial_days=20, test_days=5, step_days=2)


def test_baseline_expanding_runs_are_persisted_and_not_production(tmp_path):
    result = run_walk_forward(bundle(), "win", output_dir=tmp_path, initial_days=20, test_days=10, backends=())
    assert result["independent_test_games"] == 20
    assert len(result["folds"]) == 2
    assert not result["production_eligible"]
    assert len(list(tmp_path.glob("backtest-*.json"))) == 1
    assert all(f["cohort"] == "synthetic_software_fixture" for f in result["folds"])
