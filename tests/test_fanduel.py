import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nba_quant.fanduel import INDEX_URL, SPORTSBOOK_URL, fetch_html, load_fanduel_odds, parse_research_article, refresh_fanduel_odds

URL = "https://www.fanduel.com/research/nba-props-today-fixture"


def article(market_name="3+ Threes", runner="Player Name", american="+116", handicap=0, formatted=None):
    published = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    article = {"sport": {"slug": "nba"}, "title": "NBA Player Props",
        "firstPublishedAt": published, "publishedDate": published,
        "body": json.dumps([{"_type": "block", "listItem": "bullet",
            "children": [{"text": "Player Name 3+ Made Threes (+122)"}]},
            {"_type": "dynamicOdds", "_key": "primary-block", "event": {"id": 123, "name": "Away @ Home"},
            "oddsLeague": {"name": "NBA"}, "odds": {"event": {
                "time": "2026-11-05T00:10:00Z", "homeTeam": {"name": "Home"},
                "awayTeam": {"name": "Away"}, "eventUrl": "https://sportsbook.fanduel.com/basketball/nba/away-home-123"},
                "props": [{"marketId": "754.123", "externalMarketId": "42.123", "marketName": market_name,
                    "marketStatus": "OPEN", "inPlay": False, "sgmMarket": True,
                    "selections": [{"selectionId": 987, "runnerName": runner, "runnerStatus": "ACTIVE",
                        "handicap": handicap, "formattedHandicap": formatted, "odds": american}]}]}}])}
    return article


def html_for(data):
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"article": data}}}) + '</script></html>'


def source(html, directory=None, url=URL):
    raw = html.encode()
    sha = hashlib.sha256(raw).hexdigest()
    path = Path(directory or ".") / (sha + ".html")
    if directory:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return {"provider": "FanDuel", "source_url": url, "http_status": 200,
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "sha256": sha, "raw_path": str(path.resolve())}


def test_structured_primary_quote_preserves_market_identity_and_unknown_price_clock():
    html = html_for(article())
    result = parse_research_article(html, source(html))
    offer = result["offers"][0]
    assert offer["player_name"] == "Player Name"
    assert offer["stat"] == "fg3m"
    assert offer["side"] == "at_least" and offer["line"] == 3
    assert offer["american_odds"] == 116
    assert offer["decimal_odds"] == 2.16
    assert offer["market_id"] == "754.123" and offer["external_market_id"] == "42.123"
    assert offer["selection_id"] == "987" and offer["provider_event_id"] == "123"
    assert offer["price_freshness"] == "UNKNOWN"
    assert offer["quote_updated_at"] is None
    assert not offer["eligible_as_current_quote"]
    assert offer["sgp_eligible"]
    # Editorial price differences are retained, not silently substituted.
    assert result["editorial_summaries"] == ["Player Name 3+ Made Threes (+122)"]
    assert offer["american_odds"] != 122


@pytest.mark.parametrize("market_name,runner,handicap,formatted,stat,side,line", [
    ("12+ Rebounds", "Player Name", 0, None, "reb", "at_least", 12),
    ("Player Name - Pts + Reb + Ast", "Player Name Over", 22.5, "O 22.5", "pra", "over", 22.5),
    ("Player Name - Points", "Player Name Under", 19.5, "U 19.5", "pts", "under", 19.5),
    ("Player Name - Assists", "Player Name Over", 5.5, "O 5.5", "ast", "over", 5.5),
])
def test_supported_player_market_comparators_are_exact(market_name, runner, handicap, formatted, stat, side, line):
    html = html_for(article(market_name, runner, "-122", handicap, formatted))
    offer = parse_research_article(html, source(html))["offers"][0]
    assert (offer["stat"], offer["side"], offer["line"], offer["player_name"]) == (stat, side, line, "Player Name")
    assert offer["decimal_odds"] == pytest.approx(1 + 100 / 122)


@pytest.mark.parametrize("american", ["0", "+99", "100", "nan", "--"])
def test_invalid_prices_are_rejected_instead_of_inferred(american):
    html = html_for(article(american=american))
    result = parse_research_article(html, source(html))
    assert result["offers"] == []
    assert "American odds" in result["errors"][0]["reason"]


def test_ambiguous_or_inconsistent_market_lines_are_rejected():
    html = html_for(article("Player Name - Points", "Player Name Over", "-110", 22.5, "U 22.5"))
    result = parse_research_article(html, source(html))
    assert result["offers"] == []
    assert result["errors"]


def test_non_nba_article_and_content_hash_mismatch_fail_closed():
    data = article()
    data["sport"]["slug"] = "nfl"
    html = html_for(data)
    with pytest.raises(ValueError, match="NBA research"):
        parse_research_article(html, source(html))
    metadata = source(html)
    metadata["sha256"] = "bad"
    with pytest.raises(ValueError, match="differs from source hash"):
        parse_research_article(html, metadata)


def test_future_publication_or_retrieval_does_not_establish_valid_prices():
    data = article()
    data["firstPublishedAt"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    html = html_for(data)
    with pytest.raises(ValueError, match="publication timestamp"):
        parse_research_article(html, source(html))
    metadata = source(html)
    metadata["retrieved_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="retrieval timestamp"):
        parse_research_article(html, metadata)


def mock_fetch(directory, observations):
    def fetch(url, out):
        observations.append(url)
        if url == SPORTSBOOK_URL:
            html = "<html>Public app shell</html>"
        elif url == INDEX_URL:
            record = {"name": "NBA Props Today", "url": URL,
                      "datePublished": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
            html = '<script type="application/ld+json">' + json.dumps({"items": [record]}) + '</script>'
        else:
            html = html_for(article())
        return html, source(html, directory, url)
    return fetch


def test_automatic_discovery_acquires_primary_market_components_and_verifies_archives(tmp_path):
    calls = []
    result = refresh_fanduel_odds(tmp_path, mock_fetch(tmp_path, calls))
    assert calls == [SPORTSBOOK_URL, INDEX_URL, URL]
    assert result["status"] == "PARTIAL_PUBLISHED"
    assert result["offers_count"] == 1
    assert result["verified_current_quotes"] == 0
    assert result["joint_sgp_quote"] is None
    assert result["price_freshness"] == "UNKNOWN"
    cached = load_fanduel_odds(tmp_path)
    assert cached["hash"] == result["hash"]
    assert cached["offers"] == result["offers"]
    Path(result["sources"][0]["raw_path"]).write_text("corrupt")
    rejected = load_fanduel_odds(tmp_path)
    assert rejected["status"] == "UNAVAILABLE"
    assert not rejected["offers"]
    assert "hash mismatch" in rejected["reason"]


def test_http_403_failure_is_reported_without_credential_or_bypass_attempt(tmp_path):
    def forbidden(*_args):
        raise OSError("HTTP Error 403: Forbidden")
    result = refresh_fanduel_odds(tmp_path, forbidden)
    assert result["status"] == "UNAVAILABLE"
    assert result["offers_count"] == 0
    assert "403" in result["attempts"][0]["result"]
    assert "403" in result["errors"][0]["reason"]


def test_arbitrary_third_party_source_is_never_labelled_primary_fanduel(tmp_path):
    html = html_for(article())
    metadata = source(html, tmp_path, "https://example.com/nba")
    with pytest.raises(ValueError, match="primary FanDuel"):
        parse_research_article(html, metadata)
    with pytest.raises(ValueError, match="primary public FanDuel"):
        fetch_html("https://example.com/nba", tmp_path)


def test_unacquired_prices_have_no_fixture_fallback(tmp_path):
    result = load_fanduel_odds(tmp_path)
    assert result["status"] == "UNAVAILABLE"
    assert not result["offers"]


def test_article_acquisition_has_fixed_small_request_bound(tmp_path):
    with pytest.raises(ValueError, match="At most three"):
        refresh_fanduel_odds(tmp_path, max_articles=4)
