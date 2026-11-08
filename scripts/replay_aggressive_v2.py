"""Offline counterfactual replay from the full frozen Nov 5 pregame inputs.

No network calls, current context refresh, new quotes or outcome reads occur.
This is retrospective policy research, never a pregame performance claim.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nba_quant.schemas import utc
from nba_quant.store import canonical, digest

OUT = ROOT / "output/review-2026-11-05"
TARGET = ROOT / "output/polymarket/aggressive-tonight-2026-11-05.json"
POLY_HASH = "60bd99799de8b81010310154f5de1d68bb8951fb02a82b82ad6926f6e24344a7"


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare():
    target_raw = TARGET.read_bytes()
    original = json.loads(target_raw)
    assert original["date"] == "2026-11-05"
    assert {b["game_id"] for b in original["games"]} == {"401909936", "401909937"}
    dataset_ids = original["games"][0]["historical_dataset_ids"]
    assert all(b["historical_dataset_ids"] == dataset_ids for b in original["games"])
    db = sqlite3.connect(f"file:{(ROOT / 'data/nba_quant.sqlite').as_posix()}?mode=ro", uri=True)
    datasets, references = [], []
    exports = OUT / "replay-datasets"
    exports.mkdir(parents=True, exist_ok=True)
    for dataset_id in dataset_ids:
        row = db.execute("SELECT payload,imported_at FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        assert row is not None
        dataset = json.loads(row[0])
        assert digest(dataset) == dataset_id and dataset["synthetic"] is False
        assert all(utc(dataset["retrieved_at"]) <= utc(b["created_at"]) for b in original["games"])
        assert all(utc(row[1]) <= utc(b["created_at"]) for b in original["games"])
        path = exports / f"{dataset_id}.json"
        path.write_text(canonical(dataset), encoding="utf-8")
        references.append({"id": dataset_id, "path": str(path.resolve()), "sha256": sha_file(path), "imported_at": row[1]})
        datasets.append(dataset)
    db.close()
    teams, players, games, game_sources = {}, {}, {}, {}
    for dataset_id, dataset in zip(dataset_ids, datasets):
        teams.update({p["id"]: p for p in dataset["teams"]})
        players.update({p["id"]: p for p in dataset["players"]})
        for game in dataset["games"]:
            if game["id"] not in games or utc(game["known_at"]) >= utc(games[game["id"]]["known_at"]):
                games[game["id"]] = game
                game_sources[game["id"]] = dataset_id
    history = {"source": "Merged immutable imported NBA history", "source_url": "local://immutable-datasets",
        "retrieved_at": max(d["retrieved_at"] for d in datasets), "synthetic": False,
        "teams": list(teams.values()), "players": list(players.values()), "rotations": [],
        "games": list(games.values()), "dataset_ids": dataset_ids, "game_sources": game_sources}
    history["id"] = digest(history)
    assert all(b["history_hash"] == history["id"] for b in original["games"])
    assert len(history["games"]) == 114
    target_ids = {b["game_id"] for b in original["games"]}
    assert not target_ids.intersection(games)
    for bundle in original["games"]:
        for game in history["games"]:
            assert utc(game["known_at"]) <= utc(bundle["created_at"])
            assert utc(game["completed_at"]) < utc(bundle["created_at"])
    context_hash = original["games"][0]["context_hash"]
    assert all(b["context_hash"] == context_hash for b in original["games"])
    context_path = ROOT / f"output/context/context-{context_hash}.json"
    assert sha_file(context_path) == context_hash
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context_proofs = []
    for source in context["sources"]:
        assert sha_file(source["raw_path"]) == source["sha256"]
        assert digest(json.loads(Path(source["raw_path"]).read_bytes())) == source["content_sha256"]
        assert all(utc(source["retrieved_at"]) <= utc(b["created_at"]) for b in original["games"])
        context_proofs.append(source["sha256"])
    poly_path = ROOT / f"output/polymarket/snapshot-{POLY_HASH}.json"
    assert sha_file(poly_path) == POLY_HASH
    report = json.loads(poly_path.read_text(encoding="utf-8"))
    assert report["date"] == "2026-11-05" and len(report["markets"]) == 1054
    assert all(utc(report["retrieved_at"]) <= utc(b["created_at"]) for b in original["games"])
    source_proofs = []
    for source in report["sources"]:
        assert sha_file(source["raw_path"]) == source["sha256"]
        if source.get("content_sha256"):
            assert digest(json.loads(Path(source["raw_path"]).read_bytes())) == source["content_sha256"]
        assert all(utc(source["retrieved_at"]) <= utc(b["created_at"]) for b in original["games"])
        source_proofs.append(source["sha256"])
    frozen_games = []
    for bundle in original["games"]:
        event = next(e for e in report["events"] if e.get("game_id") == bundle["game_id"])
        entry = context["games"][bundle["game_id"]]
        assert (entry["home_id"], entry["away_id"], utc(entry["tipoff"])) == (
            bundle["provenance"]["home_id"], bundle["provenance"]["away_id"], utc(bundle["tipoff"]))
        abbreviations = {t["ordering"]: t["abbreviation"].upper() for t in event["teams"]}
        game = {"id": bundle["game_id"], "tipoff": bundle["tipoff"], "phase": "pre", "status": "STATUS_SCHEDULED", "completed": False}
        for side in ("home", "away"):
            team_id = bundle["provenance"][f"{side}_id"]
            game[side] = {"id": team_id, "name": context["rosters"][team_id]["team_name"], "abbreviation": abbreviations[side]}
        frozen_games.append({"game": game, "as_of": bundle["created_at"], "original_bundle_id": bundle["id"],
            "original_bundle_hash": digest(bundle), "context_players": entry["players"],
            "original_family_sizes_and_statuses": [{"size": f["leg_count"], "status": f["status"], "selected_leg_ids": [l["id"] for l in f["legs"]]} for f in bundle["families"]]})
    fixture = {"scope": "FULL_FROZEN_PREGAME_PIPELINE", "replay_kind": "RETROSPECTIVE_POLICY_RESEARCH",
        "date": "2026-11-05", "original_target": {"path": str(TARGET.resolve()), "sha256": hashlib.sha256(target_raw).hexdigest()},
        "datasets": references, "history": history, "context": context,
        "context_source": {"path": str(context_path.resolve()), "sha256": context_hash},
        "polymarket_source": {"path": str(poly_path.resolve()), "sha256": POLY_HASH},
        "games": frozen_games, "outcomes_in_selection_inputs": False,
        "lineage_verification": {"dataset_hashes_verified": len(references), "merged_history_hash_matches_original": True,
            "context_hash_matches_original": True, "context_raw_sources_verified": len(context_proofs),
            "polymarket_original_raw_sources_verified": len(source_proofs),
            "quote_inventory_outcomes": len(report["markets"]), "history_games": len(history["games"]),
            "all_sources_observed_before_original_cutoffs": True},
        "limits": ["Full original quote inventory is restored, not just the previously qualified 35 candidates.",
            "Original missing/rejected Phoenix historical games are not repaired retroactively.",
            "Policy changes are applied after outcomes became known; this is not prospective validation or proof of improvement.",
            "No outcome, final box, current roster or fresh quote is read by selection replay."]}
    (OUT / "replay-input.json").write_text(canonical(fixture), encoding="utf-8")
    return fixture, report


def run(fixture, report, output_label="v2"):
    from nba_quant.aggressive import VERSION
    from nba_quant.construction import VERSION as CONSTRUCTION_VERSION, construct_game_drafts
    from nba_quant.family_optimizer import VERSION as OPTIMIZER_VERSION
    from nba_quant.polymarket_binding import bind_polymarket_offers
    assert VERSION == "aggressive-priced-research-0.2.0", "Expected the reviewed v0.2 research policy"
    # This method receives only the restored fixture and quote snapshot. It does
    # not read the result manifest or use actual game outcomes during selection.
    results = []
    for entry in fixture["games"]:
        offered = bind_polymarket_offers(report, entry["game"], entry["context_players"], as_of=entry["as_of"])
        result = construct_game_drafts(entry["game"], fixture["history"], entry["context_players"],
            offered_props=offered, as_of=entry["as_of"], selection_mode="aggressive")
        result.update(game_id=entry["game"]["id"], original_bundle_id=entry["original_bundle_id"],
            replay_as_of=entry["as_of"], bound_offered_rows=len(offered), replay_kind="RETROSPECTIVE_POLICY_RESEARCH")
        results.append(result)
    output = {"scope": fixture["scope"], "replay_kind": fixture["replay_kind"], "policy_version": VERSION,
        "construction_version": CONSTRUCTION_VERSION, "family_optimizer_version": OPTIMIZER_VERSION,
        "input_fixture_sha256": sha_file(OUT / "replay-input.json"), "polymarket_source_sha256": POLY_HASH,
        "original_target_sha256": fixture["original_target"]["sha256"], "games": results,
        "selection_inputs_exclude_outcomes": True, "current_data_or_quotes_refreshed": False,
        "summary": {"games": len(results), "bound_offered_rows": sum(r["bound_offered_rows"] for r in results),
            "family_status_counts": dict(Counter(f["status"] for r in results for f in r["families"])),
            "candidate_count": sum(len(r["candidates"]) for r in results)},
        "limits": fixture["limits"]}
    path = OUT / f"replay-{output_label}-selection.json"
    path.write_text(canonical(output), encoding="utf-8")
    (OUT / f"replay-{output_label}-selection.sha256").write_text(sha_file(path), encoding="ascii")
    print(json.dumps(output["summary"], indent=2))
    for result in results:
        print(result["game_id"], "offered", result["bound_offered_rows"], "candidates", len(result["candidates"]),
              [(f["leg_count"], f["status"], len(f["legs"])) for f in result["families"]])
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--output-label", default="v2", help="Use a new label to preserve prior replay artifacts")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9-]+", args.output_label):
        raise ValueError("Replay label must contain letters, numbers and hyphens only")
    fixture, report = prepare()
    if args.prepare_only:
        print(json.dumps(fixture["lineage_verification"], indent=2))
    else:
        run(fixture, report, args.output_label)


if __name__ == "__main__":
    main()
