"""Check the running nightly board against its actual archived provider response."""
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "http://127.0.0.1:8020"
ROOT = Path(__file__).resolve().parents[1]

with urllib.request.urlopen(BASE + "/api/tonight", timeout=60) as response:
    slate = json.load(response)
assert slate["date"] == datetime.now(ZoneInfo("America/New_York")).date().isoformat()
assert slate["status"] == "AVAILABLE", slate
assert slate["games"] and len({g["id"] for g in slate["games"]}) == len(slate["games"])
for game in slate["games"]:
    assert datetime.fromisoformat(game["tipoff"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date().isoformat() == slate["date"]
    assert game["home"]["id"] != game["away"]["id"]
    assert game["sgp"]["offered_props"] == []
    assert all(f["synthetic"] is False and f["request"]["home_id"] == game["home"]["id"]
               and f["request"]["away_id"] == game["away"]["id"] for f in game["sgp"]["forecasts"])
with urllib.request.urlopen(BASE + "/app.js", timeout=15) as response:
    assert response.read() == (ROOT / "nba_quant/static/app.js").read_bytes()
evidence = {"verified_at": datetime.now(timezone.utc).isoformat(), "date": slate["date"],
    "timezone": slate["timezone"], "status": slate["status"], "retrieved_at": slate["retrieved_at"],
    "source_url": slate["source_url"], "games_count": len(slate["games"]),
    "matchups": [g["away"]["name"] + " @ " + g["home"]["name"] for g in slate["games"]],
    "saved_sgp_count": sum(len(g["sgp"]["tickets"]) for g in slate["games"]),
    "offered_props_count": 0, "actionable": False,
    "current_js_served": True, "browser_visual_qa": "UNVERIFIED"}
(ROOT / "output/tonight-runtime-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
print(json.dumps(evidence, indent=2))
