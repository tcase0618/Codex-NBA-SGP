"""Review grading checks: analytical thresholds are distinct from book settlement."""
import importlib.util
import json
import math
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/grade_pdf_parlays.py"
spec = importlib.util.spec_from_file_location("review_grader_under_test", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)

LIVE_SPEC = importlib.util.spec_from_file_location("review_live_under_test", SCRIPT.with_name("estimate_review_live.py"))
live = importlib.util.module_from_spec(LIVE_SPEC)
LIVE_SPEC.loader.exec_module(live)


def leg(pid="player-1", line=10):
    return {"player_id": pid, "team_id": "team-1", "name": pid,
            "stat": "pts", "side": "at_least", "line": line}


def box(points=10, completed=True, dnp=False):
    return {"completed": completed, "canonical_status": "VALIDATED_FINAL" if completed else "NOT_FINAL",
            "players": [{"id": "player-1", "team_id": "team-1", "name": "Fictional player",
                         "did_not_play": dnp, "row_flags": [],
                         "stats": {} if dnp else {"pts": points, "minutes": 30, "pf": 2}}]}


@pytest.mark.parametrize("points,completed,expected", [(10, True, "PASS"), (9, True, "FAIL"),
                                                      (10, False, "PASS"), (9, False, "PENDING")])
def test_final_and_live_threshold_statuses_remain_distinct(points, completed, expected):
    result = review.grade_leg(leg(), box(points, completed))
    assert result["status"] == expected
    assert result["actual"] == points
    assert result["remaining"] == max(0, 10 - points)


def test_explicit_dnp_is_neither_zero_stat_nor_assumed_book_void():
    family = {"size": 3, "legs": [leg()]}
    result = review.grade_family(family, box(dnp=True))
    assert result["status"] == "DNP_RULES_UNVERIFIED"
    assert result["hit_probability"] is None
    assert result["legs"][0]["status"] == "DNP"
    assert result["legs"][0]["actual"] is None


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_team", "missing_stat", "inconsistent"])
def test_source_identity_or_stat_ambiguity_is_unresolved(change):
    source = box()
    if change == "missing":
        source["players"] = []
    elif change == "duplicate":
        source["players"] += source["players"]
    elif change == "wrong_team":
        source["players"][0]["team_id"] = "other-team"
    elif change == "missing_stat":
        del source["players"][0]["stats"]["pts"]
    else:
        source["players"][0]["row_flags"] = ["Inconsistent scoring identity"]
    result = review.grade_family({"size": 3, "legs": [leg()]}, source)
    assert result["status"] == "UNRESOLVED"
    assert result["hit_probability"] is None


def test_all_attained_live_thresholds_are_secured_not_final_hit():
    family = {"size": 3, "legs": [leg()]}
    assert review.grade_family(family, box(completed=False))["status"] == "SECURED"
    assert review.grade_family(family, box(completed=True))["status"] == "HIT"


def test_empty_family_remains_blocked_and_no_probability_invented():
    result = review.grade_family({"size": 8, "legs": []}, box())
    assert result["status"] == "BLOCKED"
    assert result["hit_probability"] is None


def test_analytical_miss_with_another_dnp_does_not_fabricate_dnp_leg_loss():
    source = box(points=9)
    source["players"].append({"id": "player-2", "team_id": "team-1", "name": "Fictional DNP",
                              "did_not_play": True, "stats": {}, "row_flags": []})
    result = review.grade_family({"size": 3, "legs": [leg(), leg("player-2")]}, source)
    assert result["status"] == "MISS"
    assert [row["status"] for row in result["legs"]] == ["FAIL", "DNP"]


def test_unreconciled_final_box_is_not_given_threshold_grade():
    source = box()
    source["canonical_status"] = "INVALID_FINAL"
    result = review.grade_leg(leg(), source)
    assert result["status"] == "UNRESOLVED" and result["actual"] is None


@pytest.mark.parametrize("period,clock,minutes", [(1, "12:00", 48), (3, "6:00", 18),
                                               (4, "8:15", 8.25), (4, "0.0", 0), (5, "2:30", 2.5)])
def test_live_remaining_clock(period, clock, minutes):
    assert live.remaining_regulation(period, clock) == minutes


def live_fixture(tmp_path, monkeypatch, families, history_count=3, clock="6:00", history_transform=None):
    history = {"games": [{"id": f"history-{i}", "known_at": "2026-01-01T03:00:00Z",
                          "completed_at": "2026-01-01T02:00:00Z", "tipoff": f"2025-12-{20+i}T00:00:00Z",
                          "boxes": [{"player_id": "p1", "team_id": "t1", "minutes": 10, "pts": 5}]}
                         for i in range(history_count)]}
    if history_transform:
        history_transform(history)
    report = {"games": [{"game": "Explicit fictional live test", "box": {"completed": False, "period": 3 if clock != "0.0" else 4, "clock": clock},
                          "families": families}]}
    (tmp_path / "graded-review.json").write_text(json.dumps(report))
    (tmp_path / "frozen-input.json").write_text(json.dumps({"scan_meta": {"scanned_at": "2026-01-02T00:00:00Z"}}))
    monkeypatch.setattr(live, "OUT", tmp_path)
    # Never instantiate or mutate the actual research database from these tests.
    monkeypatch.setattr(live, "Store", lambda _: None)
    monkeypatch.setattr(live, "merged_history", lambda _: history)
    live.main()
    return json.loads((tmp_path / "graded-review-live.json").read_text())


def pending(line=8):
    return {"player_id": "p1", "team_id": "t1", "stat": "pts", "side": "at_least", "line": line,
            "actual": 6, "minutes": 12, "status": "PENDING"}


def independent_nbinom_tail(needed, shape, beta, exposure):
    p = beta / (beta + exposure)
    # Sum the predictive mass by recurrence, independently of scipy's survival function.
    mass = p**shape
    cdf = mass
    for k in range(1, needed):
        mass *= (shape + k - 1) / k * (1 - p)
        cdf += mass
    return 1 - cdf


def test_single_pending_leg_frechet_bounds_equal_conditional_marginal(tmp_path, monkeypatch):
    family = {"size": 3, "status": "PENDING", "legs": [{"status": "PASS"}, pending(), {"status": "PASS"}]}
    result = live_fixture(tmp_path, monkeypatch, [family])
    graded = result["games"][0]["families"][0]
    estimate = graded["legs"][1]["live_estimate"]
    shape, beta, mid = .5 * 20 + 6 + .5, 20 + 12, 18 * 10 / 48
    for label, exposure in [("conditional_low", .5 * mid), ("conditional_central", mid), ("conditional_high", 1.5 * mid)]:
        assert estimate[label] == pytest.approx(independent_nbinom_tail(2, shape, beta, exposure), abs=1e-12)
    bounds = graded["conditional_probability_bounds"]
    assert bounds["low"] == pytest.approx(estimate["conditional_low"])
    assert bounds["high"] == pytest.approx(estimate["conditional_high"])
    assert bounds["central_minutes_dependence_bounds"] == pytest.approx([estimate["conditional_central"]] * 2)
    assert 0 <= bounds["low"] <= bounds["high"] <= 1
    assert result["live_probability_method"]["family_point_probability"] is None


def test_live_uncertainty_bounds_never_assume_leg_independence(tmp_path, monkeypatch):
    family = {"size": 3, "status": "PENDING", "legs": [{"status": "PASS"}, pending(), pending()]}
    result = live_fixture(tmp_path, monkeypatch, [family])
    graded = result["games"][0]["families"][0]
    p = graded["legs"][1]["live_estimate"]["conditional_central"]
    bounds = graded["conditional_probability_bounds"]["central_minutes_dependence_bounds"]
    assert bounds == pytest.approx([max(0, 2 * p - 1), p])
    assert bounds[0] <= p * p <= bounds[1]


def test_secured_and_dnp_families_are_not_assigned_new_model_estimates(tmp_path, monkeypatch):
    families = [{"size": 3, "status": "SECURED", "hit_probability": 1,
                 "legs": [{"status": "PASS"}] * 3},
                {"size": 3, "status": "DNP_RULES_UNVERIFIED", "hit_probability": None,
                 "legs": [{"status": "DNP"}, pending()]}]
    result = live_fixture(tmp_path, monkeypatch, families)
    secured, dnp = result["games"][0]["families"]
    assert secured["hit_probability"] == 1 and "conditional_probability_bounds" not in secured
    assert dnp["hit_probability"] is None and "conditional_probability_bounds" not in dnp


def test_missing_historical_sample_keeps_live_probability_unavailable(tmp_path, monkeypatch):
    family = {"size": 3, "status": "PENDING", "legs": [pending(), {"status": "PASS"}]}
    result = live_fixture(tmp_path, monkeypatch, [family], history_count=2)
    family = result["games"][0]["families"][0]
    assert family["legs"][0]["live_estimate"] is None
    assert "conditional_probability_bounds" not in family


def test_same_player_different_thresholds_do_not_share_tail_probability(tmp_path, monkeypatch):
    families = [{"size": 3, "status": "PENDING", "legs": [pending(line=n), {"status": "PASS"}]} for n in (8, 12)]
    result = live_fixture(tmp_path, monkeypatch, families)
    low, high = result["games"][0]["families"]
    assert high["legs"][0]["live_estimate"]["required_additional"] == 6
    assert low["legs"][0]["live_estimate"]["conditional_central"] > high["legs"][0]["live_estimate"]["conditional_central"]


def test_zero_remaining_regulation_has_no_nan_or_invented_overtime_chance(tmp_path, monkeypatch):
    family = {"size": 3, "status": "PENDING", "legs": [pending(), {"status": "PASS"}]}
    result = live_fixture(tmp_path, monkeypatch, [family], clock="0.0")
    graded = result["games"][0]["families"][0]
    estimate = graded["legs"][0]["live_estimate"]
    assert estimate["conditional_low"] == estimate["conditional_central"] == estimate["conditional_high"] == 0
    assert graded["conditional_probability_bounds"]["central_minutes_dependence_bounds"] == [0, 0]
    assert "NaN" not in json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("change", ["known_after_cutoff", "completed_after_cutoff", "other_team"])
def test_live_prior_respects_frozen_cutoff_and_current_team(tmp_path, monkeypatch, change):
    def transform(history):
        if change == "known_after_cutoff":
            history["games"][0]["known_at"] = "2026-01-03T00:00:00Z"
        elif change == "completed_after_cutoff":
            history["games"][0]["completed_at"] = "2026-01-03T00:00:00Z"
        else:
            history["games"][0]["boxes"][0]["team_id"] = "former-team"
    family = {"size": 3, "status": "PENDING", "legs": [pending(), {"status": "PASS"}]}
    result = live_fixture(tmp_path, monkeypatch, [family], history_transform=transform)
    assert result["games"][0]["families"][0]["legs"][0]["live_estimate"] is None


@pytest.mark.parametrize("period,clock", [(0, "0.0"), (True, "0.0"), (4, "nan"), (4, "13:00"), (5, "6:00")])
def test_invalid_source_clocks_cannot_silently_create_live_probabilities(period, clock):
    with pytest.raises(ValueError):
        live.remaining_regulation(period, clock)
