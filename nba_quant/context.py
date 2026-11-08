"""Current roster membership and provider-reported injuries, never inferred health.

An ESPN roster status of Active is a contractual/roster field and does not
establish game participation. Unlisted injuries remain UNVERIFIED availability.
"""
import hashlib
import json
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .providers import BASE, POSITIONS, fetch_json
from .schemas import utc
from .store import canonical, digest

EASTERN = ZoneInfo("America/New_York")
MAX_AGE_SECONDS = 3600
_refresh_lock = threading.Lock()
EXPLICIT_AVAILABILITY = {"out": "OUT", "questionable": "QUESTIONABLE", "doubtful": "DOUBTFUL", "available": "AVAILABLE"}


def _now():
    return datetime.now(timezone.utc)


def _verify_source(source):
    if source.get("provider") != "ESPN":
        raise ValueError("Unexpected context provider")
    observed = utc(source["retrieved_at"])
    if observed > _now():
        raise ValueError("Context observation timestamp cannot be in the future")
    raw = Path(source["raw_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("Roster source archive SHA256 mismatch")
    payload = json.loads(raw)
    if digest(payload) != source["content_sha256"]:
        raise ValueError("Roster source archive content hash mismatch")
    return payload


def parse_roster(payload, source, team_id):
    """Parse structured observed membership; no active probabilities or minutes."""
    try:
        if not isinstance(team_id, str) or not team_id.isdigit():
            raise ValueError("Expected numeric ESPN team identity")
        if source.get("provider") != "ESPN" or source.get("url") != f"{BASE}/teams/{team_id}/roster":
            raise ValueError("Roster provider URL does not match requested team")
        observed = utc(source["retrieved_at"])
        if observed > _now() or digest(payload) != source.get("content_sha256"):
            raise ValueError("Roster timestamp or content hash is invalid")
        if payload.get("status") != "success" or str(payload["team"]["id"]) != team_id:
            raise ValueError("Roster response status/team identity mismatch")
        if payload.get("timestamp") and utc(payload["timestamp"]) > observed:
            raise ValueError("Provider roster timestamp follows retrieval")
        athletes = payload["athletes"]
        if not isinstance(athletes, list) or not athletes:
            raise ValueError("Roster contains no athlete membership records")
        players, seen, errors = [], set(), []
        for athlete in athletes:
            pid = athlete["id"]
            name = athlete["displayName"]
            if not isinstance(pid, str) or not pid.isdigit() or not isinstance(name, str) or not name.strip():
                raise ValueError("Invalid roster athlete identity/name")
            if pid in seen:
                raise ValueError("Duplicate athlete identity within roster")
            seen.add(pid)
            position = athlete.get("position", {}).get("abbreviation", "")
            roster_status = athlete.get("status", {}).get("name", "UNREPORTED")
            injuries = athlete.get("injuries", [])
            if not isinstance(injuries, list):
                raise ValueError("Malformed injury records")
            parsed_injuries = []
            injury_errors = []
            for injury in injuries:
                reported = injury.get("date")
                status = injury.get("status")
                if not isinstance(status, str) or not status.strip():
                    injury_errors.append("Injury entry has no explicit status")
                    continue
                if reported:
                    try:
                        injury_time = utc(reported)
                        if injury_time > observed:
                            injury_errors.append("Injury entry timestamp is in the future")
                            continue
                    except (ValueError, TypeError, AttributeError):
                        injury_errors.append("Injury entry has an invalid timestamp")
                        continue
                description = injury.get("longComment") or injury.get("shortComment")
                details = injury.get("details", {})
                if not description and isinstance(details, dict):
                    description = " ".join(str(details.get(k, "")) for k in ("type", "location", "side", "detail")).strip() or None
                parsed_injuries.append({"status": status, "reported_at": reported,
                                        "description": description, "source": source})
            dated = [i for i in parsed_injuries if i["reported_at"]]
            undated = [i for i in parsed_injuries if not i["reported_at"]]
            if dated:
                latest_time = max(utc(i["reported_at"]) for i in dated)
                candidates = [i for i in dated if utc(i["reported_at"]) == latest_time] + undated
            else:
                candidates = undated
            statuses = {i["status"].strip().lower() for i in candidates}
            conflicting = len(statuses) > 1
            injury_status = candidates[0]["status"] if len(statuses) == 1 else "CONFLICTING" if statuses else "UNREPORTED"
            availability = EXPLICIT_AVAILABILITY.get(injury_status.strip().lower(), "UNVERIFIED")
            if conflicting or injury_errors:
                availability = "UNVERIFIED"
                errors.append({"team_id": team_id, "player_id": pid,
                               "reason": "Conflicting injury statuses" if conflicting else "; ".join(injury_errors)})
            players.append({"id": pid, "name": name, "team_id": team_id,
                "position": POSITIONS.get(position, "UNKNOWN"), "source_position": position,
                "roster_status": roster_status, "membership_observed_at": observed.isoformat(),
                "injury_status": injury_status, "injury_description": next((i["description"] for i in candidates if i["description"]), None),
                "injury_reported_at": candidates[0]["reported_at"] if candidates and not conflicting else None,
                "injury_records": parsed_injuries, "availability": availability,
                "availability_basis": "explicit_provider_injury_status" if availability != "UNVERIFIED" else "not_established",
                "game_active_status": "UNVERIFIED", "source": source})
        return {"team_id": team_id, "team_name": payload["team"]["displayName"],
                "season": payload.get("season"), "provider_timestamp": payload.get("timestamp"),
                "players": players, "source": source, "errors": errors}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Malformed ESPN roster response: {type(exc).__name__}") from exc


def refresh_context(slate, output_dir="output/context", fetcher=fetch_json):
    day = date.fromisoformat(slate["date"])
    if day != _now().astimezone(EASTERN).date():
        raise ValueError("Current roster endpoint cannot establish historical or future game-day membership")
    if slate.get("status") != "AVAILABLE" or slate.get("stale"):
        raise ValueError("A fresh AVAILABLE source slate is required for current context acquisition")
    teams = {g[side]["id"] for g in slate["games"] for side in ("home", "away")}
    if not teams or len(teams) > 30 or len(slate["games"]) > 15:
        raise ValueError("Context acquisition requires 1-15 games and at most 30 distinct NBA teams")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with _refresh_lock:
        def fetch_team(team_id):
            payload, source = fetcher(f"{BASE}/teams/{team_id}/roster", directory)
            if digest(_verify_source(source)) != digest(payload):
                raise ValueError("Roster fetch differs from persisted source bytes")
            return parse_roster(payload, source, team_id)

        rosters, errors = {}, []
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {pool.submit(fetch_team, team_id): team_id for team_id in sorted(teams)}
            for future in as_completed(pending):
                team_id = pending[future]
                try:
                    rosters[team_id] = future.result()
                    errors.extend(rosters[team_id]["errors"])
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append({"team_id": team_id, "reason": str(exc)})
        memberships = defaultdict(list)
        for team_id, roster in rosters.items():
            for player in roster["players"]:
                memberships[player["id"]].append(team_id)
        conflicts = [{"player_id": pid, "team_ids": sorted(ids), "reason": "Athlete appears on multiple observed team rosters"}
                     for pid, ids in sorted(memberships.items()) if len(ids) > 1]
        for conflict in conflicts:
            for team_id in conflict["team_ids"]:
                for player in rosters[team_id]["players"]:
                    if player["id"] == conflict["player_id"]:
                        player.update(availability="UNVERIFIED", availability_basis="conflicting_membership",
                                      roster_status="CONFLICTING_MEMBERSHIP")
        mapped_games = {}
        for game in slate["games"]:
            ids = [game[side]["id"] for side in ("home", "away")]
            missing = [tid for tid in ids if tid not in rosters]
            mapped_games[game["id"]] = {"id": game["id"], "tipoff": game["tipoff"],
                "home_id": ids[0], "away_id": ids[1], "status": "AVAILABLE" if not missing else "PARTIAL",
                "missing_team_ids": missing,
                "players": [player for tid in ids if tid in rosters for player in rosters[tid]["players"]]}
        sources = [rosters[tid]["source"] for tid in sorted(rosters)]
        all_players = [p for roster in rosters.values() for p in roster["players"]]
        context = {"date": day.isoformat(), "timezone": "America/New_York",
            "status": "AVAILABLE" if len(rosters) == len(teams) and not conflicts else "PARTIAL" if rosters else "UNAVAILABLE",
            "retrieved_at": _now().isoformat(), "games": mapped_games,
            "rosters": {tid: rosters[tid] for tid in sorted(rosters)},
            "sources": sources, "errors": errors, "membership_conflicts": conflicts,
            "cross_source_conflicts": [], "cross_source_comparison": "NOT_PERFORMED; official PDF identity mapping is unreviewed",
            "counts": {"games": len(mapped_games), "teams_requested": len(teams), "teams_acquired": len(rosters),
                       "players": len(all_players), "unique_players": len(memberships),
                       "explicit_injury_statuses": sum(p["injury_status"] != "UNREPORTED" for p in all_players),
                       "availability": dict(Counter(p["availability"] for p in all_players))},
            "slate_source_sha256": slate.get("source_sha256"),
            "limitations": ["Current ESPN roster membership is observed at retrieval; roster Active does not establish game participation.",
                "Empty or unreported injuries are UNVERIFIED availability. No player active probabilities or minutes restrictions are inferred.",
                "Day-To-Day and Probable remain UNVERIFIED unless an explicit supported game availability status is supplied.",
                "Provider injury status is not a confirmed lineup; older injury reports and official NBA PDF identity mapping require review.",
                "ESPN is a secondary provider. Cross-source injury reconciliation against the official PDF has not been performed."]}
        return _publish(context, directory)


def _publish(context, directory):
    key = digest(context)
    raw = canonical(context).encode()
    path = directory / f"context-{key}.json"
    if not path.exists():
        with path.open("xb") as stream:
            stream.write(raw)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != key:
        raise ValueError("Existing context snapshot hash mismatch")
    receipt = {"hash": key, "path": str(path.resolve()), "date": context["date"],
               "retrieved_at": context["retrieved_at"]}
    pointer = directory / f"latest-{context['date']}.json"
    temp = pointer.with_suffix(".tmp")
    temp.write_text(canonical(receipt), encoding="utf-8")
    temp.replace(pointer)
    return {**context, "hash": key, "snapshot_path": str(path.resolve()),
            "age_seconds": _age(context), "stale": _age(context) > MAX_AGE_SECONDS,
            "cache_max_age_seconds": MAX_AGE_SECONDS}


def _age(context):
    times = [utc(s["retrieved_at"]) for s in context["sources"]]
    return round((_now() - min(times or [utc(context["retrieved_at"])] )).total_seconds(), 3)


def load_context(output_dir="output/context", day=None, max_age_seconds=MAX_AGE_SECONDS):
    selected_day = _now().astimezone(EASTERN).date() if day is None else date.fromisoformat(str(day))
    pointer = Path(output_dir) / f"latest-{selected_day.isoformat()}.json"
    base = {"date": selected_day.isoformat(), "timezone": "America/New_York", "status": "UNAVAILABLE",
            "games": {}, "sources": [], "counts": {}, "errors": [], "retrieved_at": None,
            "stale": False, "age_seconds": None, "cache_max_age_seconds": max_age_seconds}
    if not pointer.exists():
        return {**base, "reason": "No observed current roster context has been acquired"}
    try:
        receipt = json.loads(pointer.read_text(encoding="utf-8"))
        raw = Path(receipt["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["hash"]:
            raise ValueError("Context snapshot SHA256 mismatch")
        context = json.loads(raw)
        if context["date"] != selected_day.isoformat() or utc(context["retrieved_at"]) > _now():
            raise ValueError("Context date or observation timestamp mismatch")
        for source in context["sources"]:
            _verify_source(source)
        age = _age(context)
        result = {**context, "hash": receipt["hash"], "snapshot_path": receipt["path"],
                  "age_seconds": age, "stale": age > max_age_seconds,
                  "cache_max_age_seconds": max_age_seconds}
        if age > max_age_seconds:
            return {**result, "status": "LAST_GOOD", "last_good_status": context["status"],
                    "reason": "Current roster context exceeds its source observation age limit"}
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**base, "reason": f"Current context failed persisted verification: {exc}"}
