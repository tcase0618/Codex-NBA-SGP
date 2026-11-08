import copy
import hashlib
import json
from pathlib import Path

import pytest

from nba_quant.providers import BASE, acquire_history, archived_fetcher, fetch_json, parse_espn_summary
from nba_quant.schemas import Dataset, utc
from nba_quant.store import digest


def provider_fixture():
    labels = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF", "+/-"]
    competitors, sections = [], []
    for team, side, pts in [("1", "home", 20), ("2", "away", 18)]:
        competitors.append({"id": team, "homeAway": side, "score": str(pts * 5),
                            "team": {"id": team, "displayName": f"Team {team}"},
                            "linescores": [{"displayValue": x} for x in (["25"] * 4 if side == "home" else ["20", "20", "25", "25"])]})
        athletes = [{"athlete": {"id": f"{team}{i}", "displayName": f"Player {team}{i}",
                                  "position": {"abbreviation": "G" if i < 2 else "F" if i < 4 else "C"}},
                     "didNotPlay": False, "stats": ["48", str(pts), "9-20" if pts == 20 else "8-20", "2-5", "0-0",
                                                     "5", "2", "2", "1", "1", "1", "4", "2", "0"]} for i in range(5)]
        sections.append({"team": {"id": team}, "statistics": [{"labels": labels, "athletes": athletes}]})
    payload = {"header": {"id": "100", "season": {"year": 2025, "type": 2}, "competitions": [{
        "id": "100", "date": "2025-01-10T00:00Z", "competitors": competitors,
        "status": {"type": {"name": "STATUS_FINAL", "completed": True}}}]},
        "boxscore": {"players": sections}, "plays": [{"id": "end", "type": {"text": "End Game"},
            "period": {"number": 4}, "homeScore": 100, "awayScore": 90,
            "wallclock": "2025-01-10T03:00:00Z"}]}
    return payload


def provenance(payload):
    raw = json.dumps(payload).encode()
    return {"provider": "ESPN", "url": f"{BASE}/summary?event=100",
            "retrieved_at": "2025-01-11T00:00:00+00:00", "sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": digest(payload), "raw_path": "fixture.json"}


def test_parse_authentic_structure_preserves_ids_positions_and_observation_times():
    payload = provider_fixture()
    p = provenance(payload)
    game, teams, players, notes = parse_espn_summary(payload, p)
    assert game["id"] == "100"
    assert utc(game["known_at"]) == utc("2025-01-11T00:00:00+00:00")
    assert utc(game["completed_at"]) == utc("2025-01-10T03:00:00+00:00")
    assert game["known_at"] != game["tipoff"]
    assert {t["id"] for t in teams} == {"1", "2"}
    assert {p["position"] for p in players} == {"G", "F", "C"}
    assert notes["completion_evidence"]["play_id"] == "end"
    assert notes["season_type"] == 2
    assert all(b["minutes"] == 48 for b in game["boxes"])


@pytest.mark.parametrize("mutation,expected", [
    (lambda x: x["header"]["competitions"][0]["status"]["type"].update(name="STATUS_IN_PROGRESS"), "completed STATUS_FINAL"),
    (lambda x: x.update(plays=[]), "End Game wallclock"),
    (lambda x: x["plays"][0].pop("wallclock"), "End Game wallclock"),
    (lambda x: x["plays"][0].update(homeScore=101), "Final event score differs"),
    (lambda x: x["header"]["competitions"][0]["competitors"][0].update(score="101"), "Final event score differs"),
    (lambda x: x["header"]["competitions"][0]["competitors"][0]["linescores"].pop(), "Quarter/overtime scores"),
    (lambda x: x["boxscore"]["players"].pop(), "player-box sections"),
    (lambda x: x["boxscore"]["players"][0]["statistics"][0]["athletes"].pop(), "Team minutes|Player points|five participants|at least 10 items"),
    (lambda x: x["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"].pop(), "Incomplete player"),
    (lambda x: x["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"].__setitem__(2, "--"), "made-attempted"),
    (lambda x: x["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"].__setitem__(5, "8"), "rebounds do not reconcile"),
])
def test_reject_incomplete_or_incoherent_final_summary(mutation, expected):
    payload = provider_fixture()
    mutation(payload)
    with pytest.raises(ValueError, match=expected):
        parse_espn_summary(payload, provenance(payload))


def test_canonical_content_hash_detects_payload_changes():
    payload = provider_fixture()
    p = provenance(payload)
    payload["header"]["id"] = "999"
    with pytest.raises(ValueError, match="content hash mismatch"):
        parse_espn_summary(payload, p)


def test_explicit_dnp_is_zero_and_unknown_role_stays_unknown():
    payload = provider_fixture()
    payload["boxscore"]["players"][0]["statistics"][0]["athletes"].append({
        "athlete": {"id": "99", "displayName": "Explicit DNP", "position": {}},
        "didNotPlay": True, "stats": []})
    game, _, players, notes = parse_espn_summary(payload, provenance(payload))
    assert next(p for p in players if p["id"] == "99")["position"] == "UNKNOWN"
    box = next(b for b in game["boxes"] if b["player_id"] == "99")
    assert box["minutes"] == box["pts"] == 0
    assert notes["provider_positions"]["99"] == ""


def test_minutes_in_seconds_are_preserved_without_rescaling():
    payload = provider_fixture()
    athletes = payload["boxscore"]["players"][0]["statistics"][0]["athletes"]
    athletes[0]["stats"][0] = "47:59"
    game, _, _, notes = parse_espn_summary(payload, provenance(payload))
    assert game["boxes"][0]["minutes"] == pytest.approx(47 + 59 / 60)
    assert notes["minutes_precision"][game["boxes"][0]["player_id"]] == "seconds"


def test_whole_minute_rounding_discrepancy_is_disclosed_without_correction():
    payload = provider_fixture()
    payload["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][0] = "47"
    game, _, _, notes = parse_espn_summary(payload, provenance(payload))
    assert game["minutes_precision_seconds"] == 60
    assert game["boxes"][0]["minutes"] == 47
    assert notes["team_minutes"]["1"]["source_total"] == 239
    assert notes["team_minutes"]["1"]["source_minus_game_total"] == -1
    assert not notes["team_minutes"]["1"]["values_adjusted"]


def test_rounding_tolerance_does_not_accept_missing_minutes():
    payload = provider_fixture()
    payload["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][0] = "40"
    with pytest.raises(ValueError, match="Team minutes"):
        parse_espn_summary(payload, provenance(payload))


def test_overtime_comes_from_final_event_and_quarter_scores():
    payload = provider_fixture()
    payload["plays"][0]["period"]["number"] = 5
    for competitor in payload["header"]["competitions"][0]["competitors"]:
        competitor["linescores"].append({"displayValue": "0"})
    for team in payload["boxscore"]["players"]:
        for athlete in team["statistics"][0]["athletes"]:
            athlete["stats"][0] = "53"
    game, _, _, _ = parse_espn_summary(payload, provenance(payload))
    assert game["overtime_periods"] == 1
    assert sum(b["minutes"] for b in game["boxes"] if b["team_id"] == "1") == 265


def fake_fetch(payload, observed_calls):
    def fetch(url, directory):
        observed_calls.append(url)
        if "/scoreboard" in url:
            response = {"events": [{"id": "100", "status": {"type": {"completed": True}}},
                                   {"id": "101", "status": {"type": {"completed": False}}}]}
        else:
            response = payload
        p = provenance(response)
        p["url"] = url
        return response, p
    return fetch


def test_acquisition_outputs_schema_valid_real_dataset_without_inferred_rotations(tmp_path):
    calls = []
    result = acquire_history("2025-01-10", "2025-01-10", tmp_path, 1, fake_fetch(provider_fixture(), calls))
    assert result["status"] == "ACQUIRED"
    assert result["games_accepted"] == 1
    assert len(calls) == 2
    data = Dataset.model_validate_json(Path(result["dataset_path"]).read_text())
    assert not data.synthetic
    assert data.rotations == []
    assert data.games[0].known_at.isoformat() == "2025-01-11T00:00:00+00:00"
    assert result["dataset_sha256"] == digest(data.model_dump(mode="json"))
    assert Path(result["manifest_path"]).is_file()


def test_acquisition_rejects_preseason_preserving_error_manifest(tmp_path):
    payload = provider_fixture()
    payload["header"]["season"]["type"] = 1
    result = acquire_history("2025-01-10", "2025-01-10", tmp_path, 1, fake_fetch(payload, []))
    assert result["status"] == "NO_ACCEPTED_GAMES"
    assert result["dataset_path"] is None
    assert "regular-season" in result["rejected"][0]["reason"]
    assert len(result["sources"]) == 2


def test_acquisition_failure_does_not_fabricate_empty_dataset(tmp_path):
    def failure(*_args):
        raise OSError("unavailable")
    result = acquire_history("2025-01-10", "2025-01-10", tmp_path, 1, failure)
    assert result["status"] == "NO_ACCEPTED_GAMES"
    assert result["dataset_path"] is None
    assert result["rejected"] == [{"stage": "scoreboard", "date": "2025-01-10", "reason": "unavailable"}]


@pytest.mark.parametrize("start,end,limit", [("2025-02-02", "2025-01-01", 1), ("2025-01-01", "2025-02-02", 1), ("2025-01-01", "2025-01-02", 101), ("2025-01-01", "2025-01-02", 0)])
def test_acquisition_has_explicit_date_and_game_bounds(tmp_path, start, end, limit):
    with pytest.raises(ValueError, match="at most 31 days"):
        acquire_history(start, end, tmp_path, limit)


def test_fetch_allowlist_rejects_arbitrary_urls_before_network(tmp_path):
    with pytest.raises(ValueError, match="fixed HTTPS ESPN NBA"):
        fetch_json("https://example.com/summary", tmp_path)


def test_archived_replay_preserves_original_observation_time_and_hashes(tmp_path):
    payload = provider_fixture()
    p = provenance(payload)
    raw_path = tmp_path / "raw.json"
    raw_path.write_bytes(json.dumps(payload).encode())
    p["raw_path"] = str(raw_path)
    (tmp_path / "manifest-fixture.json").write_text(json.dumps({"sources": [p]}))
    fetched, metadata = archived_fetcher(tmp_path)(p["url"], tmp_path)
    assert fetched == payload
    assert metadata["retrieved_at"] == p["retrieved_at"]
    assert metadata["reused_archive"]
    raw_path.write_text("{}")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        archived_fetcher(tmp_path)(p["url"], tmp_path)
