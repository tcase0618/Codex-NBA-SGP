"""Verify source-populated draft construction over the actual local HTTP service."""
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://127.0.0.1:8020"
ROOT = Path(__file__).resolve().parents[1]


def call(path, payload=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


job = call("/api/tonight/build", {})
deadline = time.monotonic() + 180
while job["status"] == "RUNNING" and time.monotonic() < deadline:
    time.sleep(1)
    job = call(f"/api/build-jobs/{job['job_id']}")
assert job["status"] == "COMPLETE", job
board = call("/api/tonight")
assert board["status"] == "AVAILABLE"
families, complete, blocked = [], 0, 0
published = []
for game in board["games"]:
    roster = {p["id"]: p for p in game["context"]["players"]}
    for family in game["sgp"]["draft_families"]:
        families.append(family)
        assert family["actionable"] is False and family["joint_probability"] is None
        assert family["combined_odds"] is None
        if family["status"] == "BLOCKED":
            assert not family["legs"]
            blocked += 1
            continue
        complete += 1
        assert len(family["legs"]) == family["leg_count"]
        assert len({p["player_id"] for p in family["legs"]}) == family["leg_count"]
        assert {p["team_id"] for p in family["legs"]} == {game["home"]["id"], game["away"]["id"]}
        for leg in family["legs"]:
            current = roster[leg["player_id"]]
            assert current["availability"] not in {"OUT", "DOUBTFUL", "QUESTIONABLE"}
            assert leg["descriptive"]["games"] >= 3
            assert leg["threshold_kind"] == "ANALYTICAL_THRESHOLD_NOT_OFFERED"
    published.extend(game["sgp"].get("published_props", []))
assert complete > 0
assert published, "Automatic primary FanDuel collection produced no mapped published quotes"
assert all(p["price_freshness"] == "UNKNOWN" and p["eligible_for_current_pricing"] is False for p in published)
proof = {"verified_at": datetime.now(timezone.utc).isoformat(), "job": job,
    "games": len(board["games"]), "roster_players": sum(len(g["context"]["players"]) for g in board["games"]),
    "draft_families": len(families), "constructed_families": complete, "blocked_families": blocked,
    "published_fanduel_quotes": len(published), "verified_current_prices": 0,
    "published_prices": [{k: p[k] for k in ("player_name", "stat", "side", "line", "american_odds", "source_url")} for p in published],
    "joint_probability_measured": False, "actionable": False,
    "browser_visual_qa": "UNVERIFIED"}
(ROOT / "output/product-runtime-evidence.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
print(json.dumps(proof, indent=2))
