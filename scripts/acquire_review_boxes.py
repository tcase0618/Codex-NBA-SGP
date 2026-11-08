"""Archive and normalize actual boxes for the exact frozen November 4 rescan."""
import hashlib
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_quant.providers import BASE, fetch_json, parse_espn_summary
from nba_quant.store import digest


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value):
        return None
    return int(value)


def minutes(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"\d+:\d{2}", value):
        m, s = map(int, value.split(":"))
        return m + s / 60 if s < 60 else None
    return number(value)


def pair(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+-\d+", value):
        return None, None
    return tuple(map(int, value.split("-")))


def normalize(payload, source, original):
    competitions = payload.get("header", {}).get("competitions", [])
    if len(competitions) != 1:
        raise ValueError("Expected one competition")
    comp = competitions[0]
    if str(payload["header"]["id"]) != original["id"] or str(comp["id"]) != original["id"]:
        raise ValueError("Source identity differs from exact frozen target")
    status = comp.get("status", {})
    state = status.get("type", {})
    teams = {str(c["team"]["id"]): {"id": str(c["team"]["id"]),
             "name": c["team"]["displayName"], "home_away": c["homeAway"], "score": number(c.get("score")),
             "line_scores": c.get("linescores", [])} for c in comp.get("competitors", [])}
    if len(teams) != 2 or {t["home_away"] for t in teams.values()} != {"home", "away"}:
        raise ValueError("Missing or duplicated home/away source teams")
    players, flags, seen = [], [], set()
    for section in payload.get("boxscore", {}).get("players", []):
        team_id = str(section.get("team", {}).get("id", ""))
        if team_id not in teams:
            flags.append("Player box section has an unknown team")
            continue
        for group in section.get("statistics", []):
            labels = group.get("labels", [])
            if "PTS" not in labels or "MIN" not in labels:
                continue
            for row in group.get("athletes", []):
                athlete = row.get("athlete", {})
                pid = str(athlete.get("id", ""))
                key = (team_id, pid)
                if key in seen:
                    flags.append(f"Duplicate player row {team_id}:{pid}")
                    continue
                seen.add(key)
                stats_list = row.get("stats", [])
                raw_stats = dict(zip(labels, stats_list)) if len(labels) == len(stats_list) else {}
                stats = {k: None for k in ("minutes", "pts", "reb", "oreb", "dreb", "ast", "fg3m", "fg3a", "fgm", "fga", "ftm", "fta", "stl", "blk", "tov", "pf")}
                row_flags = []
                if row.get("didNotPlay") is True:
                    row_status = "EXPLICIT_DNP"
                else:
                    for k, label in [("pts", "PTS"), ("reb", "REB"), ("oreb", "OREB"), ("dreb", "DREB"),
                        ("ast", "AST"), ("stl", "STL"), ("blk", "BLK"), ("tov", "TO"), ("pf", "PF")]:
                        stats[k] = number(raw_stats.get(label))
                    stats["minutes"] = minutes(raw_stats.get("MIN"))
                    for label, made, attempted in [("FG", "fgm", "fga"), ("3PT", "fg3m", "fg3a"), ("FT", "ftm", "fta")]:
                        stats[made], stats[attempted] = pair(raw_stats.get(label))
                    if any(value is None for value in stats.values()):
                        row_flags.append("Missing/unparseable source statistics; nulls were not converted to zero")
                    if all(stats[k] is not None for k in ("pts", "fgm", "fg3m", "ftm")) and stats["pts"] != 2 * stats["fgm"] + stats["fg3m"] + stats["ftm"]:
                        row_flags.append("Points do not reconcile to made field goals and free throws")
                    if all(stats[k] is not None for k in ("reb", "oreb", "dreb")) and stats["reb"] != stats["oreb"] + stats["dreb"]:
                        row_flags.append("Rebounds do not reconcile to offensive and defensive rebounds")
                    for made, attempted in [("fgm", "fga"), ("fg3m", "fg3a"), ("ftm", "fta")]:
                        if stats[made] is not None and stats[attempted] is not None and stats[made] > stats[attempted]:
                            row_flags.append(f"{made} exceeds {attempted}")
                    row_status = "FLAGGED" if row_flags else "PARSED"
                players.append({"id": pid, "name": athlete.get("displayName"), "team_id": team_id,
                    "team_name": teams[team_id]["name"], "position": athlete.get("position", {}).get("abbreviation"),
                    "did_not_play": row.get("didNotPlay") is True, "reason": row.get("reason"),
                    "starter": row.get("starter"), "ejected": row.get("ejected"), "stats": stats,
                    "row_status": row_status, "row_flags": row_flags, "raw_stats": raw_stats})
    for team_id, team in teams.items():
        rows = [p for p in players if p["team_id"] == team_id and not p["did_not_play"]]
        if not rows:
            flags.append(f"No participating rows for {team['name']}")
        elif all(p["stats"]["pts"] is not None for p in rows):
            player_points = sum(p["stats"]["pts"] for p in rows)
            team["player_points_sum"] = player_points
            team["points_reconcile"] = player_points == team["score"]
            if not team["points_reconcile"]:
                flags.append(f"Player points {player_points} differ from {team['name']} score {team['score']}")
    final_events = [p for p in payload.get("plays", []) if p.get("type", {}).get("text") == "End Game"]
    last_play = payload.get("plays", [])[-1] if payload.get("plays") else {}
    period = status.get("period", {}).get("number") if isinstance(status.get("period"), dict) else status.get("period")
    period = period or last_play.get("period", {}).get("number")
    clock = status.get("displayClock") or last_play.get("clock", {}).get("displayValue")
    canonical_status, canonical_error = "NOT_FINAL", None
    if state.get("completed") is True:
        try:
            parse_espn_summary(payload, source)
            canonical_status = "VALIDATED_FINAL"
        except ValueError as exc:
            canonical_status, canonical_error = "FINAL_WITH_VALIDATION_FLAGS", str(exc)
            flags.append("Canonical completed-game validation rejected this source snapshot; normalized rows retained for review")
    return {"id": original["id"], "game": original["game"], "target_tipoff": original["tipoff"],
        "source_tipoff": comp.get("date"), "status": state.get("name"),
        "status_detail": state.get("detail") or state.get("description"), "completed": state.get("completed") is True,
        "period": period, "clock": clock, "teams": teams, "players": players,
        "canonical_status": canonical_status, "canonical_error": canonical_error, "flags": flags,
        "end_game_event": final_events[-1] if final_events else None,
        "source_url": f"https://www.espn.com/nba/game/_/gameId/{original['id']}", "source": source}


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / "output/tonight-parlays-rescan-2026-11-04.json"
    out = root / "output/review-2026-11-04"
    original_bytes = target.read_bytes()
    original = json.loads(original_bytes)
    if len(original["games"]) != 14 or len({g["id"] for g in original["games"]}) != 14:
        raise ValueError("Expected the exact fourteen unique frozen rescan targets")
    out.mkdir(parents=True, exist_ok=True)
    acquired, failures = {}, []
    def fetch(game):
        payload, source = fetch_json(f"{BASE}/summary?event={game['id']}", out)
        raw = Path(source["raw_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"] or digest(json.loads(raw)) != source["content_sha256"]:
            raise ValueError("Persisted source hash verification failed")
        return normalize(payload, source, game)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(fetch, game): game["id"] for game in original["games"]}
        for future in as_completed(pending):
            event_id = pending[future]
            try:
                acquired[event_id] = future.result()
            except Exception as exc:
                failures.append({"game_id": event_id, "reason": str(exc)})
    result = {"date": "2026-11-04", "acquired_at": datetime.now(timezone.utc).isoformat(),
        "review_target_path": str(target.resolve()), "review_target_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "review_target_meta": original["meta"], "games": {g["id"]: acquired[g["id"]] for g in original["games"] if g["id"] in acquired},
        "failures": failures, "games_requested": 14, "games_acquired": len(acquired),
        "games_final": sum(g["completed"] for g in acquired.values()),
        "canonical_status_counts": dict(Counter(g["canonical_status"] for g in acquired.values())),
        "note": "Read-only actual result acquisition for exact frozen targets. Unavailable/unparseable stats remain null. Explicit DNP retained separately, not silently zero. Provider rows retained with validation flags. No sportsbook settlement rule or P&L inference."}
    path = out / "manifest.json"
    raw = json.dumps(result, indent=2).encode()
    path.write_bytes(raw)
    (out / "manifest.sha256").write_text(hashlib.sha256(raw).hexdigest(), encoding="ascii")
    print(json.dumps({k: result[k] for k in ("games_requested", "games_acquired", "games_final", "canonical_status_counts", "failures")}, indent=2))
    for game in acquired.values():
        print(game["id"],game["game"],game["status"],game["period"],game["clock"],[(t["name"],t["score"]) for t in game["teams"].values()],game["canonical_status"])


if __name__ == "__main__":
    main()
