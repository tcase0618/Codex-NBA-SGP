from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import combinations
import random

import pytest

from nba_quant.construction import _select, construct_game_drafts


def fixture():
    now = datetime.now(timezone.utc)
    players = [{"id": f"{tid}-{i}", "name": f"Fictional {tid}-{i}", "team_id": tid,
                "position": "PG", "roster_status": "active", "injury_status": "not verified",
                "availability": "UNVERIFIED", "source": {"retrieved_at": (now - timedelta(minutes=1)).isoformat(),
                                                           "source_url": "fixture://current-roster"}}
               for tid in ("H", "A") for i in range(6)]
    games = []
    for n in range(5):
        tipoff = now - timedelta(days=n + 1)
        games.append({"id": f"g{n}", "home_id": "H", "away_id": "A", "tipoff": tipoff.isoformat(),
            "completed_at": (tipoff + timedelta(hours=3)).isoformat(),
            "known_at": (tipoff + timedelta(hours=4)).isoformat(),
            "boxes": [{"player_id": p["id"], "team_id": p["team_id"], "minutes": 30,
                       "pts": 20 + n, "oreb": 2, "dreb": 6, "ast": 6, "fg3m": 3} for p in players]})
    dataset = {"id": "fictional-source-snapshot", "synthetic": False,
        "source": "Fictional fixture (non-synthetic flag tests gate only)", "source_url": "fixture://history",
        "retrieved_at": (now - timedelta(seconds=30)).isoformat(),
        "teams": [{"id": tid, "name": tid} for tid in ("H", "A")],
        "players": [{"id": p["id"], "name": p["name"]} for p in players], "games": games}
    game = {"id": "tonight-fixture", "home": {"id": "H"}, "away": {"id": "A"},
            "tipoff": (now + timedelta(hours=2)).isoformat(), "phase": "pre", "completed": False}
    return game, dataset, players, now


def test_draft_families_are_nonempty_mixed_deterministic_and_unpriced():
    game, dataset, players, now = fixture()
    result = construct_game_drafts(game, dataset, players, as_of=now)
    assert result == construct_game_drafts(game, dataset, players, as_of=now)
    assert [f["leg_count"] for f in result["families"]] == [3, 5, 8]
    for family in result["families"]:
        assert family["status"] == "DRAFT_UNPRICED"
        assert len(family["legs"]) == family["leg_count"]
        assert len({leg["player_id"] for leg in family["legs"]}) == family["leg_count"]
        assert {leg["team_id"] for leg in family["legs"]} == {"H", "A"}
        assert max(Counter(leg["stat"] for leg in family["legs"]).values()) <= 2
        assert family["joint_probability"] is family["expected_return_per_unit"] is family["combined_odds"] is None
        assert family["actionable"] is False
    for candidate in result["candidates"]:
        assert candidate["threshold_kind"] == "ANALYTICAL_THRESHOLD_NOT_OFFERED"
        assert candidate["probability"] is None and candidate["offer"] is None
        assert candidate["descriptive"]["games"] == 5
        assert candidate["descriptive"]["hit_rate"] == 1
        assert len(candidate["recent5"]) == 5
        assert candidate["roster_source"]["source_url"] == "fixture://current-roster"
        assert candidate["historical_source"]["dataset_id"] == dataset["id"]


def test_highest_alternate_ladder_threshold_is_based_on_exact_sample_counts():
    game, dataset, players, now = fixture()
    points = [10, 15, 20, 20, 30]
    for row, value in zip(dataset["games"], points):
        row["boxes"][0]["pts"] = value
    result = construct_game_drafts(game, dataset, players, as_of=now)
    candidate = next(c for c in result["candidates"] if c["player_id"] == "H-0" and c["stat"] == "pts")
    assert candidate["line"] == 15
    assert candidate["descriptive"]["hits"] == 4
    assert candidate["descriptive"]["hit_rate"] == .8
    assert candidate["descriptive"]["interval_95"][0] < .8 < candidate["descriptive"]["interval_95"][1]


@pytest.mark.parametrize("availability", ["OUT", "DOUBTFUL", "QUESTIONABLE"])
def test_default_drafts_exclude_injury_concerns(availability):
    game, dataset, players, now = fixture()
    players[0]["availability"] = availability
    result = construct_game_drafts(game, dataset, players, as_of=now)
    assert not any(c["player_id"] == "H-0" for c in result["candidates"])
    assert any(e["player_id"] == "H-0" for e in result["excluded_players"])


def test_stale_or_future_current_rosters_are_excluded_not_assumed_active():
    game, dataset, players, now = fixture()
    players[0]["source"]["retrieved_at"] = (now - timedelta(days=1)).isoformat()
    players[1]["source"]["retrieved_at"] = (now + timedelta(seconds=1)).isoformat()
    del players[2]["source"]
    result = construct_game_drafts(game, dataset, players, as_of=now)
    assert not {"H-0", "H-1", "H-2"} & {c["player_id"] for c in result["candidates"]}
    assert any("lineups" in reason for reason in result["block_reasons"])


def test_future_outcomes_dnp_and_old_team_history_do_not_supply_three_game_minimum():
    game, dataset, players, now = fixture()
    # One real current-team appearance remains for H-0.
    dataset["games"][0]["known_at"] = (now + timedelta(seconds=1)).isoformat()
    dataset["games"][1]["completed_at"] = (now + timedelta(seconds=1)).isoformat()
    dataset["games"][2]["boxes"][0]["minutes"] = 0
    dataset["games"][3]["boxes"][0]["team_id"] = "former-team"
    result = construct_game_drafts(game, dataset, players, as_of=now)
    assert not any(c["player_id"] == "H-0" for c in result["candidates"])
    assert any(e["player_id"] == "H-0" and "three prior" in e["reason"] for e in result["excluded_players"])


def test_low_minutes_and_missing_current_roster_membership_are_excluded():
    game, dataset, players, now = fixture()
    for row in dataset["games"]:
        row["boxes"][0]["minutes"] = 17
    players = players[:-1]
    result = construct_game_drafts(game, dataset, players, as_of=now)
    assert not {"H-0", "A-5"} & {c["player_id"] for c in result["candidates"]}


@pytest.mark.parametrize("change", ["synthetic", "missing", "wrong_team", "live", "final"])
def test_missing_authentic_source_and_closed_pregame_are_blocked(change):
    game, dataset, players, now = fixture()
    if change == "synthetic":
        dataset["synthetic"] = True
    elif change == "missing":
        dataset = None
    elif change == "wrong_team":
        game["away"]["id"] = "foreign"
    elif change == "live":
        game["phase"] = "in"
    else:
        game["completed"] = True
    result = construct_game_drafts(game, dataset, players, as_of=now)
    assert result["candidates"] == []
    assert all(f["status"] == "BLOCKED" and not f["legs"] for f in result["families"])
    assert result["actionable"] is False


def test_too_few_players_and_one_team_only_do_not_create_fake_eight_leg_family():
    game, dataset, players, now = fixture()
    result = construct_game_drafts(game, dataset, players[:7], as_of=now)
    assert result["families"][-1]["status"] == "BLOCKED"
    assert result["families"][0]["status"] == "DRAFT_UNPRICED"
    one_team = construct_game_drafts(game, dataset, players[:6], as_of=now)
    assert all(f["status"] == "BLOCKED" for f in one_team["families"])


def offered(game, now):
    return {"player_id": "H-0", "team_id": "H", "stat": "pts", "side": "over", "line": 19.5,
            "provider": "The Odds API", "market_key": "player_points", "alternate": False, "line_type": "standard", "line_type_source": "PROVIDER_MARKET_KEY",
            "decimal_odds": 1.5, "sportsbook": "fanduel", "home_id": "H", "away_id": "A",
            "tipoff": game["tipoff"], "observed_at": (now - timedelta(seconds=30)).isoformat(),
            "retrieved_at": (now - timedelta(seconds=20)).isoformat(),
            "source_reference": "Fictional manual price receipt", "source_sha256": "a" * 64}


@pytest.mark.parametrize("side,line", [("over", 19.5), ("under", 24.5)])
def test_exact_current_offered_market_is_source_bound_but_no_combined_price(side, line):
    game, dataset, players, now = fixture()
    quote = offered(game, now)
    quote.update(side=side, line=line)
    result = construct_game_drafts(game, dataset, players, [quote], as_of=now)
    candidate = next(c for c in result["candidates"] if c["player_id"] == "H-0" and c["stat"] == "pts")
    assert candidate["threshold_kind"] == "OFFERED_LINE"
    assert candidate["line"] == line and candidate["side"] == side
    assert candidate["offer"] == quote
    assert result["expected_return_per_unit"] is None
    assert all(f["combined_odds"] is None for f in result["families"])


@pytest.mark.parametrize("mutation", [{"home_id": "wrong"}, {"player_id": "wrong"}, {"team_id": "wrong"},
    {"line": 20}, {"decimal_odds": True}, {"source_sha256": None},
    {"observed_at": "2025-01-01T00:00:00Z"}, {"retrieved_at": "2099-01-01T00:00:00Z"}])
def test_invalid_offers_are_not_presented_as_book_lines(mutation):
    game, dataset, players, now = fixture()
    quote = {**offered(game, now), **mutation}
    result = construct_game_drafts(game, dataset, players, [quote], as_of=now)
    candidate = next(c for c in result["candidates"] if c["player_id"] == "H-0" and c["stat"] == "pts")
    assert candidate["threshold_kind"] == "ANALYTICAL_THRESHOLD_NOT_OFFERED"
    assert candidate["offer"] is None


def test_matching_event_id_does_not_override_conflicting_teams_or_tipoff():
    game, dataset, players, now = fixture()
    quote = {**offered(game, now), "event_id": game["id"], "away_id": "wrong"}
    result = construct_game_drafts(game, dataset, players, [quote], as_of=now)
    assert not any(c["offer"] for c in result["candidates"])


def test_future_retrieved_history_is_not_accepted_at_earlier_cutoff():
    game, dataset, players, now = fixture()
    dataset["retrieved_at"] = (now + timedelta(seconds=1)).isoformat()
    result = construct_game_drafts(game, dataset, players, as_of=now)
    assert result["candidates"] == []
    assert all(f["status"] == "BLOCKED" for f in result["families"])


def test_duplicate_roster_or_conflicting_game_revision_fails_closed():
    game, dataset, players, now = fixture()
    with pytest.raises(ValueError, match="duplicate current-roster"):
        construct_game_drafts(game, dataset, players + [players[0]], as_of=now)
    revision = deepcopy(dataset["games"][0])
    revision["boxes"][0]["pts"] += 1
    dataset["games"].append(revision)
    with pytest.raises(ValueError, match="Conflicting historical"):
        construct_game_drafts(game, dataset, players, as_of=now)


def test_no_input_mutation_and_future_evaluation_rejected():
    game, dataset, players, now = fixture()
    original = deepcopy((game, dataset, players))
    construct_game_drafts(game, dataset, players, as_of=now)
    assert (game, dataset, players) == original
    with pytest.raises(ValueError, match="future"):
        construct_game_drafts(game, dataset, players, as_of=now + timedelta(days=1))


def selection_candidate(player, team, stat, hits, games):
    return {"player_id": player, "team_id": team, "stat": stat,
            "descriptive": {"hits": hits, "games": games, "hit_rate": hits / games},
            "minutes": {"mean": 30}, "side": "at_least", "line": 10}


def test_complete_selection_recovers_valid_family_after_greedy_dead_end():
    candidates = [selection_candidate("H-flex", "H", "pts", 10, 10),
                  selection_candidate("H-flex", "H", "reb", 8, 10),
                  selection_candidate("H-pts", "H", "pts", 9, 10),
                  selection_candidate("A-pts", "A", "pts", 9, 10)]
    selected = _select(candidates, 3, ("H", "A"))
    assert len(selected) == 3
    assert {(c["player_id"], c["stat"]) for c in selected} == {
        ("H-flex", "reb"), ("H-pts", "pts"), ("A-pts", "pts")}
    assert max(Counter(c["stat"] for c in selected).values()) <= 2


def test_equal_balance_priority_prefers_nine_of_ten_over_three_of_three():
    candidates = [selection_candidate("H-tiny", "H", "pts", 3, 3),
                  selection_candidate("H-supported", "H", "pts", 9, 10),
                  selection_candidate("A-other", "A", "reb", 9, 10)]
    selected = _select(candidates, 2, ("H", "A"))
    assert {c["player_id"] for c in selected} == {"H-supported", "A-other"}


def test_complete_selection_is_stable_under_candidate_input_order():
    candidates = [selection_candidate("H-flex", "H", "pts", 10, 10),
                  selection_candidate("H-flex", "H", "reb", 8, 10),
                  selection_candidate("H-pts", "H", "pts", 9, 10),
                  selection_candidate("A-pts", "A", "pts", 9, 10)]
    assert _select(candidates, 3, ("H", "A")) == _select(list(reversed(candidates)), 3, ("H", "A"))


def test_complete_selection_agrees_with_exhaustive_small_constraint_graphs():
    rng = random.Random(73)
    for _ in range(40):
        candidates = [selection_candidate(f"{team}-{i}", team, stat, 9, 10)
                      for team in ("H", "A") for i in range(3) for stat in ("pts", "reb", "ast")
                      if rng.random() < .45]
        size = rng.choice((3, 4, 5))
        feasible = any(len({c["player_id"] for c in subset}) == size
                       and {c["team_id"] for c in subset} == {"H", "A"}
                       and max(Counter(c["stat"] for c in subset).values()) <= 2
                       for subset in combinations(candidates, size))
        selected = _select(candidates, size, ("H", "A"))
        assert bool(selected) == feasible
        if selected:
            assert len(selected) == len({c["player_id"] for c in selected}) == size
