from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from nba_quant.sgp import Leg, TicketQuote, evaluate_sgp, leg_key


def fixture(synthetic=False):
    """Fictional math fixtures; synthetic=False tests the gate, not real NBA data."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=2)
    tipoff = now + timedelta(hours=1)
    hi = [0] * 10 + [30] * 10
    lo = [30] * 10 + [0] * 10
    players = []
    for pid, team, points in [("p1", "H", hi), ("p2", "A", lo)]:
        players.append({"id": pid, "name": f"Fictional {pid}", "team_id": team,
                        "samples": {"pts": points, "reb": [x // 3 for x in hi],
                                    "ast": [x // 5 for x in hi], "fg3m": [x // 10 for x in hi],
                                    "stl": [0] * 20, "blk": [1] * 20, "tov": [2] * 20}})
    f = {"id": "fictional-forecast", "created_at": (now - timedelta(seconds=20)).isoformat(),
         "synthetic": synthetic, "source": "Math fixture only", "source_url": "fixture://sgp",
         "input_hash": "a" * 64,
         "request": {"dataset_id": "fixture", "home_id": "H", "away_id": "A", "tipoff": tipoff.isoformat(),
                     "cutoff": cutoff.isoformat(), "simulations": 20, "seed": 42},
         "model": {"version": "fixture", "feature_hash": "b" * 64,
                   "teams": [{"players": [{"player_id": pid, "known_at": cutoff.isoformat()} for pid in ("p1", "p2")]}]},
         "result": {"players": players, "score_samples": [[100, 90]] * 20}}
    return f, now


def legs():
    return [Leg(player_id="p1", stat="pts", side="over", line=20.5),
            Leg(player_id="p1", stat="reb", side="at_least", line=10)]


def quote(forecast, selections, as_of):
    return {"quote_id": "fictional-book-quote", "sportsbook": "Test book (fictional)",
            "decimal_odds": 3, "leg_keys": [leg_key(leg) for leg in selections],
            "home_id": "H", "away_id": "A", "tipoff": forecast["request"]["tipoff"],
            "observed_at": (as_of - timedelta(seconds=30)).isoformat(),
            "retrieved_at": (as_of - timedelta(seconds=20)).isoformat(),
            "source_reference": "Fictional quote used only for unit tests", "source_url": "fixture://quote"}


def test_positive_dependence_uses_conjunction_not_independence():
    f, now = fixture()
    result = evaluate_sgp(f, legs(), as_of=now)
    assert result["joint_probability"] == .5
    assert [leg["probability"] for leg in result["legs"]] == [.5, .5]
    assert result["dependence"]["independence_product_diagnostic"] == .25
    assert result["dependence"]["lift_vs_independence"] == 2
    assert result["joint_hits"] == 10 and result["sample_count"] == 20
    assert result["quote_status"] == "MISSING"
    assert result["expected_return_per_unit"] is None
    assert result["pricing_status"] == "NOT_MEASURABLE"
    assert result["use_status"] == "RESEARCH_ONLY" and result["actionable"] is False
    assert result["scenario_robustness"]["status"] == "UNAVAILABLE"


def test_negative_dependence_does_not_multiply_marginals():
    f, now = fixture()
    selections = [Leg(player_id=pid, stat="pts", side="at_least", line=30) for pid in ("p1", "p2")]
    result = evaluate_sgp(f, selections, as_of=now)
    assert result["joint_probability"] == 0
    assert result["dependence"]["independence_product_diagnostic"] == .25
    assert result["dependence"]["lift_vs_independence"] == 0
    assert result["interval_95"][0] == 0
    assert result["interval_95"][1] > 0


@pytest.mark.parametrize("stat,threshold", [("PRA", 46), ("points_rebounds", 40),
                                            ("points_assists", 36), ("rebounds_assists", 16)])
def test_combination_stats_sum_components_on_same_game_index(stat, threshold):
    f, now = fixture()
    selections = [Leg(player_id="p1", stat=stat, side="at_least", line=threshold),
                  Leg(player_id="p2", stat="blk", side="over", line=.5)]
    result = evaluate_sgp(f, selections, as_of=now)
    assert result["joint_probability"] == .5
    assert result["weakest_leg"]["player_id"] == "p1"


def test_real_label_with_exact_fresh_quote_only_returns_illustrative_arithmetic():
    f, now = fixture()
    selections = legs()
    q = quote(f, selections, now)
    result = evaluate_sgp(f, selections, quote=q, as_of=now)
    assert result["quote_status"] == "VALID"
    assert result["break_even_probability"] == pytest.approx(1 / 3)
    assert result["expected_return_per_unit"] == .5
    assert result["pricing_status"] == "ILLUSTRATIVE_RESEARCH_ONLY"
    assert result["actionable"] is False
    assert any("calibration" in reason for reason in result["betting_block_reasons"])
    assert any("authenticity" in reason for reason in result["betting_block_reasons"])
    assert result["provenance"]["input_hash"] == f["input_hash"]
    assert result["provenance"]["seed"] == 42
    assert result["availability_evidence"]["status"] == "UNVERIFIED"
    assert result["quote"]["quote_id"] == q["quote_id"]


def test_synthetic_forecast_cannot_produce_betting_ev_even_with_quote():
    f, now = fixture(synthetic=True)
    result = evaluate_sgp(f, legs(), quote=quote(f, legs(), now), as_of=now)
    assert result["quote_status"] == "VALID"
    assert result["expected_return_per_unit"] is None
    assert result["expected_return_interval_95"] is None
    assert result["pricing_status"] == "NOT_MEASURABLE"
    assert result["actionable"] is False
    assert "Synthetic forecast" in result["betting_block_reasons"][0]


@pytest.mark.parametrize("mutation", [
    {"leg_keys": ["wrong", "keys"]},
    {"home_id": "wrong-team"},
    {"sportsbook": "  "},
    {"decimal_odds": 1},
    {"decimal_odds": float("nan")},
    {"source_reference": ""},
    {"source_reference": "  "},
    {"leg_keys": []},
])
def test_invalid_combined_quote_never_produces_ev(mutation):
    f, now = fixture()
    q = {**quote(f, legs(), now), **mutation}
    result = evaluate_sgp(f, legs(), quote=q, as_of=now)
    assert result["quote_status"] == "INVALID"
    assert result["expected_return_per_unit"] is None
    assert result["break_even_probability"] is None
    assert result["quote_reasons"]


@pytest.mark.parametrize("kind", ["stale", "future", "retrieved_before_observed", "wrong_tipoff", "naive"])
def test_quote_time_and_event_identity_validation(kind):
    f, now = fixture()
    q = quote(f, legs(), now)
    if kind == "stale":
        q["observed_at"] = (now - timedelta(seconds=301)).isoformat()
    elif kind == "future":
        q["observed_at"] = q["retrieved_at"] = (now + timedelta(seconds=1)).isoformat()
    elif kind == "retrieved_before_observed":
        q["retrieved_at"] = (now - timedelta(seconds=31)).isoformat()
    elif kind == "wrong_tipoff":
        q["tipoff"] = (now + timedelta(hours=2)).isoformat()
    else:
        q["observed_at"] = "2026-01-01T00:00:00"
    result = evaluate_sgp(f, legs(), quote=q, as_of=now)
    assert result["quote_status"] == "INVALID"
    assert result["expected_return_per_unit"] is None


@pytest.mark.parametrize("side,line", [("over", 20), ("under", 20), ("over", 20.25),
                                        ("under", 20.75), ("at_least", 20.5), ("at_least", True)])
def test_push_ambiguous_or_invalid_thresholds_are_rejected(side, line):
    with pytest.raises(ValidationError):
        Leg(player_id="p1", stat="pts", side=side, line=line)


@pytest.mark.parametrize("second", [
    {"player_id": "p1", "stat": "pts", "side": "under", "line": 20.5},
    {"player_id": "p1", "stat": "pra", "side": "at_least", "line": 30},
    {"player_id": "p1", "stat": "points_rebounds", "side": "at_least", "line": 30},
])
def test_duplicate_and_overlapping_player_stat_constructions_are_rejected(second):
    f, now = fixture()
    with pytest.raises(ValueError, match="overlapping"):
        evaluate_sgp(f, [legs()[0], second], as_of=now)


def test_unknown_player_wrong_team_and_missing_aligned_samples_fail():
    f, now = fixture()
    unknown = {"player_id": "unknown", "stat": "pts", "side": "over", "line": 20.5}
    with pytest.raises(ValueError, match="Unknown forecast player"):
        evaluate_sgp(f, [legs()[0], unknown], as_of=now)
    wrong = deepcopy(f)
    wrong["result"]["players"][0]["team_id"] = "not-this-game"
    with pytest.raises(ValueError, match="exact game"):
        evaluate_sgp(wrong, legs(), as_of=now)
    missing = deepcopy(f)
    missing["result"]["players"][0]["samples"]["reb"] = [0]
    with pytest.raises(ValueError, match="same complete frozen game sample"):
        evaluate_sgp(missing, legs(), as_of=now)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -.5, 1.5, True])
def test_noncount_or_nonfinite_samples_are_rejected(invalid):
    f, now = fixture()
    f["result"]["players"][0]["samples"]["pts"][0] = invalid
    with pytest.raises(ValueError, match="nonnegative integer"):
        evaluate_sgp(f, legs(), as_of=now)


def test_future_evaluation_post_tipoff_and_future_forecast_are_rejected():
    f, now = fixture()
    with pytest.raises(ValueError, match="future"):
        evaluate_sgp(f, legs(), as_of=now + timedelta(days=1))
    posttipoff = deepcopy(f)
    posttipoff["request"]["tipoff"] = (now - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="pregame forecast"):
        evaluate_sgp(posttipoff, legs(), as_of=now)
    future = deepcopy(f)
    future["created_at"] = (now + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="pregame forecast"):
        evaluate_sgp(future, legs(), as_of=now)


def test_stale_missing_roster_evidence_remains_blocked():
    f, now = fixture()
    f["model"]["teams"][0]["players"][0]["known_at"] = (now - timedelta(days=1)).isoformat()
    result = evaluate_sgp(f, legs(), as_of=now)
    assert result["availability_evidence"]["fresh_snapshot_count"] == 0
    assert any("lack a fresh" in reason for reason in result["betting_block_reasons"])
    assert result["actionable"] is False


def test_unambiguous_leg_keys_preserve_exact_market_identity():
    first = Leg(player_id='p|1:escaped"', stat="pts", side="at_least", line=20)
    second = Leg(player_id='p|1:escaped"', stat="pts", side="over", line=20.5)
    assert leg_key(first) != leg_key(second)
    assert leg_key(first) == '["p|1:escaped\\\"","pts","at_least",20.0]'


def test_input_snapshot_is_not_mutated():
    f, now = fixture()
    original = deepcopy(f)
    evaluate_sgp(f, legs(), as_of=now)
    assert f == original
