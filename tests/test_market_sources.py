import json
from datetime import datetime, timedelta, timezone

import pytest

from nba_quant.injuries import latest_report, refresh_report
from nba_quant.odds import OddsAPI


def test_credentials_required_no_request(tmp_path):
    with pytest.raises(ValueError, match="not configured"):
        OddsAPI(api_key="", output_dir=tmp_path).events()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("raw", [b'broken SECRET-SENTINEL', b'{"echo":"SECRET-SENTINEL"}'])
def test_credential_echo_never_archived(tmp_path, raw):
    with pytest.raises(ValueError) as exc:
        OddsAPI(api_key="SECRET-SENTINEL", output_dir=tmp_path, transport=lambda _: raw).events()
    assert "SECRET-SENTINEL" not in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_transport_exception_does_not_leak_url(tmp_path):
    def failing(url):
        raise RuntimeError(url)
    with pytest.raises(ValueError) as exc:
        OddsAPI(api_key="SECRET-SENTINEL", output_dir=tmp_path, transport=failing).events()
    assert "SECRET-SENTINEL" not in str(exc.value)


def payload():
    return {"id": "event1", "sport_key": "basketball_nba", "commence_time": "2026-11-05T00:00:00Z",
        "home_team": "H", "away_team": "A", "bookmakers": [{"key": "fanduel", "markets": [{
            "key": "player_points", "last_update": "2026-01-01T00:00:00Z", "outcomes": [
                {"name": "Over", "description": "Real Player", "price": 1.91, "point": 20.5}]}]}]}


def test_props_preserve_source_unmapped_identity_no_sgp_price(tmp_path):
    client = OddsAPI(api_key="SECRET-SENTINEL", output_dir=tmp_path,
                     transport=lambda _: json.dumps(payload()).encode())
    result = client.props("event1", bookmakers="fanduel")
    assert result["combined_sgp_quote"] is None
    assert result["lines"][0]["player_id"] is None
    assert result["lines"][0]["identity_status"] == "UNMAPPED"
    assert result["lines"][0]["decimal_odds"] == 1.91
    for path in tmp_path.iterdir():
        assert b"SECRET-SENTINEL" not in path.read_bytes()


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf")])
def test_invalid_numeric_line_rejected(tmp_path, bad):
    p = payload()
    p["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = bad
    with pytest.raises(ValueError, match="Malformed"):
        OddsAPI(api_key="key-sentinel", output_dir=tmp_path, transport=lambda _: json.dumps(p).encode()).props("event1")


def test_invalid_provider_json_is_safe(tmp_path):
    with pytest.raises(ValueError, match="malformed JSON"):
        OddsAPI(api_key="key-sentinel", output_dir=tmp_path, transport=lambda _: b'{oops').events()


def test_official_report_hashed_and_not_eligibility(tmp_path):
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=1)
    from zoneinfo import ZoneInfo
    local = past.astimezone(ZoneInfo("America/New_York"))
    url = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_" + local.strftime("%Y-%m-%d_%I_%M%p") + ".pdf"
    r = refresh_report(tmp_path, source_url=url, fetch=lambda _: b"%PDF-1.4 official test fixture")
    assert r["eligible_for_automatic_roster_gate"] is False
    assert r["selection"] == "explicit_official_url"
    assert latest_report(tmp_path)["sha256"] == r["sha256"]
    from pathlib import Path
    Path(r["raw_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        latest_report(tmp_path)


def test_future_retrieval_and_nonofficial_url_rejected(tmp_path):
    with pytest.raises(ValueError, match="future"):
        refresh_report(tmp_path, now=datetime.now(timezone.utc) + timedelta(days=1))
    with pytest.raises(ValueError, match="official"):
        refresh_report(tmp_path, source_url="https://example.com/fake.pdf")
