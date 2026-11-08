import json
import subprocess
import sys


def test_installed_cli_expanding_window_and_export(tmp_path):
    db = tmp_path / "replay.sqlite"
    prefix = [sys.executable, "-m", "nba_quant", "--db", str(db)]
    imported = subprocess.run(prefix + ["demo"], check=True, capture_output=True, text=True)
    key = json.loads(imported.stdout)["dataset_id"]
    output = tmp_path / "walk-forward.json"
    subprocess.run(prefix + ["walk-forward", key, "--min-history", "10", "--limit", "2",
        "--simulations", "20", "--output", str(output)], check=True, capture_output=True, text=True)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert len(report["completed"]) == 2
    assert report["skipped"] == []
    assert report["evaluation"]["cohorts"]["synthetic"]["count"] == 2
    for row in report["completed"]:
        frozen = subprocess.run(prefix + ["export", row["forecast_id"]], check=True, capture_output=True, text=True)
        forecast = json.loads(frozen.stdout)
        assert forecast["model"]["games_used"] >= 10
        assert forecast["model"]["last_game_at"] < forecast["request"]["cutoff"]
        assert forecast["model"]["retrospective_source"] is True
    scored = subprocess.run(prefix + ["evaluate"], check=True, capture_output=True, text=True)
    assert json.loads(scored.stdout)["forecasts_scored"] == 2
