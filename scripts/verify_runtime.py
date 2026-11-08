"""Exercise the running local server and persist evidence; no external providers."""
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "http://127.0.0.1:8020"
ROOT = Path(__file__).resolve().parents[1]


def call(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read())


status = call("/api/status")
dataset = next((d for d in status["datasets"] if d["synthetic"]), None)
if dataset is None:
    key = call("/api/demo", {})["dataset_id"]
    dataset = next(d for d in call("/api/status")["datasets"] if d["id"] == key)
now = datetime.now(timezone.utc)
request = {"dataset_id": dataset["id"], "home_id": dataset["teams"][0]["id"],
           "away_id": dataset["teams"][1]["id"], "tipoff": (now + timedelta(days=1)).isoformat(),
           "cutoff": now.isoformat(), "seed": 42, "simulations": 250, "scenarios": []}
f = call("/api/forecasts", request)
saved = call(f"/api/forecasts/{f['id']}/export")
assert saved == f, "Persisted forecast differs from HTTP response"
g = f["result"]["representative_game"]
for i, tid in enumerate((request["home_id"], request["away_id"])):
    ps = [p["id"] for p in f["result"]["players"] if p["team_id"] == tid]
    assert sum(g["boxes"][pid]["pts"] for pid in ps) == g["score"][i]
    assert sum(g["boxes"][pid]["minutes"] for pid in ps) == 240 + 25 * g["overtime_periods"]
metrics = call(f"/api/metrics?dataset_id={dataset['id']}&scope=team&entity_id={request['home_id']}")
assert metrics["coverage"]["total"] == 240
assert any(m["status"] == "UNAVAILABLE" and m["value"] is None for m in metrics["metrics"])
assert call("/api/evaluation")["status"] == "NOT MEASURABLE", "Runtime must not imply measured accuracy"
evidence = {"verified_at": datetime.now(timezone.utc).isoformat(), "base_url": BASE,
    "forecast_id": f["id"], "synthetic": f["synthetic"], "games_in_fixture": dataset["games_count"],
    "simulations": request["simulations"], "persisted_forecast_equal": True,
    "score_and_minutes_reconciled": True, "team_metric_coverage": metrics["coverage"],
    "real_nba_accuracy": "NOT MEASURABLE", "browser_visual_qa": "UNVERIFIED: local browser use disabled in Work settings"}
(ROOT / "output").mkdir(exist_ok=True)
for name, value in [("runtime-evidence.json", evidence), ("latest-forecast.json", f), ("forecast-request.json", request)]:
    (ROOT / "output" / name).write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
print(json.dumps(evidence, indent=2))
