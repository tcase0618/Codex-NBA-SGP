import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nba_quant.slate import load_slate
from nba_quant.store import digest

NOW = datetime(2026, 11, 4, 22, tzinfo=timezone.utc)


def event(event_id="1", tipoff="2026-11-05T00:30Z"):
    return {"id": event_id, "date": tipoff, "status": {"type": {
        "name": "STATUS_SCHEDULED", "completed": False, "detail": "7:30 PM EST"}},
        "competitions": [{"id": event_id, "date": tipoff, "competitors": [
            {"homeAway": "home", "team": {"id": "4", "displayName": "Chicago Bulls", "abbreviation": "CHI"}},
            {"homeAway": "away", "team": {"id": "2", "displayName": "Boston Celtics", "abbreviation": "BOS"}}]}]}


def fetcher(payload, clock=lambda: NOW, calls=None):
    def fetch(url, directory):
        if calls is not None:
            calls.append(url)
        raw = json.dumps(payload).encode()
        sha = hashlib.sha256(raw).hexdigest()
        path = Path(directory) / "raw" / f"{sha}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return copy.deepcopy(payload), {"provider": "ESPN", "url": url,
            "retrieved_at": clock().isoformat(), "sha256": sha,
            "content_sha256": digest(payload), "raw_path": str(path.resolve())}
    return fetch


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("nba_quant.slate._now", lambda: NOW)


def failure(*_args):
    raise OSError("provider offline")


def test_eastern_calendar_day_includes_next_utc_date_not_next_eastern_day(tmp_path):
    payload = {"events": [event("1", "2026-11-05T00:30Z"), event("2", "2026-11-05T05:30Z")]}
    result = load_slate(tmp_path, fetcher=fetcher(payload))
    assert result["date"] == "2026-11-04"
    assert result["timezone"] == "America/New_York"
    assert result["status"] == "AVAILABLE"
    assert [g["id"] for g in result["games"]] == ["1"]
    assert result["games"][0]["home"]["name"] == "Chicago Bulls"
    assert result["games"][0]["status"] == "STATUS_SCHEDULED"
    assert result["games"][0]["phase"] == "pre"
    assert not result["games"][0]["completed"]
    assert result["age_seconds"] == 0
    assert result["cache_status"] == "FETCHED"


def test_empty_provider_slate_is_explicit_not_league_schedule_claim(tmp_path):
    result = load_slate(tmp_path, fetcher=fetcher({"events": []}))
    assert result["status"] == "EMPTY_PROVIDER_SLATE"
    assert not result["games"]
    assert "does not independently establish" in result["reason"]


def test_fresh_verified_cache_avoids_network_and_preserves_observation_time(tmp_path):
    initial = load_slate(tmp_path, fetcher=fetcher({"events": [event()]}))
    cached = load_slate(tmp_path, fetcher=failure)
    assert cached["status"] == "AVAILABLE"
    assert cached["cache_status"] == "FRESH_CACHE"
    assert cached["retrieved_at"] == initial["retrieved_at"]


def test_expired_cache_network_failure_returns_verified_last_good(tmp_path, monkeypatch):
    initial = load_slate(tmp_path, fetcher=fetcher({"events": [event()]}))
    monkeypatch.setattr("nba_quant.slate._now", lambda: NOW + timedelta(minutes=6))
    result = load_slate(tmp_path, fetcher=failure)
    assert result["status"] == "LAST_GOOD"
    assert result["last_good_status"] == "AVAILABLE"
    assert result["stale"]
    assert result["age_seconds"] == 360
    assert result["retrieved_at"] == initial["retrieved_at"]
    assert result["games"] == initial["games"]
    assert "provider offline" in result["reason"]


def test_failed_refresh_does_not_claim_freshness_even_with_recent_cache(tmp_path):
    load_slate(tmp_path, fetcher=fetcher({"events": [event()]}))
    result = load_slate(tmp_path, refresh=True, fetcher=failure)
    assert result["status"] == "LAST_GOOD"
    assert result["stale"]
    assert result["cache_status"] == "REFRESH_FAILED"


def test_network_failure_without_cache_returns_no_fixture_games(tmp_path):
    result = load_slate(tmp_path, fetcher=failure)
    assert result["status"] == "UNAVAILABLE"
    assert result["retrieved_at"] is None
    assert result["games"] == []


def test_corrupted_cached_raw_archive_is_never_last_good(tmp_path):
    result = load_slate(tmp_path, fetcher=fetcher({"events": [event()]}))
    Path(result["source_raw_path"]).write_text("{}")
    rejected = load_slate(tmp_path, fetcher=failure)
    assert rejected["status"] == "UNAVAILABLE"
    assert rejected["games"] == []
    assert "SHA256 mismatch" in rejected["reason"]


def test_expired_response_from_fetcher_is_not_fresh(tmp_path):
    result = load_slate(tmp_path, fetcher=fetcher({"events": [event()]}, lambda: NOW - timedelta(minutes=8)))
    assert result["status"] == "LAST_GOOD"
    assert result["age_seconds"] == 480
    assert result["stale"]


def test_future_observation_is_rejected(tmp_path):
    result = load_slate(tmp_path, fetcher=fetcher({"events": [event()]}, lambda: NOW + timedelta(seconds=1)))
    assert result["status"] == "UNAVAILABLE"
    assert "future" in result["reason"]


@pytest.mark.parametrize("mutation,reason", [
    (lambda p: p["events"].append(p["events"][0]), "Duplicate provider"),
    (lambda p: p["events"][0].update(date="2026-11-04T19:30:00"), "Timezone-aware"),
    (lambda p: p["events"][0]["competitions"][0]["competitors"].pop(), "distinct home and away"),
    (lambda p: p["events"][0]["competitions"][0]["competitors"][1]["team"].update(id="4"), "same team"),
    (lambda p: p["events"][0]["competitions"][0].update(date="2026-11-05T01:00Z"), "tipoff mismatch"),
    (lambda p: p["events"][0]["status"]["type"].update(completed="false"), "boolean"),
    (lambda p: p.pop("events"), "no events list"),
])
def test_malformed_schedule_fails_closed(tmp_path, mutation, reason):
    payload = {"events": [event()]}
    mutation(payload)
    result = load_slate(tmp_path, fetcher=fetcher(payload))
    assert result["status"] == "UNAVAILABLE"
    assert result["games"] == []
    assert reason in result["reason"]


def test_days_have_separate_cache_keys(tmp_path):
    load_slate(tmp_path, "2026-11-04", fetcher=fetcher({"events": [event()]}))
    other = load_slate(tmp_path, "2026-11-05", fetcher=failure)
    assert other["status"] == "UNAVAILABLE"
    assert other["date"] == "2026-11-05"


def test_singleflight_concurrent_reads_fetch_once(tmp_path):
    calls = []
    underlying = fetcher({"events": [event()]}, calls=calls)
    gate = threading.Barrier(6)
    def read():
        gate.wait(timeout=5)
        return load_slate(tmp_path, fetcher=underlying)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: read(), range(6)))
    assert len(calls) == 1
    assert all(r["status"] == "AVAILABLE" for r in results)
