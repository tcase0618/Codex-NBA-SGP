"""Complete the bounded observed current-season window without removing old datasets."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_quant.providers import BASE, acquire_history, archived_fetcher
from nba_quant.schemas import Dataset, utc
from nba_quant.store import Store, canonical, digest


def main():
    root = Path(__file__).resolve().parents[1]
    out = root / "output/authentic-history"
    out.mkdir(parents=True, exist_ok=True)
    manifests = []
    for start, end in [("2026-10-20", "2026-10-27"), ("2026-10-28", "2026-11-03")]:
        manifests.append(acquire_history(start, end, out, 100, archived_fetcher(out)))
    games, teams, players, rejected, source_proofs = {}, {}, {}, {}, {}
    for manifest in manifests:
        if manifest["games_not_requested"]:
            raise ValueError("Bounded window truncated; completeness cannot be reported")
        for source in manifest["sources"]:
            raw = Path(source["raw_path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != source["sha256"] or digest(json.loads(raw)) != source["content_sha256"]:
                raise ValueError("Acquisition source archive verification failed")
            source_proofs[(source["url"], source["sha256"])] = source
        for row in manifest["rejected"]:
            rejected[(row.get("stage"), row.get("game_id"), row.get("date"))] = row
        if manifest["dataset_path"] is None:
            continue
        dataset = Dataset.model_validate_json(Path(manifest["dataset_path"]).read_text(encoding="utf-8"))
        payload = dataset.model_dump(mode="json")
        if digest(payload) != manifest["dataset_sha256"]:
            raise ValueError("Window dataset hash mismatch")
        teams.update({t["id"]: t for t in payload["teams"]})
        players.update({p["id"]: p for p in payload["players"]})
        for game in payload["games"]:
            if game["id"] not in games or utc(game["known_at"]) > utc(games[game["id"]]["known_at"]):
                games[game["id"]] = game
    merged = Dataset.model_validate({"source": "ESPN NBA observed regular-season history, complete bounded acquisition window",
        "source_url": BASE, "retrieved_at": datetime.now(timezone.utc).isoformat(), "synthetic": False,
        "teams": sorted(teams.values(), key=lambda x: x["id"]),
        "players": sorted(players.values(), key=lambda x: x["id"]), "rotations": [],
        "games": sorted(games.values(), key=lambda x: (x["tipoff"], x["id"]))})
    dataset_hash = digest(merged.model_dump(mode="json"))
    path = out / f"dataset-{dataset_hash}.json"
    path.write_text(canonical(merged.model_dump(mode="json")), encoding="utf-8")
    store = Store(root / "data/nba_quant.sqlite")
    key = store.import_dataset(merged)
    actual = next(d for d in store.datasets() if d["id"] == key)
    if len(actual["games"]) != len(games) or actual["rotations"] or actual["synthetic"]:
        raise ValueError("Persisted dataset verification failed")
    receipt = {"status": "ACQUIRED_WITH_REJECTIONS" if rejected else "ACQUIRED",
        "requested_start": "2026-10-20", "requested_end": "2026-11-03",
        "dataset_id": key, "dataset_path": str(path.resolve()),
        "database_path": str((root / "data/nba_quant.sqlite").resolve()),
        "verified_at": datetime.now(timezone.utc).isoformat(), "synthetic": False, "rotations": 0,
        "games_discovered": sum(m["games_considered"] for m in manifests),
        "games_not_requested": sum(m["games_not_requested"] for m in manifests),
        "games_accepted": len(games), "teams_acquired": len(teams), "players_acquired": len(players),
        "coverage_start": min(g["tipoff"] for g in games.values()),
        "coverage_end": max(g["tipoff"] for g in games.values()),
        "rejected": list(rejected.values()), "source_archives_verified": len(source_proofs),
        "manifests": [m["manifest_path"] for m in manifests],
        "game_ids": sorted(games),
        "note": "Every discovered completed regular-season game in the bounded dates was requested; rejected source records remain excluded. Historical source known_at stays at actual retrieval. This is not complete accepted game coverage or historical pregame knowledge."}
    (out / "current-season-coverage.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({k: receipt[k] for k in ("status", "dataset_id", "games_discovered", "games_not_requested",
        "games_accepted", "teams_acquired", "players_acquired", "coverage_start", "coverage_end", "source_archives_verified")}, indent=2))


if __name__ == "__main__":
    main()
