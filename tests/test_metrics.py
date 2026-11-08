from datetime import datetime, timezone

import pytest

from nba_quant.metrics import CATALOG, metric_values


def dataset_fixture():
    base = {"minutes": 30, "pts": 25, "fga": 20, "fgm": 9, "fg3a": 8, "fg3m": 3,
            "fta": 5, "ftm": 4, "oreb": 2, "dreb": 6, "ast": 7, "stl": 1, "blk": 1, "tov": 2, "pf": 2}
    opponent = {**base, "pts": 22, "fga": 18, "fgm": 8, "fg3a": 6, "fg3m": 2, "fta": 5, "ftm": 4,
                "oreb": 3, "dreb": 5}
    return {"source": "Explicit fictional metrics fixture", "source_url": "fixture://metrics", "synthetic": True,
            "games": [{"id": "g1", "known_at": "2026-01-02T04:00:00+00:00",
                       "completed_at": "2026-01-02T03:00:00+00:00", "overtime_periods": 0,
                       "boxes": [{**base, "team_id": "H", "player_id": "p1"},
                                 {**opponent, "team_id": "A", "player_id": "p2"}]}]}


def values(dataset, scope="player", entity="p1"):
    result = metric_values(dataset, scope, entity, datetime(2026, 2, 1, tzinfo=timezone.utc))
    return {m["label"]: m for m in result["metrics"]}


def test_catalog_exact_blueprint_scopes_and_unique_identifiers():
    assert len(CATALOG) == len({m["id"] for m in CATALOG}) == 440
    for scope, count in {"player": 100, "team": 240, "PG": 20, "SG": 20, "SF": 20, "PF": 20, "C": 20}.items():
        assert sum(m["scope"] == scope for m in CATALOG) == count


def test_player_box_score_metrics_match_formulas():
    metrics = values(dataset_fixture())
    expected = {"Points / game": 25, "Points / 36 minutes": 30,
                "True shooting %": 25 / (2 * (20 + .44 * 5)) * 100,
                "Effective FG%": (9 + .5 * 3) / 20 * 100,
                "2PT FG%": (9 - 3) / (20 - 8) * 100,
                "3PT FG%": 3 / 8 * 100, "FT%": 4 / 5 * 100,
                "3PA share": 8 / 20 * 100, "Assist-to-turnover ratio": 7 / 2,
                "Turnover rate": 2 / (20 + .44 * 5 + 2) * 100,
                "Games played": 1, "Minutes / game": 30, "Defensive rebounds secured": 6}
    for label, value in expected.items():
        assert metrics[label]["value"] == pytest.approx(value, abs=.0001), label
        assert metrics[label]["status"] == "SYNTHETIC_DERIVED"
        assert metrics[label]["source_url"] == "fixture://metrics"


def test_team_efficiency_uses_shared_possession_estimate():
    metrics = values(dataset_fixture(), "team", "H")
    possession_estimate = .5 * ((20 + .44 * 5 - 2 + 2) + (18 + .44 * 5 - 3 + 2))
    expected = {"Offensive rating": 25 / possession_estimate * 100,
                "Defensive rating": 22 / possession_estimate * 100,
                "Net rating": 3 / possession_estimate * 100,
                "Pace": possession_estimate,
                "Offensive rebound %": 2 / (2 + 5) * 100,
                "Defensive rebound %": 6 / (6 + 3) * 100}
    for label, value in expected.items():
        assert metrics[label]["value"] == pytest.approx(value, abs=.0001), label


def test_missing_tracking_and_zero_denominator_stay_unavailable():
    dataset = dataset_fixture()
    for b in dataset["games"][0]["boxes"]:
        for k in list(b):
            if isinstance(b[k], (int, float)):
                b[k] = 0
    metrics = values(dataset)
    for label in ("True shooting %", "3PT FG%", "Assist-to-turnover ratio", "Rim FG%", "Distance traveled / game"):
        assert metrics[label]["value"] is None
        assert metrics[label]["status"] == "UNAVAILABLE"


def test_cutoff_excludes_future_known_outcomes():
    dataset = dataset_fixture()
    result = metric_values(dataset, "player", "p1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["coverage"]["available"] == 0
    assert all(m["value"] is None for m in result["metrics"])


def test_dnp_rows_do_not_reduce_per_game_averages():
    dataset = dataset_fixture()
    empty = {key: 0 for key in dataset["games"][0]["boxes"][0] if key not in {"team_id", "player_id"}}
    dataset["games"].append({**dataset["games"][0], "id": "g2",
                            "boxes": [{**empty, "team_id": "H", "player_id": "p1"}]})
    metrics = values(dataset)
    assert metrics["Games played"]["value"] == 1
    assert metrics["Points / game"]["value"] == 25
    assert metrics["Minutes / game"]["value"] == 30
