"""Slot scan: acquire current-season ESPN history (first run), then build 3/5/8 families for target home teams.
Usage: .venv/bin/python scripts/slot_scan.py "ATL,CHA,Atlanta Hawks,..." 1900ET"""
import json, sys, time
from pathlib import Path
from datetime import datetime, timezone
ROOT = Path("/home/user/Codex-NBA-SGP"); sys.path.insert(0, str(ROOT))
from nba_quant.providers import BASE, acquire_history
from nba_quant.schemas import Dataset, utc
from nba_quant.store import Store, canonical, digest
from nba_quant.slate import load_slate
from nba_quant.context import refresh_context
from nba_quant.polymarket import refresh_polymarket_odds
from nba_quant.board import merged_history, build_game

TARGETS = set(sys.argv[1].split(","))  # e.g. "ATL,CHA,DET,TOR" (home abbreviations)
out = ROOT / "output/authentic-history"; out.mkdir(parents=True, exist_ok=True)
store = Store(ROOT / "data/nba_quant.sqlite")
if not [d for d in store.datasets() if not d["synthetic"]]:
    games, teams, players = {}, {}, {}
    for s, e in [("2026-10-20", "2026-10-27"), ("2026-10-28", "2026-11-03"), ("2026-11-04", "2026-11-08")]:
        t = time.time(); m = acquire_history(s, e, out, 100)
        print(json.dumps({"window": [s, e], "considered": m["games_considered"], "not_requested": m["games_not_requested"],
                          "accepted": len(m["accepted"]), "rejected": len(m["rejected"]), "sec": round(time.time() - t)}), flush=True)
        if not m["dataset_path"]: continue
        p = Dataset.model_validate_json(Path(m["dataset_path"]).read_text()).model_dump(mode="json")
        teams.update({x["id"]: x for x in p["teams"]}); players.update({x["id"]: x for x in p["players"]})
        games.update({g["id"]: g for g in p["games"]})
    merged = Dataset.model_validate({"source": "ESPN NBA observed regular-season history 2026-10-20..2026-11-08",
        "source_url": BASE, "retrieved_at": datetime.now(timezone.utc).isoformat(), "synthetic": False,
        "teams": sorted(teams.values(), key=lambda x: x["id"]), "players": sorted(players.values(), key=lambda x: x["id"]),
        "rotations": [], "games": sorted(games.values(), key=lambda x: (x["tipoff"], x["id"]))})
    print("imported dataset", store.import_dataset(merged), len(games), "games", flush=True)
slate = load_slate(refresh=True); context = refresh_context(slate)
print(json.dumps({"slate": slate["date"], "games": len(slate["games"]), "context": context["status"]}), flush=True)
quotes = refresh_polymarket_odds(slate=slate)
print(json.dumps({"polymarket": quotes["status"], "coverage": quotes["coverage"]}), flush=True)
history = merged_history(store); built, errors = [], []
for g in slate["games"]:
    if g["home"].get("abbreviation", g["home"].get("abbr")) not in TARGETS and g["home"]["name"] not in TARGETS: continue
    try: built.append(build_game(store, g, context, history, selection_mode="aggressive"))
    except ValueError as e: errors.append({"game": g["away"]["name"] + " @ " + g["home"]["name"], "reason": str(e)})
rep = {"date": slate["date"], "created_at": datetime.now(timezone.utc).isoformat(), "games": built, "errors": errors,
       "polymarket": {"status": quotes["status"], "coverage": quotes["coverage"]}}
dst = ROOT / f"output/nightly-priority/slot-{slate['date']}-{sys.argv[2]}.json"; dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(rep, indent=2, default=str)); print("SAVED", dst, "errors", errors, flush=True)
