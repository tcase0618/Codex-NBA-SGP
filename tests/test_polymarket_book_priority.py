from copy import deepcopy

from nba_quant.polymarket import _book_priority


def rows(event, player, markets):
    return [{"event_id": event, "market_type": "points", "player_name": player,
             "market_id": f"{event}-{player}-{i}", "line": 10+i*5,
             "indicative_price": p if side == 0 else 1-p, "outcome_index": side}
            for i, p in enumerate(markets) for side in (0, 1)]


def test_budget_visits_each_game_before_consuming_first_games_ladder():
    original = rows("1", "A", [.8, .5, .3, .1]) + rows("2", "B", [.6, .4])
    before = deepcopy(original); ordered = list(_book_priority(original))
    assert [r["event_id"] for r in ordered[:4]] == ["1", "1", "2", "2"]
    assert len(ordered) == len(original)
    assert original == before


def test_player_groups_and_central_support_upside_books_are_prioritized():
    original = rows("1", "A", [.1, .3, .5, .8]) + rows("1", "B", [.7, .4])
    ordered = list(_book_priority(original))
    assert [r["player_name"] for r in ordered[:4]] == ["A", "A", "B", "B"]
    assert [r["indicative_price"] for r in ordered if r["player_name"] == "A" and r["outcome_index"] == 0] == [.5, .8, .3, .1]
    assert [(r["market_id"], r["outcome_index"]) for r in ordered] == [(r["market_id"], r["outcome_index"]) for r in _book_priority(list(reversed(original)))]
