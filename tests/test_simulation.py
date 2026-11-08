import copy
import random
from collections import Counter

import numpy as np
import pytest

from nba_quant.simulation import BOX_KEYS, field_goal_outcome, game, quotas, rotation, run
from helpers import prepared_fixture


@pytest.mark.parametrize("seed", range(12))
def test_event_ledger_reconstructs_every_box_and_score(seed):
    prepared = prepared_fixture()
    result = game(prepared, seed, trace=True)
    rebuilt = {pid: {k: 0 for k in BOX_KEYS} for pid in result["boxes"]}
    team_ids = [t["id"] for t in prepared["teams"]]
    team_points, team_rebounds, possessions = Counter(), Counter(), Counter()
    for stint in result["lineups"]:
        assert stint["duration_seconds"] == 30
        for players in stint["teams"]:
            assert len(set(players)) == 5
            for pid in players:
                rebuilt[pid]["minutes"] += stint["duration_seconds"] / 60
    ids = []
    for e in result["events"]:
        ids.append(e["id"])
        assert not (e["second_chance"] and e["clock_seconds"] <= 0)
        side = team_ids.index(e["team_id"])
        own, opp = e["on_court"][side], e["on_court"][1 - side]
        pid = e["player_id"]
        assert pid in own
        assert len(set(own)) == len(set(opp)) == 5
        if not e["second_chance"]:
            possessions[e["team_id"]] += 1
        if e["type"] == "turnover":
            rebuilt[pid]["tov"] += 1
            assert "rebound_id" not in e and "assist_id" not in e
            if "steal_id" in e:
                assert e["steal_id"] in opp
                rebuilt[e["steal_id"]]["stl"] += 1
        else:
            points = e["points"]
            rebuilt[pid]["pts"] += points
            team_points[e["team_id"]] += points
            if e["type"] == "shooting_foul":
                rebuilt[pid]["fta"] += e["fta"]
                rebuilt[pid]["ftm"] += e["ftm"]
                assert points == e["ftm"] <= e["fta"]
                missed_live = not e["last_free_throw_made"]
                assert "assist_id" not in e
            else:
                assert e["type"] == "field_goal"
                rebuilt[pid]["fga"] += 1
                rebuilt[pid]["fg3a"] += int(e["zone"] == "three")
                rebuilt[pid]["fgm"] += int(e["made"])
                rebuilt[pid]["fg3m"] += int(e["made"] and e["zone"] == "three")
                assert points == ((3 if e["zone"] == "three" else 2) if e["made"] else 0)
                missed_live = not e["made"]
                if "block_id" in e:
                    assert e["block_id"] in opp and not e["made"]
                    rebuilt[e["block_id"]]["blk"] += 1
                if "assist_id" in e:
                    assert e["made"] and e["assist_id"] in own and e["assist_id"] != pid
                    rebuilt[e["assist_id"]]["ast"] += 1
            if missed_live:
                assert ("rebound_id" in e) ^ ("team_rebound_id" in e)
                rebound_side = side if e["offensive_rebound"] else 1 - side
                if "rebound_id" in e:
                    assert e["rebound_id"] in e["on_court"][rebound_side]
                    rebuilt[e["rebound_id"]]["oreb" if e["offensive_rebound"] else "dreb"] += 1
                else:
                    assert e["team_rebound_id"] == team_ids[rebound_side]
                    team_rebounds[e["team_rebound_id"]] += 1
            else:
                assert "rebound_id" not in e and "team_rebound_id" not in e
    assert ids == list(range(1, len(ids) + 1))
    for pid, b in rebuilt.items():
        for k in BOX_KEYS:
            assert b[k] == result["boxes"][pid][k], (pid, k)
        assert result["boxes"][pid]["reb"] == b["oreb"] + b["dreb"]
        assert b["pts"] == 2 * b["fgm"] + b["fg3m"] + b["ftm"]
    assert [team_points[tid] for tid in team_ids] == result["score"]
    assert [possessions[tid] for tid in team_ids] == result["possessions"]
    assert [team_rebounds[tid] for tid in team_ids] == result["team_rebounds"]
    for t in prepared["teams"]:
        assert sum(rebuilt[p["player_id"]]["minutes"] for p in t["players"]) == 240 + 25 * result["overtime_periods"]
    assert result["score"][0] != result["score"][1]


def test_reproducible_seed_and_actual_overtime_periods():
    prepared = prepared_fixture(5)
    overtime_game = None
    for seed in range(200):
        result = game(prepared, seed, trace=True)
        if result["overtime_periods"]:
            overtime_game = result
            assert result == game(prepared, seed, trace=True)
            break
    assert overtime_game is not None
    n = overtime_game["overtime_periods"]
    assert len(overtime_game["lineups"]) == 96 + 10 * n
    assert max(e["period"] for e in overtime_game["events"]) == 4 + n
    assert all(b["minutes"] == 48 + 5 * n for b in overtime_game["boxes"].values())


def test_buzzer_offensive_rebound_does_not_create_another_shot():
    events = game(prepared_fixture(), 1, trace=True)["events"]
    endpoints = [(i, e) for i, e in enumerate(events)
                 if e["clock_seconds"] == 0 and e.get("offensive_rebound")]
    assert endpoints, "Fixture must actually exercise a retained rebound at the buzzer"
    for i, event in endpoints:
        assert i == len(events) - 1 or events[i + 1]["period"] > event["period"]


def test_out_player_and_restricted_minutes_remain_consistent():
    prepared = prepared_fixture()
    out = prepared["teams"][0]["players"][0]
    out["active_probability"] = 0
    restricted = prepared["teams"][0]["players"][1]
    restricted["minutes_cap"] = 12.25
    for seed in range(20):
        result = game(prepared, seed)
        assert all(v == 0 for v in result["boxes"][out["player_id"]].values())
        assert result["boxes"][restricted["player_id"]]["minutes"] <= 12
        assert out["player_id"] not in result["active_player_ids"][0]


def test_infeasible_restrictions_fail_without_fabricated_rotation():
    prepared = prepared_fixture(5)
    prepared["teams"][0]["players"][0]["minutes_cap"] = 47
    with pytest.raises(ValueError, match="cannot reliably supply"):
        game(prepared, 42)


def test_availability_conditioning_is_disclosed_and_participation_reported():
    prepared = prepared_fixture(5)
    prepared["teams"][0]["players"][0]["active_probability"] = .5
    result = run(prepared, 20, 42)
    player = result["players"][0]
    assert player["configured_active_probability"] == .5
    assert player["sampled_active_probability"] == player["played_probability"] == 1
    assert result["availability_rejections"] > 0
    assert "conditioned" in result["availability_sampling"]


def test_rotation_exhausts_feasible_quota_with_no_duplicate_players():
    rng = random.Random(42)
    for _ in range(100):
        players = copy.deepcopy(prepared_fixture(rng.randrange(5, 16))["teams"][0]["players"])
        for p in players:
            p["expected_minutes"] = rng.uniform(0, 48)
        q = quotas(players, rng)
        assert sum(q) == 480
        actual = Counter()
        expected = dict(zip((p["player_id"] for p in players), q))
        for remaining in range(96, 0, -1):
            lineup = rotation(players, q, remaining, rng)
            assert len({p["player_id"] for p in lineup}) == 5
            actual.update(p["player_id"] for p in lineup)
        assert all(n == 0 for n in q)
        assert all(actual[pid] == n for pid, n in expected.items())


def test_monte_carlo_outputs_reconcile_samples_and_intervals():
    prepared = prepared_fixture()
    result = run(prepared, 20, 42)
    assert result["home_win_probability"] + result["away_win_probability"] == 1
    sample = np.array(result["score_samples"])
    assert result["home_win_probability"] == np.mean(sample[:, 0] > sample[:, 1])
    lo, hi = result["win_probability_monte_carlo_interval_95"]
    assert 0 <= lo < hi <= 1
    for side, team in enumerate(prepared["teams"]):
        players = [p for p in result["players"] if p["team_id"] == team["id"]]
        assert np.array_equal(sum(np.array(p["samples"]["pts"]) for p in players), sample[:, side])
    corr = np.array(result["correlations"]["matrix"])
    assert np.isfinite(corr).all()
    assert np.allclose(corr, corr.T)
    for p in result["players"]:
        assert p["thresholds"]["pts_ge_30"] <= p["thresholds"]["pts_ge_20"]
        assert p["stats"]["pts"]["p05"] <= p["stats"]["pts"]["p95"]


class UniformGrid:
    """Deterministic uniform quadrature avoids a noisy empirical rate assertion."""
    def __init__(self, size):
        self.size = size
        self.index = 0

    def random(self):
        draw = (self.index + .5) / self.size
        self.index += 1
        return draw


@pytest.mark.parametrize("make,block", [(.53, .055), (.36, .018), (.85, .055), (.1, .018)])
def test_blocked_attempts_do_not_reduce_unconditional_make_target(make, block):
    rng = UniformGrid(10000)
    outcomes = [field_goal_outcome(rng, make, block) for _ in range(rng.size)]
    assert sum(made for made, _ in outcomes) / rng.size == pytest.approx(make, abs=.0001)
    assert sum(blocked for _, blocked in outcomes) / rng.size == pytest.approx(block, abs=.0001)
    assert all(not (made and blocked) for made, blocked in outcomes)
    # This specifically rejects the old independent-block double penalty.
    assert sum(made for made, _ in outcomes) / rng.size > make * (1 - block)


@pytest.mark.parametrize("make,block,expected_make,expected_block", [
    (1., 1., 1., 0.), (0., 1., 0., 1.), (0., 0., 0., 0.),
    (.9, .8, .9, .1), (.5, 0., .5, 0.),
])
def test_shot_outcome_limits_never_have_negative_or_impossible_probability_mass(make, block, expected_make, expected_block):
    rng = UniformGrid(10000)
    outcomes = [field_goal_outcome(rng, make, block) for _ in range(rng.size)]
    assert sum(made for made, _ in outcomes) / rng.size == pytest.approx(expected_make, abs=.0001)
    assert sum(blocked for _, blocked in outcomes) / rng.size == pytest.approx(expected_block, abs=.0001)
    assert all(not (made and blocked) for made, blocked in outcomes)


@pytest.mark.parametrize("invalid", [-.1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_invalid_shot_probability_inputs_are_rejected(invalid):
    with pytest.raises(ValueError, match="finite numbers"):
        field_goal_outcome(random.Random(42), invalid, .055)
    with pytest.raises(ValueError, match="finite numbers"):
        field_goal_outcome(random.Random(42), .53, invalid)
