from collections import Counter
from copy import deepcopy
from itertools import combinations, product
import math
import random

import pytest

from nba_quant.family_optimizer import select_family


def candidate(pid, team, stat, p=.8, price=1.5, mean=-.03, rank=-.1, line=10):
    return {"id": f"{pid}-{stat}-{line}", "player_id": pid, "player_name": pid,
            "team_id": team, "stat": stat, "side": "over", "line": line,
            "offer": {"sportsbook": "polymarket", "decimal_odds": price,
                      "source_sha256": "fictional-test-source-only"},
            "aggressive_evidence": {"uncertainty_adjusted_probability": p,
                                    "ranking_score": rank,
                                    "proxy_return_after_entry_fee": mean,
                                    "ranking_basis": "ENTRY_FEE_ADJUSTED_RESEARCH_PROXY"}}


def pool():
    return [candidate(f"{team}-{i}", team, stat, p=.8 + i * .01)
            for team in ("H", "A") for i, stat in enumerate(("pts", "reb", "ast", "fg3m"))]


def check_composition(result, size):
    assert len(result["legs"]) == size
    assert len({c["player_id"] for c in result["legs"]}) == size
    assert {c["team_id"] for c in result["legs"]} <= {"H", "A"}
    assert max(Counter(c["stat"] for c in result["legs"]).values()) <= math.ceil(size / 2)
    assert result["diagnostics"]["joint_probability"] is None
    assert result["diagnostics"]["expected_return_per_unit"] is None
    assert result["diagnostics"]["combined_odds"] is None


def test_full_five_and_eight_coverage_is_not_blocked_by_negative_support_edges():
    candidates = pool()
    for size in (5, 8):
        result = select_family(candidates, size, ("H", "A"))
        check_composition(result, size)
        assert result["diagnostics"]["status"] == "BEST_AVAILABLE_COVERAGE"
        assert result["diagnostics"]["negative_mean_proxy_legs"] == size
        assert result["diagnostics"]["anchors_achieved"] == 0
        assert result["diagnostics"]["anchor_shortfall"] == (2 if size == 5 else 3)
        assert all(c["family_role"] == "support" for c in result["legs"])


def test_family_specific_anchor_targets_and_support_choices_use_whole_ladders():
    candidates = pool()
    # Every player has a high-support low line plus a source-valid anchor line.
    for c in list(candidates):
        candidates.append(candidate(c["player_id"], c["team_id"], c["stat"], p=.3,
                                    price=3, mean=.2, rank=.04, line=30))
    five = select_family(candidates, 5, ("H", "A"))
    eight = select_family(candidates, 8, ("H", "A"))
    check_composition(five, 5)
    check_composition(eight, 8)
    assert five["diagnostics"]["anchors_achieved"] == 2
    assert eight["diagnostics"]["anchors_achieved"] == 3
    assert Counter(c["line"] for c in five["legs"]) == {30: 2, 10: 3}
    assert Counter(c["line"] for c in eight["legs"]) == {30: 3, 10: 5}


def test_only_positive_mean_and_decimal_at_least_two_qualify_as_anchors():
    candidates = pool()
    candidates[0] = candidate("H-0", "H", "pts", price=2, mean=.2, rank=-.1)
    candidates[1] = candidate("H-1", "H", "reb", price=1.99, mean=.9, rank=.8)
    candidates[2] = candidate("H-2", "H", "ast", price=3, mean=0, rank=.1)
    result = select_family(candidates, 8, ("H", "A"))
    assert result["diagnostics"]["anchors_achieved"] == 1
    assert result["diagnostics"]["anchor_source_players"] == ["H-0"]
    assert result["diagnostics"]["lack_positive_uncertainty_legs"] >= 1


def test_infeasible_stat_counts_never_produce_fabricated_coverage():
    candidates = [candidate(f"H-{i}", "H", "pts") for i in range(8)]
    result = select_family(candidates, 8, ("H", "A"))
    assert result["legs"] == []
    assert result["diagnostics"]["status"] == "BLOCKED_INSUFFICIENT_FEASIBLE_SOURCE_COVERAGE"
    assert result["diagnostics"]["min_probability"] is None


def test_eight_legs_with_one_threes_player_and_three_points_players_are_feasible():
    stats = ("pts", "pts", "pts", "reb", "reb", "ast", "ast", "fg3m")
    candidates = [candidate(str(i), "H" if i % 2 else "A", stat,
                            price=3 if i < 3 else 1.5, mean=.2 if i < 3 else -.1)
                  for i, stat in enumerate(stats)]
    result = select_family(candidates, 8, ("H", "A"))
    check_composition(result, 8)
    assert result["diagnostics"]["stat_cap"] == 4
    assert result["diagnostics"]["anchors_achieved"] == 3
    assert Counter(c["stat"] for c in result["legs"]) == {"pts": 3, "reb": 2, "ast": 2, "fg3m": 1}


def test_same_team_source_coverage_is_allowed_and_concentration_is_explicit():
    candidates = [candidate(str(i), "H", stat) for i, stat in enumerate(
        ("pts", "pts", "pts", "reb", "reb", "ast", "ast", "fg3m"))]
    result = select_family(candidates, 8, ("H", "A"))
    assert len(result["legs"]) == 8
    assert result["diagnostics"]["same_team_only"] is True
    assert result["diagnostics"]["team_counts"] == {"A": 0, "H": 8}
    assert result["diagnostics"]["distinct_stats"] == 4


def test_team_diversity_is_only_a_tie_preference_not_a_probability_penalty():
    candidates = [candidate("H-points", "H", "pts"), candidate("H-reb", "H", "reb"),
                  candidate("H-assists", "H", "ast"), candidate("A-assists", "A", "ast")]
    tied = select_family(candidates, 3, ("H", "A"))
    assert tied["diagnostics"]["same_team_only"] is False
    candidates[-1]["aggressive_evidence"]["uncertainty_adjusted_probability"] = .1
    higher_support = select_family(candidates, 3, ("H", "A"))
    assert higher_support["diagnostics"]["same_team_only"] is True


def test_complete_assignment_avoids_flexible_player_greedy_dead_end():
    candidates = [candidate("H-flex", "H", "pts", p=.99),
                  candidate("H-flex", "H", "reb", p=.8),
                  candidate("H-pts", "H", "pts", p=.8),
                  candidate("A-pts", "A", "pts", p=.8)]
    result = select_family(candidates, 3, ("H", "A"))
    check_composition(result, 3)
    assert {(c["player_id"], c["stat"]) for c in result["legs"]} == {
        ("H-flex", "reb"), ("H-pts", "pts"), ("A-pts", "pts")}


def test_anchor_quality_precedes_support_objective_and_support_alternative_uses_largest_p():
    candidates = pool()
    candidates += [candidate("H-0", "H", "pts", p=.3, price=3, mean=.2, rank=.5, line=30),
                   candidate("H-0", "H", "pts", p=.5, price=2.5, mean=.3, rank=.4, line=20),
                   candidate("A-0", "A", "pts", p=.4, price=3, mean=.2, rank=.3, line=30),
                   candidate("H-1", "H", "reb", p=.95, price=1.1, mean=-.1, rank=-.2, line=5)]
    result = select_family(candidates, 5, ("H", "A"))
    anchors = [c for c in result["legs"] if c["family_role"] == "anchor"]
    assert {c["player_id"] for c in anchors} == {"H-0", "A-0"}
    assert next(c for c in anchors if c["player_id"] == "H-0")["line"] == 30
    support = next(c for c in result["legs"] if c["player_id"] == "H-1")
    assert support["line"] == 5


def test_deterministic_and_no_input_mutation_even_when_input_order_is_reversed():
    candidates = pool()
    original = deepcopy(candidates)
    first = select_family(candidates, 8, ("H", "A"))
    second = select_family(list(reversed(candidates)), 8, ("A", "H"))
    assert first["legs"] == second["legs"]
    assert candidates == original
    assert all("family_role" not in c for c in candidates)


@pytest.mark.parametrize("mutation", [
    {"uncertainty_adjusted_probability": float("nan")}, {"uncertainty_adjusted_probability": 1.1},
    {"uncertainty_adjusted_probability": True}, {"ranking_score": float("inf")},
    {"proxy_return_after_entry_fee": None},
])
def test_invalid_or_unsupported_numeric_evidence_is_rejected(mutation):
    candidates = pool()
    candidates[0]["aggressive_evidence"].update(mutation)
    with pytest.raises(ValueError):
        select_family(candidates, 8, ("H", "A"))


@pytest.mark.parametrize("price", [True, float("nan"), float("inf"), 1, "2.5"])
def test_no_missing_or_fake_price_is_substituted(price):
    candidates = pool()
    candidates[0]["offer"]["decimal_odds"] = price
    with pytest.raises(ValueError, match="actual finite offered"):
        select_family(candidates, 8, ("H", "A"))


def test_gross_only_compatibility_remains_explicit_in_diagnostics():
    candidates = pool()
    e = candidates[0]["aggressive_evidence"]
    e.update(proxy_return_after_entry_fee=None, proxy_return_gross=.1,
             ranking_basis="GROSS_RESEARCH_ONLY_FEES_UNAVAILABLE")
    candidates[0]["offer"]["decimal_odds"] = 3
    result = select_family(candidates, 8, ("H", "A"))
    assert result["diagnostics"]["gross_only_fee_unknown_legs"] == 1
    assert result["diagnostics"]["anchors_achieved"] == 1


def exhaustive_objective(candidates, size, target):
    best = None
    for selected in combinations(candidates, size):
        if len({c["player_id"] for c in selected}) != size:
            continue
        if max(Counter(c["stat"] for c in selected).values()) > math.ceil(size / 2):
            continue
        for roles in product((False, True), repeat=size):
            if sum(roles) > target:
                continue
            if any(role and not (c["offer"]["decimal_odds"] >= 2 and c["aggressive_evidence"]["proxy_return_after_entry_fee"] > 0)
                   for role, c in zip(roles, selected)):
                continue
            objective = (sum(roles), sum(c["aggressive_evidence"]["ranking_score"] for role, c in zip(roles, selected) if role),
                         sum(math.log(max(c["aggressive_evidence"]["uncertainty_adjusted_probability"], 1e-12)) for c in selected))
            if best is None or objective > best:
                best = objective
    return best


def test_dynamic_program_agrees_with_exhaustive_small_family_role_assignments():
    rng = random.Random(91)
    for _ in range(20):
        candidates = [candidate(f"{team}-{i}", team, rng.choice(("pts", "reb", "ast")),
                                p=rng.choice((.3, .6, .9)), price=rng.choice((1.5, 3)),
                                mean=rng.choice((-.1, .2)), rank=rng.choice((-.2, .1, .3)))
                      for team in ("H", "A") for i in range(2)]
        # Alternative line for an existing player tests lossless role reduction.
        c = candidates[0]
        candidates.append(candidate(c["player_id"], c["team_id"], c["stat"], p=.7, price=3, mean=.1, rank=.2, line=20))
        expected = exhaustive_objective(candidates, 3, 1)
        result = select_family(candidates, 3, ("H", "A"))
        if expected is None:
            assert result["legs"] == []
        else:
            diagnostic = result["diagnostics"]
            assert (diagnostic["anchors_achieved"], diagnostic["anchor_score_sum"], diagnostic["sum_log_support"]) == pytest.approx(expected)


def test_zero_support_probability_is_disclosed_without_nan_or_joint_probability():
    candidates = pool()
    candidates[0]["aggressive_evidence"]["uncertainty_adjusted_probability"] = 0
    result = select_family(candidates, 8, ("H", "A"))
    assert result["diagnostics"]["min_probability"] == 0
    assert math.isfinite(result["diagnostics"]["sum_log_support"])
    assert result["diagnostics"]["joint_probability"] is None
