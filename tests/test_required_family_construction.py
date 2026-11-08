from copy import deepcopy

from nba_quant.api import nightly_requirements
from nba_quant.construction import construct_game_drafts
from test_construction import fixture, offered


def test_nightly_five_and_eight_use_all_valid_ladders_and_disclose_support_costs():
    game, dataset, players, now = fixture()
    # Actual-price fixtures, not live market claims. Eight distinct players with
    # four points selections demonstrate why the old two-per-stat cap was wrong.
    specifications = [
        ("H-0", "H", "pts", 22.5, 5.), ("H-1", "H", "pts", 22.5, 5.),
        ("A-0", "A", "ast", 5.5, 2.1),
        ("H-2", "H", "pts", 20.5, 1.01), ("H-3", "H", "reb", 7.5, 1.01),
        ("A-1", "A", "pts", 20.5, 1.01), ("A-2", "A", "ast", 5.5, 1.01),
        ("A-3", "A", "reb", 7.5, 1.01),
    ]
    quotes = [{**offered(game, now), "player_id": pid, "team_id": tid,
               "stat": stat, "line": line, "decimal_odds": price}
              for pid, tid, stat, line, price in specifications]
    before = deepcopy((dataset, players, quotes))
    result = construct_game_drafts(game, dataset, players, quotes, now, "aggressive")
    for size, anchor_count in ((5, 2), (8, 3)):
        family = next(f for f in result["families"] if f["leg_count"] == size)
        assert len(family["legs"]) == size
        assert len({leg["player_id"] for leg in family["legs"]}) == size
        assert sum(p["family_role"] == "anchor" for p in family["legs"]) == anchor_count
        assert family["nightly_priority"] is True
        assert family["optimization"]["stat_cap"] == (size + 1) // 2
        assert family["joint_probability"] is None and family["combined_odds"] is None
        assert all(p["offer"] in quotes for p in family["legs"])
    eight = result["families"][2]
    assert eight["optimization"]["negative_mean_proxy_legs"] >= 2
    assert any(p["aggressive_evidence"]["eligible"] is False and p["coverage_eligible"] for p in eight["legs"])
    assert nightly_requirements(result["families"])["missing_sizes"] == []
    assert (dataset, players, quotes) == before


def test_nightly_requirement_reports_real_shortfall_and_never_fakes_legs():
    requirements = nightly_requirements([{"leg_count": 5, "legs": [{}] * 5}, {"leg_count": 8, "legs": []}])
    assert requirements["status"] == "PARTIAL"
    assert requirements["missing_sizes"] == [8]
    assert requirements["available_by_size"] == {"5": 1, "8": 0}
    assert nightly_requirements([])["status"] == "UNAVAILABLE"
