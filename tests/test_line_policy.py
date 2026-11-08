from copy import deepcopy

import pytest

from nba_quant.line_policy import line_policy_reason
from nba_quant.aggressive import evaluate_aggressive_ladder
from nba_quant.construction import _offered_alternative, construct_game_drafts
from nba_quant.family_optimizer import select_family
from test_construction import fixture, offered
from test_family_optimizer import candidate


def standard():
    return {"provider": "The Odds API", "side": "under", "line": 24.5, "decimal_odds": 3., "market_key": "player_points",
            "alternate": False, "line_type": "standard", "line_type_source": "PROVIDER_MARKET_KEY"}


def test_regular_provider_standard_under_is_allowed():
    assert line_policy_reason("under", standard()) is None
    result = evaluate_aggressive_ladder([10, 15, 20, 21, 30], [("under", 24.5, standard())])
    assert result["eligible"]


@pytest.mark.parametrize("mutation", [
    {"alternate": True}, {"market_key": "player_points_alternate"}, {"line_type": "unknown"},
    {"line_type_source": None}, {"line_type_source": "CLOSEST_TO_EVEN_ODDS"}, {"market_key": "points"},
    {"alternate": None}, {"line_type": "alternate"}, {"provider": "Polymarket"},
])
def test_under_unknown_or_alternate_is_never_evidence_eligible(mutation):
    quote = {**standard(), **mutation}
    assert line_policy_reason("under", quote)
    row = evaluate_aggressive_ladder([10, 15, 20, 21, 30], [("under", 24.5, quote)])["ladder"][0]
    assert row["eligible"] is row["evidence_eligible"] is False


def test_even_odds_do_not_identify_the_standard_line():
    assert line_policy_reason("under", {"side": "under", "line": 24.5, "decimal_odds": 2.})
    assert line_policy_reason("over", {"alternate": True}) is None
    assert line_policy_reason("at_least", {}) is None


def test_constructor_excludes_alternate_under_but_retains_alternate_over():
    game, dataset, players, now = fixture()
    bad = {**offered(game, now), "side": "under", "line": 24.5, "market_key": "player_points_alternate", "alternate": True, "line_type": "alternate"}
    good = {**bad, "side": "over", "line": 19.5, "decimal_odds": 3.}
    original = deepcopy((bad, good))
    result = construct_game_drafts(game, dataset, players, [bad, good], now, "aggressive")
    assert result["candidates"]
    assert all(p["side"] != "under" for p in result["candidates"])
    assert all(p["side"] != "under" for f in result["families"] for p in f["legs"])
    assert (bad, good) == original


def test_coverage_and_delivery_cannot_resurrect_a_cached_under_alt():
    row = {"side": "under", "line": 24.5, "offer": {"side": "under"}, "evidence_eligible": True}
    with pytest.raises(ValueError, match="Under"):
        _offered_alternative({}, row)
    p = candidate("x", "H", "pts");p.update(side="under", line=24.5)
    with pytest.raises(ValueError, match="Under"):
        select_family([p], 5, ("H", "A"))
