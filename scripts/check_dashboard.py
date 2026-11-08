"""Reproduce dashboard checks with an isolated API fixture and a lightweight DOM.

This does not launch a browser or verify visual layout. The temporary database and
JSON fixture are deleted on exit; the configured research database is untouched.
Run from the project directory: python scripts/check_dashboard.py
"""
import json
import shutil
import runpy
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from nba_quant.api import create_app  # noqa: E402
from nba_quant.board import player_history  # noqa: E402
from nba_quant.construction import construct_game_drafts  # noqa: E402


def main():
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js is required for the dashboard JavaScript checks")
    subprocess.run([node, "--check", str(ROOT / "nba_quant/static/app.js")], check=True)
    with tempfile.TemporaryDirectory(prefix="nba-quant-ui-check-") as directory:
        temporary = Path(directory)
        with TestClient(create_app(temporary / "ui.sqlite")) as client:
            response = client.post("/api/demo")
            response.raise_for_status()
            key = response.json()["dataset_id"]
            status = client.get("/api/status").json()
            dataset = next(d for d in status["datasets"] if d["id"] == key)
            now = datetime.now(timezone.utc)
            request = {
                "dataset_id": key,
                "home_id": dataset["teams"][0]["id"],
                "away_id": dataset["teams"][1]["id"],
                "cutoff": now.isoformat(),
                "tipoff": (now + timedelta(days=1)).isoformat(),
                "simulations": 20,
                "seed": 42,
                "scenarios": [],
            }
            response = client.post("/api/forecasts", json=request)
            response.raise_for_status()
            fixture = {
                "status": status,
                "forecast": response.json(),
                "history": client.get("/api/forecasts").json(),
                "profiles": client.get("/api/profiles", params={"dataset_id": key}).json(),
                "metrics": client.get("/api/metrics", params={
                    "dataset_id": key, "entity_id": dataset["players"][0]["id"]
                }).json(),
                "evaluation": client.get("/api/evaluation").json(),
            }
            sources = client.get("/api/sources")
            sources.raise_for_status()
            fixture["sources"] = sources.json()
            forecast = fixture["forecast"]
            fixture["sgp_request"] = {
                "forecast_id": forecast["id"],
                "legs": [
                    {"player_id": forecast["result"]["players"][0]["id"], "stat": "pts", "side": "over", "line": 10.5},
                    {"player_id": forecast["result"]["players"][1]["id"], "stat": "reb", "side": "at_least", "line": 3},
                ],
                "quote": None,
            }
            sgp = client.post("/api/sgp/evaluate", json=fixture["sgp_request"])
            sgp.raise_for_status()
            fixture["sgp"] = sgp.json()
            assert fixture["sgp"]["actionable"] is False
            assert fixture["sgp"]["synthetic"] is True
            assert fixture["sgp"]["expected_return_per_unit"] is None
            # Isolate the schedule provider. These fictional test teams exercise
            # endpoint enrichment, not real-world NBA schedule accuracy.
            schedule = {
                "date": now.astimezone(timezone(timedelta(hours=-5))).date().isoformat(),
                "timezone": "America/New_York", "status": "AVAILABLE",
                "retrieved_at": now.isoformat(), "source_url": "fixture://dashboard-schedule",
                "games": [{
                    "id": "ui-test-game", "home": {**dataset["teams"][0], "abbreviation": "HBR"},
                    "away": {**dataset["teams"][1], "abbreviation": "MET"},
                    "tipoff": request["tipoff"], "status": "STATUS_SCHEDULED", "phase": "pre", "status_detail": "Scheduled test fixture",
                    "completed": False, "source_url": "fixture://dashboard-schedule/ui-test-game",
                }],
            }
            with patch("nba_quant.api.load_slate", return_value=schedule):
                slate = client.get("/api/tonight")
                slate.raise_for_status()
                fixture["tonight"] = slate.json()
                refreshed = client.post("/api/tonight/refresh", json={})
                refreshed.raise_for_status()
                assert refreshed.json()["games"][0]["id"] == "ui-test-game"
            # Synthetic forecasts must not match a real schedule enrichment path.
            assert not fixture["tonight"]["games"][0]["sgp"]["forecasts"]
            assert not fixture["tonight"]["games"][0]["sgp"]["tickets"]
            # Reuse the explicitly fictional construction unit-test fixture. Its
            # non-synthetic flag exercises a gate only; it is never imported into
            # a database, emitted as a real NBA claim, or used for accuracy claims.
            test_fixture = runpy.run_path(str(ROOT / "tests/test_construction.py"))["fixture"]
            draft_game, draft_history, roster, cutoff = test_fixture()
            construction = construct_game_drafts(draft_game, draft_history, roster, as_of=cutoff)
            fixture["product_game"] = {
                **draft_game,
                "home": {"id": "H", "name": "Fictional Home", "abbreviation": "HOM"},
                "away": {"id": "A", "name": "Fictional Away", "abbreviation": "AWY"},
                "status": "STATUS_SCHEDULED", "status_detail": "Fictional fixture only",
                "history": {"home_games": 5, "away_games": 5},
                "context": {"status": "AVAILABLE", "players": [
                    {**player, "historical": player_history(draft_history, player["id"], player["team_id"], cutoff)}
                    for player in roster
                ]},
                "sgp": {"draft_families": construction["families"], "candidates": construction["candidates"],
                        "tickets": [], "offered_props": [], "published_props": [], "forecasts": [],
                        "block_reasons": construction["block_reasons"]},
            }
            fixture_path = temporary / "fixture.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            subprocess.run([
                node, str(ROOT / "scripts/check_dashboard.cjs"), str(fixture_path)
            ], cwd=ROOT, check=True)
    print("Temporary API fixture and SQLite database cleaned up. No browser visual check performed.")


if __name__ == "__main__":
    main()
