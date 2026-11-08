from copy import deepcopy

import pytest

from nba_quant.construction import construct_game_drafts, _select
from test_construction import fixture, offered, selection_candidate


def test_aggressive_selects_higher_priced_threshold_without_inventing_forecast():
    game, dataset, players, now = fixture()
    quotes = [{**offered(game, now), "line": 19.5, "decimal_odds": 1.2},
              {**offered(game, now), "line": 22.5, "decimal_odds": 5.0}]
    standard = construct_game_drafts(game, dataset, players, quotes, now)
    aggressive = construct_game_drafts(game, dataset, players, quotes, now, "aggressive")
    old = next(c for c in standard["candidates"] if c["player_id"] == "H-0" and c["stat"] == "pts")
    new = next(c for c in aggressive["candidates"] if c["player_id"] == "H-0" and c["stat"] == "pts")
    assert old["line"] == 19.5 and new["line"] == 22.5
    assert new["aggressive_evidence"]["proxy_return_gross"] > 0
    assert new["probability"] is None and new["actionable"] is False
    assert aggressive["selection_mode"] == "aggressive"
    assert len(new["offered_alt_ladder"]) == 2
    assert new["aggressive_evidence"]["ranking_score"] > 0
    assert all(f["joint_probability"] is None and f["combined_odds"] is None for f in aggressive["families"])


def test_aggressive_no_price_or_metadata_only_quote_blocks_instead_of_fallback():
    game, dataset, players, now = fixture()
    for quotes in ([], [{**offered(game, now), "sportsbook": "Polymarket", "executable": False,
                         "quote_basis": "INDICATIVE_MARKET_METADATA_NOT_EXECUTABLE"}]):
        result = construct_game_drafts(game, dataset, players, quotes, now, "aggressive")
        assert result["candidates"] == []
        assert all(f["status"] == "BLOCKED" and f["selection_mode"] == "aggressive" for f in result["families"])


def test_aggressive_reverse_order_and_historical_inputs_are_stable():
    game, dataset, players, now = fixture()
    quotes = [{**offered(game, now), "line": x, "decimal_odds": price} for x, price in [(19.5, 1.2), (22.5, 3.0)]]
    before = deepcopy((dataset, players, quotes))
    a = construct_game_drafts(game, dataset, players, quotes, now, "aggressive")
    b = construct_game_drafts(game, dataset, players, list(reversed(quotes)), now, "aggressive")
    assert [(c["player_id"], c["stat"], c["line"]) for c in a["candidates"]] == [(c["player_id"], c["stat"], c["line"]) for c in b["candidates"]]
    assert (dataset, players, quotes) == before


def test_unknown_mode_rejected():
    game, dataset, players, now = fixture()
    with pytest.raises(ValueError, match="mode"):
        construct_game_drafts(game, dataset, players, as_of=now, selection_mode="force-high-lines")


def test_aggressive_family_ranks_price_proxy_ahead_of_easy_floor_frequency():
    candidates = [selection_candidate("H-floor", "H", "pts", 10, 10),
                  selection_candidate("H-upside", "H", "pts", 5, 10),
                  selection_candidate("A-reb", "A", "reb", 8, 10),
                  selection_candidate("A-ast", "A", "ast", 8, 10)]
    for c in candidates:
        c.update(selection_mode="aggressive", aggressive_evidence={"proxy_return_gross": .1})
    candidates[1]["aggressive_evidence"]["proxy_return_gross"] = .5
    selected = _select(candidates, 3, ("H", "A"))
    assert selected[0]["player_id"] == "H-upside"
    assert len(selected) == 3


def test_best_supported_upside_line_beats_a_higher_badly_priced_threshold():
    game, dataset, players, now = fixture()
    for g, value in zip(dataset["games"], [20, 25, 25, 30, 30]):
        g["boxes"][0]["pts"] = value
    quotes = [{**offered(game, now), "line": 24.5, "decimal_odds": 4.0},
              {**offered(game, now), "line": 29.5, "decimal_odds": 3.0}]
    result = construct_game_drafts(game, dataset, players, quotes, now, "aggressive")
    candidate = next(c for c in result["candidates"] if c["player_id"] == "H-0")
    assert candidate["line"] == 24.5
    scores = {r["line"]: r["ranking_score"] for r in candidate["offered_alt_ladder"]}
    assert scores[24.5] > scores[29.5]
    assert candidate["family_eligible"] is True


def test_mean_positive_but_uncertainty_negative_candidate_cannot_fill_a_family():
    game, dataset, players, now = fixture()
    quotes = [{**offered(game, now), "line": 22.5, "decimal_odds": 3.0}]
    result = construct_game_drafts(game, dataset, players, quotes, now, "aggressive")
    candidate = next(c for c in result["candidates"] if c["player_id"] == "H-0")
    assert candidate["aggressive_evidence"]["proxy_return_gross"] > 0
    assert candidate["aggressive_evidence"]["ranking_score"] < 0
    assert candidate["family_eligible"] is False
    assert all(f["status"] == "BLOCKED" for f in result["families"])


def test_family_selection_uses_new_uncertainty_score_not_legacy_gross_score():
    candidates = [selection_candidate("H-legacy", "H", "pts", 7, 10),
                  selection_candidate("H-new", "H", "pts", 7, 10),
                  selection_candidate("A-reb", "A", "reb", 8, 10),
                  selection_candidate("A-ast", "A", "ast", 8, 10)]
    for c in candidates:
        c.update(selection_mode="aggressive", aggressive_evidence={"proxy_return_gross": .1, "ranking_score": .1})
    candidates[0]["aggressive_evidence"].update(proxy_return_gross=5., ranking_score=.05)
    candidates[1]["aggressive_evidence"].update(proxy_return_gross=.2, ranking_score=.15)
    selected = _select(candidates, 3, ("H", "A"))
    assert selected[0]["player_id"] == "H-new"
