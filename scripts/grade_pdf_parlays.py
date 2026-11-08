"""Grade the exact frozen PDF thresholds; no sportsbook settlement assumptions."""
import collections
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/review-2026-11-04"


def grade_leg(leg, box):
    rows = [p for p in box["players"] if p["id"] == leg["player_id"]]
    result = {**leg, "actual": None, "remaining": None, "status": "UNRESOLVED", "reason": None}
    if box["completed"] and box.get("canonical_status") != "VALIDATED_FINAL":
        result["reason"] = "Final box score has not passed source reconciliation"
        return result
    if len(rows) != 1:
        result["reason"] = "Player missing or ambiguous in the matching game's box score"
        return result
    row = rows[0]
    result.update(minutes=row.get("stats", {}).get("minutes"), provider_player_name=row["name"])
    if row["team_id"] != leg["team_id"]:
        result["reason"] = "Box-score team does not match the frozen selection's team"
        return result
    if row["did_not_play"]:
        result.update(status="DNP", reason="Provider explicitly lists DNP; sportsbook void/repricing rules are unverified")
        return result
    actual = row.get("stats", {}).get(leg["stat"])
    if actual is None or row.get("row_flags"):
        result["reason"] = "Missing or inconsistent source statistic; not treated as zero"
        return result
    assert leg["side"] == "at_least"
    result.update(actual=actual, remaining=max(0, leg["line"] - actual))
    if actual >= leg["line"]:
        result["status"] = "PASS"
    elif box["completed"]:
        result["status"] = "FAIL"
    else:
        result["status"] = "PENDING"
    result["ejected"] = row.get("ejected", False)
    if not box["completed"] and result["status"] == "PENDING" and (row.get("ejected") or row.get("stats", {}).get("pf", 0) >= 6):
        result.update(status="FAIL", reason="Player cannot return after ejection or six personal fouls")
    return result


def grade_family(family, box):
    if not family["legs"]:
        return {**family, "status": "BLOCKED", "legs": [], "hit_probability": None, "probability_note": "No ticket was constructed"}
    legs = [grade_leg(leg, box) for leg in family["legs"]]
    statuses = [leg["status"] for leg in legs]
    if "FAIL" in statuses:
        status, probability, note = "MISS", 0., "At least one selected threshold is definitively missed; not a model prediction"
    elif "UNRESOLVED" in statuses:
        status, probability, note = "UNRESOLVED", None, "Required source statistic or player identity is unresolved"
    elif "DNP" in statuses:
        status, probability, note = "DNP_RULES_UNVERIFIED", None, "DNP void/repricing rules not supplied; no win/loss assumed"
    elif "PENDING" in statuses:
        status, probability, note = "PENDING", None, "Unfinished game; no calibrated live joint probability model is available"
    else:
        status, probability, note = ("HIT" if box["completed"] else "SECURED"), 1., "All selected thresholds met; live stats remain subject to official corrections"
    return {**family, "status": status, "legs": legs, "hit_probability": probability, "probability_note": note,
            "passed_legs": statuses.count("PASS"), "failed_legs": statuses.count("FAIL"),
            "pending_legs": statuses.count("PENDING"), "dnp_legs": statuses.count("DNP"),
            "unresolved_legs": statuses.count("UNRESOLVED")}


def main():
    frozen = json.loads((OUT / "frozen-input.json").read_text(encoding="utf-8"))
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    games = []
    for game in frozen["games"]:
        source = manifest["games"][game["id"]]
        assert game["id"] == source["id"]
        for receipt in [source["source"]]:
            assert hashlib.sha256(Path(receipt["raw_path"]).read_bytes()).hexdigest() == receipt["sha256"]
        gs = {**game, "box": {k: v for k, v in source.items() if k != "players"},
              "families": [grade_family(f, source) for f in game["families"]]}
        games.append(gs)
    ticket_counts = collections.Counter(f["status"] for g in games for f in g["families"])
    entries = [p for g in games for f in g["families"] for p in f["legs"]]
    unique = {(g["id"], p["player_id"], p["stat"], p["side"], p["line"]): p for g in games for f in g["families"] for p in f["legs"]}
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "source_pdf": frozen["source_pdf"],
        "pdf_sha256": frozen["pdf_sha256"], "source_as_of": manifest["acquired_at"],
        "ticket_counts": dict(ticket_counts), "leg_entry_counts": dict(collections.Counter(p["status"] for p in entries)),
        "unique_leg_counts": dict(collections.Counter(p["status"] for p in unique.values())),
        "games_final": sum(g["box"]["completed"] for g in games), "games": games,
        "limitations": ["Exact PDF threshold review, not an account betting ledger or sportsbook settlement.",
            "No offered combined prices or stake records exist, so ROI and monetary returns are not measurable.",
            "DNP and missing player rows remain distinct; no zero totals or void outcomes are invented.",
            "Incomplete live joint probabilities are unavailable until a defensible model and live inputs exist.",
            "Repeated legs across nested 3/5/8-leg families are reported separately from unique selections."]}
    (OUT / "graded-review.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with (OUT / "all-leg-results.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        fields = ["game", "ticket_size", "ticket_status", "player", "stat", "threshold", "actual", "leg_status", "remaining", "game_status", "period", "clock", "source_url"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for g in games:
            for f in g["families"]:
                for p in f["legs"]:
                    writer.writerow(dict(zip(fields, [g["game"], f["size"], f["status"], p["name"], p["stat"], p["line"], p["actual"], p["status"], p["remaining"], g["box"]["status"], g["box"]["period"], g["box"]["clock"], g["box"]["source_url"]])))
    print(json.dumps({k: result[k] for k in ("source_as_of", "ticket_counts", "leg_entry_counts", "unique_leg_counts", "games_final")}, indent=2))
    for g in games:
        print(g["game"],g["box"]["status"],g["box"]["period"],g["box"]["clock"],[(f["size"],f["status"]) for f in g["families"]])


if __name__ == "__main__":
    main()
