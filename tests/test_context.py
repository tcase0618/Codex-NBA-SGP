import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nba_quant.context import load_context, parse_roster, refresh_context
from nba_quant.providers import BASE
from nba_quant.store import digest

NOW = datetime(2026, 11, 4, 23, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("nba_quant.context._now", lambda: NOW)


def roster(team_id="1", injuries=None):
    return {"status": "success", "timestamp": "2026-11-04T22:50:00Z",
            "season": {"year": 2027, "type": 2},
            "team": {"id": team_id, "displayName": f"Team {team_id}"},
            "athletes": [{"id": f"{team_id}1", "displayName": f"Athlete {team_id}1",
                          "position": {"abbreviation": "G"}, "status": {"name": "Active"},
                          "injuries": [] if injuries is None else injuries}]}


def observed(payload, directory, team_id="1", observed_at=NOW):
    raw = json.dumps(payload).encode()
    sha = hashlib.sha256(raw).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha}.json"
    path.write_bytes(raw)
    return {"provider": "ESPN", "url": f"{BASE}/teams/{team_id}/roster",
            "retrieved_at": observed_at.isoformat(), "sha256": sha,
            "content_sha256": digest(payload), "raw_path": str(path.resolve())}


def slate():
    return {"date": "2026-11-04", "status": "AVAILABLE", "stale": False,
            "source_sha256": "observed-slate", "games": [{"id": "100", "tipoff": "2026-11-05T01:00:00Z",
                "home": {"id": "1"}, "away": {"id": "2"}}]}


def provider_fetch(url, directory):
    tid = url.split("/")[-2]
    payload = roster(tid)
    return payload, observed(payload, directory, tid)


def test_active_roster_and_unlisted_injury_do_not_establish_game_availability(tmp_path):
    payload = roster()
    result = parse_roster(payload, observed(payload, tmp_path), "1")
    player = result["players"][0]
    assert player["roster_status"] == "Active"
    assert player["injury_status"] == "UNREPORTED"
    assert player["availability"] == player["game_active_status"] == "UNVERIFIED"
    assert player["position"] == "G"
    assert player["source_position"] == "G"
    assert "active_probability" not in player
    assert "expected_minutes" not in player


@pytest.mark.parametrize("status,expected", [
    ("Out", "OUT"), ("Questionable", "QUESTIONABLE"), ("Doubtful", "DOUBTFUL"),
    ("Available", "AVAILABLE"), ("Day-To-Day", "UNVERIFIED"), ("Probable", "UNVERIFIED"),
])
def test_availability_uses_only_explicit_supported_injury_status(tmp_path, status, expected):
    payload = roster(injuries=[{"status": status, "date": "2026-11-04T20:00:00Z"}])
    result = parse_roster(payload, observed(payload, tmp_path), "1")
    player = result["players"][0]
    assert player["injury_status"] == status
    assert player["availability"] == expected
    assert player["membership_observed_at"] == NOW.isoformat()
    assert player["injury_reported_at"] != player["membership_observed_at"]


def test_most_recent_dated_injury_supersedes_older_status_without_backdating_membership(tmp_path):
    payload = roster(injuries=[{"status": "Out", "date": "2026-11-03T20:00:00Z"},
                              {"status": "Available", "date": "2026-11-04T20:00:00Z"}])
    player = parse_roster(payload, observed(payload, tmp_path), "1")["players"][0]
    assert player["availability"] == "AVAILABLE"
    assert player["membership_observed_at"] == NOW.isoformat()
    assert len(player["injury_records"]) == 2


def test_conflicting_same_time_or_undated_statuses_remain_unverified(tmp_path):
    payload = roster(injuries=[{"status": "Out", "date": "2026-11-04T20:00:00Z"},
                              {"status": "Available"}])
    result = parse_roster(payload, observed(payload, tmp_path), "1")
    player = result["players"][0]
    assert player["injury_status"] == "CONFLICTING"
    assert player["availability"] == "UNVERIFIED"
    assert "Conflicting" in result["errors"][0]["reason"]


@pytest.mark.parametrize("timestamp", ["2026-11-05T20:00:00Z", "2026-11-04T20:00:00"])
def test_future_or_naive_injury_timestamp_cannot_authorize_availability(tmp_path, timestamp):
    payload = roster(injuries=[{"status": "Available", "date": timestamp}])
    result = parse_roster(payload, observed(payload, tmp_path), "1")
    assert result["players"][0]["availability"] == "UNVERIFIED"
    assert result["errors"]


def test_roster_identity_hash_and_observation_validations(tmp_path):
    payload = roster()
    source = observed(payload, tmp_path)
    with pytest.raises(ValueError, match="URL does not match"):
        parse_roster(payload, source, "2")
    source["content_sha256"] = "bad"
    with pytest.raises(ValueError, match="content hash"):
        parse_roster(payload, source, "1")
    source = observed(payload, tmp_path, observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="timestamp or content hash"):
        parse_roster(payload, source, "1")


def test_refresh_persists_verified_context_and_loads_without_network(tmp_path):
    result = refresh_context(slate(), tmp_path, provider_fetch)
    assert result["status"] == "AVAILABLE"
    assert result["counts"]["teams_acquired"] == 2
    assert result["counts"]["players"] == 2
    assert result["counts"]["availability"] == {"UNVERIFIED": 2}
    assert result["games"]["100"]["status"] == "AVAILABLE"
    assert len(result["games"]["100"]["players"]) == 2
    assert "NOT_PERFORMED" in result["cross_source_comparison"]
    assert hashlib.sha256(Path(result["snapshot_path"]).read_bytes()).hexdigest() == result["hash"]
    loaded = load_context(tmp_path)
    assert loaded["hash"] == result["hash"]
    assert loaded["games"] == result["games"]
    assert not loaded["stale"]


def test_partial_roster_failure_preserves_other_observed_team(tmp_path):
    def partial_fetch(url, directory):
        if "/teams/2/" in url:
            raise OSError("provider missing roster")
        return provider_fetch(url, directory)
    result = refresh_context(slate(), tmp_path, partial_fetch)
    assert result["status"] == "PARTIAL"
    assert result["games"]["100"]["missing_team_ids"] == ["2"]
    assert result["counts"]["teams_acquired"] == 1
    assert "provider missing" in result["errors"][0]["reason"]


def test_membership_conflict_is_exposed_and_cannot_be_available(tmp_path):
    def conflict_fetch(url, directory):
        tid = url.split("/")[-2]
        payload = roster(tid, [{"status": "Available"}])
        payload["athletes"][0]["id"] = "999"
        return payload, observed(payload, directory, tid)
    result = refresh_context(slate(), tmp_path, conflict_fetch)
    assert result["status"] == "PARTIAL"
    assert result["membership_conflicts"][0]["player_id"] == "999"
    assert all(p["availability"] == "UNVERIFIED" for p in result["games"]["100"]["players"])
    assert all(p["roster_status"] == "CONFLICTING_MEMBERSHIP" for p in result["games"]["100"]["players"])


def test_context_expiration_is_based_on_source_observation_not_snapshot_publication(tmp_path, monkeypatch):
    refresh_context(slate(), tmp_path, provider_fetch)
    monkeypatch.setattr("nba_quant.context._now", lambda: NOW + timedelta(hours=2))
    result = load_context(tmp_path)
    assert result["status"] == "LAST_GOOD"
    assert result["last_good_status"] == "AVAILABLE"
    assert result["age_seconds"] == 7200
    assert result["stale"]


def test_corrupt_context_or_source_is_unavailable_without_fixture_fallback(tmp_path):
    result = refresh_context(slate(), tmp_path, provider_fetch)
    Path(result["sources"][0]["raw_path"]).write_text("{}")
    loaded = load_context(tmp_path)
    assert loaded["status"] == "UNAVAILABLE"
    assert loaded["games"] == {}
    assert "SHA256 mismatch" in loaded["reason"]


def test_no_context_is_explicitly_unavailable(tmp_path):
    result = load_context(tmp_path)
    assert result["status"] == "UNAVAILABLE"
    assert not result["games"]
    assert result["retrieved_at"] is None


def test_current_endpoint_cannot_establish_historical_roster(tmp_path):
    source_slate = slate()
    source_slate["date"] = "2026-11-03"
    with pytest.raises(ValueError, match="historical or future"):
        refresh_context(source_slate, tmp_path, provider_fetch)


def test_stale_schedule_cannot_seed_fresh_context(tmp_path):
    source_slate = slate()
    source_slate["stale"] = True
    with pytest.raises(ValueError, match="fresh AVAILABLE"):
        refresh_context(source_slate, tmp_path, provider_fetch)
