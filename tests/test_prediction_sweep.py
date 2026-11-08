import copy

from nba_quant.prediction_sweep import select_prediction_family


def candidates():
    return [{"player_id": str(i), "player_name": str(i), "team_id": "a" if i%2 else "b",
        "stat": ("pts", "reb", "ast", "fg3m")[i%4], "side": "over", "line": 4.5,
        "coverage_eligible": True, "offer": {"decimal_odds": 2.1},
        "prediction_evidence": {"point_predictions": {"autogluon": 8., "xgboost": 7., "historical_baseline": 6.},
            "tuning_rmse_scale": 2.}} for i in range(10)]


def test_full_eight_distinct_players_and_no_input_mutation():
    source = candidates(); before = copy.deepcopy(source)
    result = select_prediction_family(source, 8, ("a", "b"))
    assert len({l["player_id"] for l in result["legs"]}) == 8
    assert result["anchors_selected"] == 3
    assert result["joint_probability"] is None and result["expected_value"] is None
    assert source == before


def test_disagreement_and_missing_scale_do_not_become_probabilities():
    source = candidates()[:3]
    source[0]["prediction_evidence"]["point_predictions"]["xgboost"] = 4.
    source[1]["prediction_evidence"]["tuning_rmse_scale"] = 0
    result = select_prediction_family(source, 3, ("a", "b"))
    assert result["legs"] == []
    assert result["distinct_agreeing_players"] == 1


def test_unknown_standard_under_is_excluded():
    source = candidates()[:3]
    source[0]["side"] = "under"
    result = select_prediction_family(source, 3, ("a", "b"))
    assert result["legs"] == []


def test_same_player_alternatives_never_fill_missing_players():
    source = candidates()[:2]
    source.append({**source[0], "stat": "ast"})
    assert select_prediction_family(source, 3, ("a", "b"))["legs"] == []
