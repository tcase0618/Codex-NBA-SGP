from datetime import timedelta
from copy import deepcopy

from nba_quant.polymarket_binding import bind_polymarket_offers
from test_construction import fixture, offered


def setup():
    game, _, players, now = fixture()
    row = {**offered(game, now), "game_id": game["id"], "player_name": players[0]["name"],
           "quote_basis": "ORDERBOOK_BEST_ASK", "executable": True,
           "quote_source_sha256": "b" * 64, "quote_retrieved_at": now.isoformat()}
    return game, players, now, row


def test_exact_roster_bound_quote_uses_book_not_metadata_receipt():
    game, players, now, row = setup()
    report = {"offers": [row]}
    before = deepcopy(report)
    mapped = bind_polymarket_offers(report, game, players, now)
    assert len(mapped) == 1
    assert mapped[0]["sportsbook"] == "polymarket"
    assert mapped[0]["source_sha256"] == "b" * 64
    assert mapped[0]["retrieved_at"] == now.isoformat()
    assert report == before


def test_stale_wrong_event_ambiguous_and_indicative_never_bind():
    game, players, now, row = setup()
    for mutation in ({"game_id": "wrong"}, {"home_id": "wrong"}, {"player_id": "wrong"},
                     {"observed_at": (now-timedelta(minutes=6)).isoformat()},
                     {"observed_at": (now+timedelta(seconds=1)).isoformat()},
                     {"executable": False}, {"quote_basis": "MIDPOINT"}, {"line": 20}):
        assert not bind_polymarket_offers({"offers": [{**row, **mutation}]}, game, players, now)
    assert not bind_polymarket_offers({"stale": True, "offers": [row]}, game, players, now)
    assert not bind_polymarket_offers({"offers": [row]}, game, players+[players[0]], now)
