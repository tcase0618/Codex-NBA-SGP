"""Actual-package software acceptance, explicitly synthetic; no NBA accuracy claim."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

from nba_quant.ml_engine import train_run, predict_run, registry_event


def fixture():
    rng = np.random.default_rng(42)
    rows = []
    start = datetime(2025, 1, 1, 18, tzinfo=timezone.utc)
    for day in range(60):
        tip = start + timedelta(days=day)
        for index in range(8):
            x = float(rng.uniform(-2, 2))
            outcome = float(12 + 6*x*x + rng.normal(0, .3))
            features = {"ability_proxy": x, "opportunity_proxy": float(index),
                        "player_pts_mean": 20., "player_pts_recent3_mean": 20.,
                        "team_win_rate": .5}
            for scope, targets in (("player", {"pts": outcome}), ("team", {"win": int(x > 0)})):
                rows.append({"game_id": f"synthetic-{day}", "scope": scope, "entity_id": f"{scope}-{index}",
                    "team_id": f"team-{index}", "opponent_id": "synthetic-opponent",
                    "tipoff": tip.isoformat(), "cutoff": (tip-timedelta(hours=1)).isoformat(),
                    "label_completed_at": (tip+timedelta(hours=3)).isoformat(),
                    "label_known_at": (tip+timedelta(hours=4)).isoformat(),
                    "point_in_time_mode": "strict", "production_eligible": False,
                    "features": features, "targets": targets, "provenance": {"synthetic": True}})
    return {"rows": rows, "audit": {"synthetic": True, "mode": "SYNTHETIC_SOFTWARE_FIXTURE"}}


def main():
    root = Path("output/ml/software-acceptance")
    root.mkdir(parents=True, exist_ok=True)
    bundle = fixture()
    report = {"cohort": "SYNTHETIC_SOFTWARE_ONLY", "nba_accuracy": "NOT MEASURABLE", "runs": []}
    for target in ("pts", "win"):
        started = time.monotonic()
        manifest = train_run(bundle, target, output_dir=root, time_limit=90, num_cpus=1)
        assert all(manifest["candidates"][name]["status"] == "FIT" for name in ("autogluon", "xgboost")), manifest["errors"]
        rows = [r for r in bundle["rows"] if target in r["targets"]][-3:]
        request_file = root / f"reload-{manifest['id']}.json"
        request_file.write_text(json.dumps(rows))
        restored = []
        for candidate in (None, "autogluon", "xgboost"):
            initial = predict_run(root, manifest["id"], rows, candidate=candidate)
            code = "from nba_quant.ml_engine import predict_run; import json,sys; print(json.dumps(predict_run(sys.argv[1],sys.argv[2],json.load(open(sys.argv[3])),candidate=None if sys.argv[4]=='champion' else sys.argv[4])))"
            child = subprocess.run([sys.executable, "-c", code, str(root), manifest["id"], str(request_file), candidate or "champion"],
                capture_output=True, text=True, check=True, timeout=120)
            after = json.loads(child.stdout)
            assert after["predictions"] == initial["predictions"]
            restored.append(candidate or "champion")
        try:
            registry_event(root, "promote", manifest["id"])
        except ValueError:
            blocked = True
        else:
            raise AssertionError("Synthetic model promotion must fail")
        report["runs"].append({"target": target, "id": manifest["id"], "backends_fit": ["autogluon", "xgboost"],
            "restored_in_new_process": restored, "synthetic_promotion_blocked": blocked,
            "seconds": time.monotonic()-started})
    (root / "verification.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
