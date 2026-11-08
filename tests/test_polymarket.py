import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nba_quant.polymarket import CLOB, GAMMA, PublicDataError, _market_rows, bind_event, fetch_public_json, load_polymarket_odds, odds_from_price, parse_buy_book, refresh_polymarket_odds

NOW = datetime(2026, 11, 5, 13, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("nba_quant.polymarket._now", lambda: NOW)


def book():
    return {"asset_id": "111", "market": "0xabc", "timestamp": str(int(NOW.timestamp() * 1000)),
            "hash": "providerbookhash", "min_order_size": "5",
            "asks": [{"price": ".9", "size": "30"}, {"price": ".6", "size": "10"}],
            "bids": [{"price": ".2", "size": "100"}, {"price": ".5", "size": "10"}],
            "last_trade_price": ".4"}


def source(payload, tmp_path, url):
    raw = json.dumps(payload).encode()
    sha = hashlib.sha256(raw).hexdigest()
    path = tmp_path / f"{sha}.json"
    path.write_bytes(raw)
    return {"provider": "Polymarket", "source_url": url, "retrieved_at": NOW.isoformat(),
            "http_status": 200, "sha256": sha, "content_sha256": __import__('nba_quant.store', fromlist=['digest']).digest(payload),
            "raw_path": str(path.resolve())}


def event():
    return {"id": "50", "slug": "nba-orl-phx-2026-11-05", "title": "Magic vs. Suns",
            "active": True, "closed": False, "startTime": "2026-11-06T02:00:00Z",
            "teams": [{"abbreviation": "orl", "ordering": "away"}, {"abbreviation": "phx", "ordering": "home"}],
            "markets": [{"id": "90", "question": "Franz Wagner: Points O/U 22.5", "sportsMarketType": "points",
                "line": 22.5, "conditionId": "0xabc", "outcomes": '["Yes","No"]', "outcomePrices": '[".4",".6"]',
                "clobTokenIds": '["111","222"]', "positionIds": ["333", "444"], "version": "v1",
                "description": 'This market will resolve to "Yes" if Franz Wagner scores more than 22.5 points. If inactive or does not take the court at any point, resolves No.',
                "active": True, "closed": False, "acceptingOrders": True,
                "feesEnabled": True, "feeSchedule": {"rate": .05}, "restricted": True}]}


def slate():
    return {"date": "2026-11-05", "games": [{"id": "espn123", "tipoff": "2026-11-06T02:00:00Z",
            "home": {"id": "21", "abbreviation": "PHX"}, "away": {"id": "19", "abbreviation": "ORL"}}]}


@pytest.mark.parametrize("price,decimal,american", [(.25, 4, 300), (.5, 2, 100), (.8, 1.25, -400)])
def test_american_decimal_equivalents_are_cost_based(price, decimal, american):
    assert odds_from_price(price) == {"decimal_odds": decimal, "american_odds": american}


@pytest.mark.parametrize("bad", [0, 1, -.1, float('nan'), float('inf'), True])
def test_non_prices_cannot_be_converted(bad):
    with pytest.raises(ValueError):
        odds_from_price(bad)


def test_buy_book_uses_lowest_ask_not_first_last_trade_bid_or_midpoint(tmp_path):
    b = book()
    result = parse_buy_book(b, "111", source(b, tmp_path, CLOB + '/book?token_id=111'), "0xabc")
    assert result["best_ask"] == .6
    assert result["decimal_odds"] == pytest.approx(1 / .6)
    assert result["american_odds"] == -150
    assert result["best_ask_shares"] == 10
    assert result["depth_quote"]["gross_cost"] == 3
    assert result["quote_status"] == "ASK_DEPTH_AVAILABLE"
    assert result["best_ask_has_requested_depth"]
    assert result["tradeable"] is False


def test_small_best_level_does_not_claim_minimum_size_at_best_price(tmp_path):
    b = book()
    b['asks'] = [{"price": ".6", "size": "2"}, {"price": ".7", "size": "5"}]
    result = parse_buy_book(b, "111", source(b, tmp_path, CLOB + '/book?token_id=111'), "0xabc")
    assert result["depth_available"]
    assert not result["best_ask_has_requested_depth"]
    assert result["depth_quote"]["gross_cost"] == pytest.approx(3.3)
    assert result["depth_quote"]["volume_weighted_price"] == pytest.approx(.66)


def test_missing_ask_liquidity_never_uses_bid_or_last_trade(tmp_path):
    b = book(); b['asks'] = []
    result = parse_buy_book(b, "111", source(b, tmp_path, CLOB + '/book?token_id=111'))
    assert result["quote_status"] == "NO_ASK_LIQUIDITY"
    assert result["best_ask"] is result["american_odds"] is result["decimal_odds"] is None


def test_stale_quote_cannot_be_freshened_by_retrieval(tmp_path):
    b = book(); b['timestamp'] = str(int((NOW - timedelta(minutes=6)).timestamp() * 1000))
    result = parse_buy_book(b, "111", source(b, tmp_path, CLOB + '/book?token_id=111'))
    assert result["quote_status"] == "STALE"
    assert result["decimal_odds"] is None


@pytest.mark.parametrize("mutation,error", [
    (lambda b: b.update(asset_id="999"), "asset identity"),
    (lambda b: b.update(market="other"), "condition/market identity"),
    (lambda b: b.update(timestamp=str(int((NOW + timedelta(minutes=1)).timestamp() * 1000))), "future"),
    (lambda b: b.update(timestamp=None), "timestamp"),
    (lambda b: b['asks'][0].update(price="nan"), "Nonfinite"),
    (lambda b: b['bids'][0].update(price=".8"), "Crossed"),
])
def test_invalid_book_snapshots_fail_closed(tmp_path, mutation, error):
    b = book(); mutation(b)
    with pytest.raises(ValueError, match=error):
        parse_buy_book(b, "111", source(b, tmp_path, CLOB + '/book?token_id=111'), "0xabc")


def test_exact_binding_does_not_equate_provider_ids_or_reverse_teams():
    result = bind_event(event(), slate())
    assert result['game_id'] == 'espn123'
    assert result['binding_status'] == 'EXACT_TEAM_TIME'
    shifted = event(); shifted['startTime'] = '2026-11-06T02:10:00Z'
    assert bind_event(shifted, slate())['game_id'] is None
    reversed_event = event()
    for team in reversed_event['teams']:
        team['ordering'] = 'away' if team['ordering'] == 'home' else 'home'
    assert bind_event(reversed_event, slate())['game_id'] is None


def test_protocol_identifier_selection_and_indicative_price_separation(tmp_path):
    e = event(); s = source(e, tmp_path, GAMMA + '/events')
    rows = _market_rows(e, e['markets'][0], bind_event(e, slate()), s)
    assert rows[0]['asset_id'] == '111' and rows[0]['asset_identifier_type'] == 'CTF_TOKEN_ID'
    assert rows[0]['american_odds'] is None and rows[0]['decimal_odds'] is None
    assert rows[0]['indicative_american_odds'] == 150
    assert rows[0]['indicative_decimal_odds'] == 2.5
    assert rows[0]['dnp_rule'] == 'YES_RESOLVES_NO_IF_PLAYER_DOES_NOT_APPEAR'
    assert not rows[0]['eligible_for_constructor']
    e['markets'][0]['version'] = 'v2'
    rows = _market_rows(e, e['markets'][0], bind_event(e, slate()), s)
    assert rows[0]['asset_id'] == '333' and rows[0]['asset_identifier_type'] == 'POSITION_ID'


def mocked_fetch(tmp_path, calls, blocked=False):
    def fetch(url, directory):
        calls.append(url)
        if url.endswith('/sports'):
            payload = [{'sport': 'nba', 'series': '10345'}]
        elif '/events?' in url:
            payload = [event()]
        else:
            if blocked:
                payload = {'error': 'Forbidden'}
                receipt = source(payload, tmp_path, url)
                receipt['http_status'] = 403
                raise PublicDataError('HTTP Error 403: Forbidden', receipt, 403)
            payload = book(); payload['asset_id'] = url.split('token_id=')[1]
        return payload, source(payload, tmp_path, url)
    return fetch


def test_public_acquisition_produces_depth_backed_quotes_without_trades(tmp_path):
    calls = []
    result = refresh_polymarket_odds(tmp_path, slate(), fetcher=mocked_fetch(tmp_path, calls))
    assert result['status'] == 'AVAILABLE'
    assert result['coverage']['depth_backed_outcomes'] == 2
    assert len(calls) == 4
    assert result['execution'] == 'NONE' and result['joint_sgp_quote'] is None
    assert all(r['executable'] and r['quote_basis'] == 'ORDERBOOK_BEST_ASK' for r in result['markets'])
    assert all(not r['tradeable'] and not r['actionable'] for r in result['markets'])
    loaded = load_polymarket_odds(tmp_path)
    assert loaded['hash'] == result['hash']
    assert loaded['markets'][0]['best_ask'] == .6


def test_access_rejection_stops_further_book_requests_and_never_substitutes_gamma_prices(tmp_path):
    calls = []
    result = refresh_polymarket_odds(tmp_path, slate(), fetcher=mocked_fetch(tmp_path, calls, blocked=True))
    assert result['status'] == 'QUOTE_ACCESS_UNAVAILABLE'
    assert len(calls) == 3
    assert result['coverage']['orderbooks_attempted'] == 1
    assert result['coverage']['depth_backed_outcomes'] == 0
    assert all(r['american_odds'] is None and not r['executable'] for r in result['markets'])
    assert all(r['indicative_american_odds'] is not None for r in result['markets'])
    assert result['sources'][-1]['http_status'] == 403


def test_expired_persisted_snapshot_removes_executable_prices(tmp_path, monkeypatch):
    refresh_polymarket_odds(tmp_path, slate(), fetcher=mocked_fetch(tmp_path, []))
    monkeypatch.setattr('nba_quant.polymarket._now', lambda: NOW + timedelta(minutes=6))
    loaded = load_polymarket_odds(tmp_path)
    assert loaded['status'] == 'LAST_GOOD' and loaded['stale']
    assert all(not r['executable'] and r['american_odds'] is None for r in loaded['markets'])


def test_each_book_expires_even_when_snapshot_publication_is_recent(tmp_path, monkeypatch):
    from nba_quant.store import canonical, digest
    result = refresh_polymarket_odds(tmp_path, slate(), fetcher=mocked_fetch(tmp_path, []))
    receipt = json.loads((tmp_path / 'latest.json').read_text())
    saved = json.loads(Path(receipt['path']).read_text())
    saved['retrieved_at'] = (NOW + timedelta(minutes=2)).isoformat()
    key = digest(saved)
    path = tmp_path / f'snapshot-{key}.json'
    path.write_text(canonical(saved))
    (tmp_path / 'latest.json').write_text(canonical({'hash': key, 'path': str(path)}))
    monkeypatch.setattr('nba_quant.polymarket._now', lambda: NOW + timedelta(minutes=6))
    loaded = load_polymarket_odds(tmp_path)
    assert not loaded['stale']  # Publication age is only four minutes.
    assert all(r['quote_age_seconds'] == 360 and not r['executable'] and r['american_odds'] is None for r in loaded['markets'])


def test_corrupt_source_does_not_survive_as_verified_metadata_or_quotes(tmp_path):
    result = refresh_polymarket_odds(tmp_path, slate(), fetcher=mocked_fetch(tmp_path, []))
    Path(result['sources'][0]['raw_path']).write_text('{}')
    loaded = load_polymarket_odds(tmp_path)
    assert loaded['status'] == 'UNAVAILABLE' and loaded['markets'] == []


def test_allowlist_and_empty_cache_have_no_auth_or_fixture_fallback(tmp_path):
    with pytest.raises(ValueError, match='Only documented public'):
        fetch_public_json('https://clob.polymarket.com/auth/api-key', tmp_path)
    assert load_polymarket_odds(tmp_path)['status'] == 'UNAVAILABLE'
