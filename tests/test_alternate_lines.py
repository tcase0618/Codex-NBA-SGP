import json

import pytest

from nba_quant.construction import construct_game_drafts
from nba_quant.odds import OddsAPI
from test_construction import fixture, offered
from test_market_sources import payload


def test_real_30plus_milestone_can_replace_lower_line_when_sample_supports_it():
    game, dataset, players, now = fixture()
    for row, points in zip(dataset["games"], [25, 31, 32, 35, 40]):
        row["boxes"][0]["pts"] = points
    low = {**offered(game, now), "side": "at_least", "line": 15, "decimal_odds": 1.3}
    high = {**low, "line": 30, "decimal_odds": 3.5}
    r = construct_game_drafts(game, dataset, players, [low, high], now)
    c = next(c for c in r["candidates"] if c["player_id"] == "H-0" and c["stat"] == "pts")
    assert c["line"] == 30 and c["side"] == "at_least"
    assert c["threshold_kind"] == "OFFERED_LINE"
    assert c["offer"]["decimal_odds"] == 3.5
    assert c["descriptive"]["hits"] == 4 and c["descriptive"]["games"] == 5
    assert c["probability"] is None
    assert [row["line"] for row in c["offered_alt_ladder"]] == [15, 30]
    assert all(row["expected_value"] is None and row["forecast_probability"] is None for row in c["offered_alt_ladder"])


def test_mean_of_30_does_not_automatically_select_30plus():
    game, dataset, players, now = fixture()
    for row, points in zip(dataset["games"], [20, 20, 20, 40, 50]):
        row["boxes"][0]["pts"] = points
    low = {**offered(game, now), "side": "at_least", "line": 15, "decimal_odds": 1.3}
    high = {**low, "line": 30, "decimal_odds": 3.5}
    r = construct_game_drafts(game, dataset, players, [low, high], now)
    c = next(c for c in r["candidates"] if c["player_id"] == "H-0" and c["stat"] == "pts")
    assert c["line"] == 15
    high_result = next(x for x in c["offered_alt_ladder"] if x["line"] == 30)
    assert high_result["historical_hits"] == 2 and not high_result["passes_descriptive_screen"]


def test_alternate_feed_market_keeps_exact_book_threshold_and_price(tmp_path):
    p = payload()
    p["bookmakers"][0]["markets"][0]["key"] = "player_points_alternate"
    p["bookmakers"][0]["markets"][0]["outcomes"][0].update(point=29.5, price=3.2)
    urls = []
    def transport(url):
        urls.append(url)
        return json.dumps(p).encode()
    r = OddsAPI(api_key="non-live-key-sentinel", output_dir=tmp_path, transport=transport).props("event1")
    assert "player_points_alternate" in urls[0]
    line = r["lines"][0]
    assert line["alternate"] and line["market_key"] == "player_points_alternate"
    assert line["stat"] == "pts" and line["side"] == "over" and line["line"] == 29.5
    assert line["decimal_odds"] == 3.2


def test_fractional_at_least_quote_is_not_silently_rounded():
    game, dataset, players, now = fixture()
    q = {**offered(game, now), "side": "at_least", "line": 20.5}
    r = construct_game_drafts(game, dataset, players, [q], now)
    c = next(c for c in r["candidates"] if c["player_id"] == "H-0" and c["stat"] == "pts")
    assert c["offer"] is None and c["threshold_kind"] == "ANALYTICAL_THRESHOLD_NOT_OFFERED"
