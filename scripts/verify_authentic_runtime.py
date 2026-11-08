"""Verify persisted authentic source coverage and fail-closed SGP boundaries."""
import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8020"


def call(path, body=None):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


proof = json.loads((ROOT / "output/authentic-history/import-verification.json").read_text(encoding="utf-8"))
manifest = json.loads(Path(proof["manifest_path"]).read_text(encoding="utf-8"))
for source in manifest["sources"]:
    assert hashlib.sha256(Path(source["raw_path"]).read_bytes()).hexdigest() == source["sha256"]
status = call("/api/status")
real = next(d for d in status["datasets"] if d["id"] == proof["dataset_id"])
assert real["synthetic"] is False and real["rotation_count"] == 0
assert real["games_count"] == proof["games"]
profiles = call(f"/api/profiles?dataset_id={real['id']}")
assert len(profiles["teams"]) == 30 and len(profiles["players"]) == proof["players"]
assert any(m["value"] is not None for p in profiles["players"] for m in p["metrics"])
now = datetime.now(timezone.utc)
try:
    call("/api/forecasts", {"dataset_id": real["id"], "home_id": real["teams"][0]["id"],
        "away_id": real["teams"][1]["id"], "tipoff": (now + timedelta(days=1)).isoformat(),
        "cutoff": now.isoformat(), "simulations": 20})
except urllib.error.HTTPError as exc:
    assert exc.code == 422
    error = json.load(exc)["detail"]
    assert "eligible players required" in error
else:
    raise AssertionError("History-only data must not invent current rotations")
sources = call("/api/sources")
assert sources["readiness"]["actionable"] is False
assert sources["target_sportsbook"] == "FanDuel"
assert sources["odds"]["status"] == "UNAVAILABLE"
assert sources["injuries"]["eligible_for_automatic_roster_gate"] is False
eligible_demo = next((f for f in call("/api/forecasts") if f["synthetic"] and datetime.fromisoformat(f["request"]["tipoff"]) > now), None)
sgp_proof = {"status": "NO_ELIGIBLE_DEMO_FORECAST"}
if eligible_demo:
    f = call(f"/api/forecasts/{eligible_demo['id']}")
    selections = [{"player_id": p["id"], "stat": "pts", "side": "over", "line": 10.5} for p in f["result"]["players"][:2]]
    sgp = call("/api/sgp/evaluate", {"forecast_id": f["id"], "legs": selections})
    n = f["request"]["simulations"]
    samples = [p["samples"]["pts"] for p in f["result"]["players"][:2]]
    assert sgp["joint_probability"] == sum(all(v[i] > 10.5 for v in samples) for i in range(n)) / n
    assert sgp["actionable"] is False and sgp["expected_return_per_unit"] is None
    assert call(f"/api/sgp/{sgp['id']}") == sgp
    sgp_proof = {"status": "VERIFIED_SYNTHETIC_ONLY", "sgp_id": sgp["id"],
                 "joint_calculation_checked": True, "persisted_reload_equal": True, "betting_use_blocked": True}
evidence = {"verified_at": now.isoformat(), "dataset_id": real["id"], "authentic_games": real["games_count"],
    "teams": len(real["teams"]), "players": len(real["players"]),
    "raw_source_hashes_verified": len(manifest["sources"]), "rejected_games": len(manifest["rejected"]),
    "profiles_browsable": True, "missing_rotations_forecast_rejected": True,
    "sources": sources, "sgp_runtime": sgp_proof, "independent_accuracy": "NOT MEASURABLE",
    "browser_visual_qa": "UNVERIFIED"}
(ROOT / "output/authentic-runtime-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in evidence.items() if k != "sources"}, indent=2))
