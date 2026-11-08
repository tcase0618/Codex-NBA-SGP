import copy
import math

import numpy as np
import pytest

from nba_quant.evaluation import aggregate, crps, score_forecast
from nba_quant.model import summarize
from nba_quant.schemas import Dataset, ForecastRequest
from nba_quant.service import forecast, record_outcome
from nba_quant.store import Store


def fixture():
    players, rotations, boxes = [], [], []
    for team in ("h", "a"):
        for i in range(5):
            pid = f"{team}{i}"
            players.append({"id": pid, "name": pid, "position": "PG"})
            rotations.append({"player_id": pid, "team_id": team,
                              "known_at": "2025-01-09T00:00:00Z", "expected_minutes": 48})
            boxes.append({"player_id": pid, "team_id": team, "minutes": 48,
                          "pts": 20 if team == "h" else 18,
                          "fga": 20, "fgm": 9 if team == "h" else 8,
                          "fg3a": 5, "fg3m": 2, "fta": 0, "ftm": 0,
                          "oreb": 1, "dreb": 4, "ast": 2, "stl": 1,
                          "blk": 1, "tov": 2})
    game = {"id": "target", "home_id": "h", "away_id": "a",
            "tipoff": "2025-01-10T00:00:00Z", "completed_at": "2025-01-10T03:00:00Z",
            "known_at": "2025-01-10T04:00:00Z", "home_score": 100,
            "away_score": 90, "boxes": boxes}
    dataset = {"id": "source", "source": "evaluation fixture", "source_url": "fixture://evaluation",
               "retrieved_at": "2025-01-11T00:00:00Z", "synthetic": False,
               "teams": [{"id": x, "name": x} for x in ("h", "a")],
               "players": players, "rotations": rotations, "games": [game]}
    samples = np.asarray([[100, 90], [110, 95], [120, 100]])
    result = {"home_win_probability": .75, "score_samples": samples.tolist(),
              "home_score": summarize(samples[:, 0]), "away_score": summarize(samples[:, 1]),
              "margin": summarize(samples[:, 0] - samples[:, 1]), "total": summarize(samples.sum(axis=1)),
              "players": [{"id": "h0", "team_id": "h", "stats": {"pts": summarize([10, 20, 30])},
                           "samples": {"pts": [10, 20, 30]}},
                          {"id": "missing", "team_id": "h", "stats": {"pts": summarize([0, 10, 20])},
                           "samples": {"pts": [0, 10, 20]}}]}
    frozen = {"id": "frozen", "created_at": "2025-01-09T12:01:00Z", "synthetic": False,
              "request": {"home_id": "h", "away_id": "a", "tipoff": game["tipoff"],
                          "cutoff": "2025-01-09T12:00:00Z"},
              "model": {"retrospective_source": False}, "result": result}
    return frozen, dataset, game


@pytest.mark.parametrize("samples,observed", [([1], 3), ([0, 1, 2], 1), ([9, 1, 4, 4], 7), ([-1, 0, 10], -3)])
def test_crps_matches_pairwise_definition(samples, observed):
    x = np.asarray(samples, dtype=float)
    brute_force = np.mean(abs(x - observed)) - .5 * np.mean(abs(x[:, None] - x[None, :]))
    assert crps(samples, observed) == pytest.approx(brute_force)


@pytest.mark.parametrize("samples,observed", [([], 1), ([float("nan")], 1), ([1], float("inf")), ([[1, 2]], 1)])
def test_crps_rejects_invalid_distribution(samples, observed):
    with pytest.raises(ValueError, match="finite, nonempty"):
        crps(samples, observed)


@pytest.mark.parametrize("probability,home_won,expected", [(0, False, 0), (1, True, 0), (0, True, 1), (1, False, 1), (.75, True, .0625)])
def test_brier_keeps_exact_probabilities_while_log_loss_is_finite(probability, home_won, expected):
    frozen, data, game = fixture()
    frozen["result"]["home_win_probability"] = probability
    if not home_won:
        game["home_score"], game["away_score"] = 90, 100
    outcome = score_forecast(frozen, data, game)
    assert outcome["metrics"]["brier"] == expected
    assert math.isfinite(outcome["metrics"]["log_loss"])


@pytest.mark.parametrize("probability", [-.1, 1.1, float("nan"), float("inf")])
def test_invalid_win_probabilities_fail_closed(probability):
    frozen, data, game = fixture()
    frozen["result"]["home_win_probability"] = probability
    with pytest.raises(ValueError, match="Win probability"):
        score_forecast(frozen, data, game)


def test_outcome_error_intervals_and_missing_player_coverage():
    frozen, data, game = fixture()
    result = score_forecast(frozen, data, game)
    assert result["metrics"]["home_score"]["absolute_error"] == 10
    assert result["metrics"]["home_score"]["coverage_80"] == 0
    assert result["metrics"]["margin"]["coverage_80"] == 0
    assert result["metrics"]["home_score"]["crps"] == pytest.approx(crps([100, 110, 120], 100))
    assert result["missing_player_ids"] == ["missing"]
    assert result["players_scored"] == 1
    assert len(result["player_scores"]) == 1
    assert result["player_scores"][0]["absolute_error"] == 0
    assert result["player_scores"][0]["coverage_80"] == 1


@pytest.mark.parametrize("field,value", [("home_id", "a"), ("away_id", "h"), ("tipoff", "2025-01-10T00:00:01Z")])
def test_outcome_requires_exact_teams_and_tipoff(field, value):
    frozen, data, game = fixture()
    game[field] = value
    with pytest.raises(ValueError, match="exact tipoff"):
        score_forecast(frozen, data, game)


def test_timezone_equivalent_tipoff_matches():
    frozen, data, game = fixture()
    game["tipoff"] = "2025-01-09T19:00:00-05:00"
    assert score_forecast(frozen, data, game)["game_id"] == "target"


def test_known_outcome_and_synthetic_boundary_are_rejected():
    frozen, data, game = fixture()
    game["known_at"] = frozen["request"]["cutoff"]
    with pytest.raises(ValueError, match="unknown at forecast cutoff"):
        score_forecast(frozen, data, game)
    game["known_at"] = "2025-01-10T04:00:00Z"
    data["synthetic"] = True
    with pytest.raises(ValueError, match="Cannot mix synthetic"):
        score_forecast(frozen, data, game)


def test_wrong_player_team_does_not_silently_score_as_valid():
    frozen, data, game = fixture()
    game["boxes"][0]["team_id"] = "a"
    with pytest.raises(ValueError, match="player team differs"):
        score_forecast(frozen, data, game)


@pytest.mark.parametrize("created_at,retrospective_source,synthetic,expected", [
    ("2025-01-09T12:01:00Z", False, False, "prospective"),
    ("2025-01-09T12:01:00Z", True, False, "retrospective"),
    ("2025-01-10T00:00:00Z", False, False, "retrospective"),
    ("2025-01-09T12:01:00Z", False, True, "synthetic"),
])
def test_cohort_classification(created_at, retrospective_source, synthetic, expected):
    frozen, data, game = fixture()
    frozen.update(created_at=created_at, synthetic=synthetic)
    data["synthetic"] = synthetic
    frozen["model"]["retrospective_source"] = retrospective_source
    assert score_forecast(frozen, data, game)["cohort"] == expected


def test_aggregate_counts_distinct_targets_and_keeps_cohorts_separate():
    frozen, data, game = fixture()
    original = score_forecast(frozen, data, game)
    repeated = copy.deepcopy(original)
    # A different provider's game ID must not create another independent target.
    repeated.update(dataset_id="other-provider", game_id="alternate-id")
    synthetic = copy.deepcopy(original)
    synthetic.update(cohort="synthetic", synthetic=True)
    result = aggregate([original, repeated, synthetic])
    assert result["forecasts_scored"] == 3
    assert result["distinct_games"] == 1
    assert result["cohorts"]["prospective"]["count"] == 2
    assert result["cohorts"]["prospective"]["distinct_games"] == 1
    assert result["cohorts"]["synthetic"]["count"] == 1
    assert "not independent games" in result["note"]


def test_reliability_bins_include_every_probability_boundary_once():
    frozen, data, game = fixture()
    outcomes = []
    for i in range(11):
        frozen["result"]["home_win_probability"] = i / 10
        outcomes.append(score_forecast(frozen, data, game))
    result = aggregate(outcomes)
    bins = result["cohorts"]["prospective"]["reliability_bins"]
    assert sum(b["count"] for b in bins) == 11
    assert [b["count"] for b in bins] == [1] * 9 + [2]
    assert bins[6]["predicted"] == .6


def test_aggregate_without_outcomes_makes_no_accuracy_claim():
    result = aggregate([])
    assert result["status"] == "NOT MEASURABLE"
    assert result["distinct_games"] == result["forecasts_scored"] == 0
    assert not result["metrics"]


def test_service_freezes_replay_and_enforces_outcome_matching(tmp_path, monkeypatch):
    frozen, raw, game = fixture()
    source = {k: v for k, v in raw.items() if k != "id"}
    store = Store(tmp_path / "evaluation.sqlite")
    outcome_dataset_id = store.import_dataset(Dataset.model_validate(source))
    source = {**source, "games": [], "retrieved_at": "2025-01-09T06:00:00Z"}
    dataset_id = store.import_dataset(Dataset.model_validate(source))
    monkeypatch.setattr("nba_quant.service.run", lambda *_args: frozen["result"])
    req = ForecastRequest(dataset_id=dataset_id, **frozen["request"])
    actual = forecast(store, req)
    assert store.forecast(actual["id"]) == actual
    assert actual["model"]["retrospective_source"]
    outcome = record_outcome(store, actual["id"], outcome_dataset_id, game["id"])
    assert outcome["cohort"] == "retrospective"
    assert store.outcomes()[0]["forecast_id"] == actual["id"]
    assert store.outcomes()[0]["metrics"] == outcome["metrics"]
    with pytest.raises(ValueError, match="Unknown forecast"):
        record_outcome(store, "missing", outcome_dataset_id, game["id"])
    with pytest.raises(ValueError, match="Unknown outcome game"):
        record_outcome(store, actual["id"], outcome_dataset_id, "missing")
