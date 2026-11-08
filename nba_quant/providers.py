"""Read-only ESPN NBA history adapter with byte-level source archives.

ESPN is the provider, not the NBA's official statistical feed. No historical
availability or pregame knowledge is inferred from completed-game participation.
"""
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .schemas import Dataset, Game, utc
from .store import canonical, digest

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
MAX_BYTES = 20_000_000
REQUIRED_LABELS = {"MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF"}
POSITIONS = {p: p for p in ("PG", "SG", "SF", "PF", "C", "G", "F")}


def fetch_json(url, archive_dir):
    """Archive exact response bytes before parsing or accepting any record."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "site.api.espn.com" or not parsed.path.startswith("/apis/site/v2/sports/basketball/nba/"):
        raise ValueError("Only the fixed HTTPS ESPN NBA endpoint is supported")
    with urlopen(Request(url, headers={"User-Agent": "CaseCapitalNBAQuant/0.1 (research)"}), timeout=30) as response:
        if urlparse(response.geturl()).netloc != "site.api.espn.com":
            raise ValueError("Unexpected provider redirect")
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Provider response exceeds bounded read limit")
    retrieved_at = datetime.now(timezone.utc).isoformat()
    sha = hashlib.sha256(raw).hexdigest()
    path = Path(archive_dir) / "raw" / f"{sha}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError("Existing raw archive content hash mismatch")
    else:
        with path.open("xb") as stream:
            stream.write(raw)
    payload = json.loads(raw)
    provenance = {"provider": "ESPN", "provider_class": "secondary_statistics_provider",
                  "url": url, "retrieved_at": retrieved_at, "sha256": sha,
                  "content_sha256": digest(payload), "raw_path": str(path.resolve())}
    return payload, provenance


def archived_fetcher(directory):
    """Reuse verified original responses with their original observation times.

    Opt-in for historical acquisition only. This never refreshes archived URLs
    and must not be used to assert current schedules, injuries or availability.
    """
    sources = {}
    for path in sorted(Path(directory).glob("manifest-*.json")):
        for source in json.loads(path.read_text(encoding="utf-8")).get("sources", []):
            if source["url"] not in sources or utc(source["retrieved_at"]) > utc(sources[source["url"]]["retrieved_at"]):
                sources[source["url"]] = source

    def fetch(url, archive_dir):
        if url not in sources:
            return fetch_json(url, archive_dir)
        source = sources[url]
        raw = Path(source["raw_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("Historical raw archive SHA256 mismatch")
        payload = json.loads(raw)
        if digest(payload) != source["content_sha256"]:
            raise ValueError("Historical raw archive content hash mismatch")
        return payload, {**source, "reused_archive": True}

    return fetch


def _integer(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value):
        raise ValueError(f"Missing or invalid integer statistic: {value!r}")
    return int(value)


def _minutes(value):
    if isinstance(value, str) and re.fullmatch(r"\d+:\d{2}", value):
        minutes, seconds = value.split(":")
        if int(seconds) >= 60:
            raise ValueError("Invalid minute:second statistic")
        return int(minutes) + int(seconds) / 60
    # ESPN often reports whole-minute values. Preserve them exactly; never make
    # player-level corrections to force a game's minutes to reconcile.
    return float(_integer(value))


def _made_attempted(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+-\d+", value):
        raise ValueError("Missing or invalid made-attempted statistic")
    return tuple(int(x) for x in value.split("-"))


def _provider_id(value):
    value = str(value)
    if not re.fullmatch(r"\d+", value):
        raise ValueError("Missing or invalid numeric ESPN identity")
    return value


def parse_espn_summary(payload, provenance):
    """Return (canonical game, teams, players, notes), or reject the whole game.

    A source End Game event with wallclock is mandatory. Integer player minutes
    retain their source resolution and uncertainty; no values are adjusted.
    """
    try:
        if provenance.get("provider") != "ESPN" or provenance.get("content_sha256") != digest(payload):
            raise ValueError("Summary provenance content hash mismatch")
        retrieved_at = utc(provenance["retrieved_at"])
        competitions = payload["header"]["competitions"]
        if len(competitions) != 1:
            raise ValueError("Expected one NBA competition")
        competition = competitions[0]
        status = competition["status"]["type"]
        if status.get("name") != "STATUS_FINAL" or status.get("completed") is not True:
            raise ValueError("Only completed STATUS_FINAL NBA games are accepted")
        game_id = _provider_id(payload["header"]["id"])
        if _provider_id(competition["id"]) != game_id:
            raise ValueError("Competition identity differs from event")
        competitors = competition["competitors"]
        if len(competitors) != 2 or {c["homeAway"] for c in competitors} != {"home", "away"}:
            raise ValueError("Expected distinct home and away teams")
        home, away = (next(c for c in competitors if c["homeAway"] == side) for side in ("home", "away"))
        team_ids = {_provider_id(c["team"]["id"]) for c in competitors}
        if len(team_ids) != 2:
            raise ValueError("Duplicated competitor team")
        finals = [p for p in payload.get("plays", []) if p.get("type", {}).get("text") == "End Game"]
        if not finals or not finals[-1].get("wallclock"):
            raise ValueError("Missing source End Game wallclock; completion time will not be fabricated")
        end = finals[-1]
        period = int(end["period"]["number"])
        if period < 4 or period > 24:
            raise ValueError("Invalid final regulation/overtime period")
        if (end["homeScore"], end["awayScore"]) != (_integer(home["score"]), _integer(away["score"])):
            raise ValueError("Final event score differs from header score")
        for c in competitors:
            lines = c.get("linescores", [])
            if len(lines) != period or sum(_integer(x["displayValue"]) for x in lines) != _integer(c["score"]):
                raise ValueError("Quarter/overtime scores do not reconcile")
        player_sections = payload["boxscore"]["players"]
        if len(player_sections) != 2 or {_provider_id(t["team"]["id"]) for t in player_sections} != team_ids:
            raise ValueError("Missing or duplicate team player-box sections")
        players, boxes, notes = {}, [], {"provider_positions": {}, "minutes_precision": {},
                                         "season_year": payload["header"]["season"]["year"],
                                         "season_type": payload["header"]["season"]["type"],
                                         "completion_evidence": {"play_id": str(end.get("id", "")),
                                                                 "wallclock": end["wallclock"], "period": period}}
        for section in player_sections:
            team_id = _provider_id(section["team"]["id"])
            groups = [g for g in section["statistics"] if REQUIRED_LABELS <= set(g.get("labels", []))]
            if len(groups) != 1:
                raise ValueError("Expected exactly one complete player-stat group per team")
            group = groups[0]
            labels = group["labels"]
            if len(labels) != len(set(labels)):
                raise ValueError("Duplicate player-stat labels")
            for row in group["athletes"]:
                athlete = row["athlete"]
                pid = _provider_id(athlete["id"])
                if pid in players:
                    raise ValueError("Duplicate athlete row across teams")
                provider_position = athlete.get("position", {}).get("abbreviation", "")
                position = POSITIONS.get(provider_position, "UNKNOWN")
                players[pid] = {"id": pid, "name": athlete["displayName"], "position": position,
                                "archetypes": []}
                notes["provider_positions"][pid] = provider_position
                if row.get("didNotPlay") is True:
                    # Explicit DNP is recorded as zero; missing player rows remain missing.
                    boxes.append({"player_id": pid, "team_id": team_id, **{k: 0 for k in (
                        "minutes", "pts", "fga", "fgm", "fg3a", "fg3m", "fta", "ftm", "oreb", "dreb", "ast", "stl", "blk", "tov", "pf")}})
                    continue
                values = row["stats"]
                if len(values) != len(labels):
                    raise ValueError("Incomplete player statistic row")
                stats = dict(zip(labels, values))
                fgm, fga = _made_attempted(stats["FG"])
                fg3m, fg3a = _made_attempted(stats["3PT"])
                ftm, fta = _made_attempted(stats["FT"])
                b = {"player_id": pid, "team_id": team_id, "minutes": _minutes(stats["MIN"]),
                     "pts": _integer(stats["PTS"]), "fgm": fgm, "fga": fga, "fg3m": fg3m,
                     "fg3a": fg3a, "ftm": ftm, "fta": fta,
                     **{k: _integer(stats[label]) for k, label in (
                         ("oreb", "OREB"), ("dreb", "DREB"), ("ast", "AST"), ("stl", "STL"),
                         ("blk", "BLK"), ("tov", "TO"), ("pf", "PF"))}}
                if b["oreb"] + b["dreb"] != _integer(stats["REB"]):
                    raise ValueError("Player rebounds do not reconcile")
                notes["minutes_precision"][pid] = "seconds" if ":" in stats["MIN"] else "whole_minutes_as_reported"
                boxes.append(b)
        game = Game.model_validate({"id": game_id, "home_id": str(home["team"]["id"]),
                "away_id": str(away["team"]["id"]), "tipoff": competition["date"],
                "completed_at": end["wallclock"], "known_at": retrieved_at,
                "minutes_precision_seconds": 60 if notes["minutes_precision"] and all(
                    value == "whole_minutes_as_reported" for value in notes["minutes_precision"].values()) else 0,
                "home_score": _integer(home["score"]), "away_score": _integer(away["score"]),
                "overtime_periods": period - 4, "boxes": boxes}).model_dump(mode="json")
        expected_minutes = 240 + 25 * game["overtime_periods"]
        notes["team_minutes"] = {
            tid: {"source_total": sum(b["minutes"] for b in boxes if b["team_id"] == tid),
                  "regulation_plus_overtime_total": expected_minutes,
                  "source_minus_game_total": sum(b["minutes"] for b in boxes if b["team_id"] == tid) - expected_minutes,
                  "values_adjusted": False, "source_resolution_seconds": game["minutes_precision_seconds"]}
            for tid in sorted(team_ids)}
        teams = [{"id": str(c["team"]["id"]), "name": c["team"]["displayName"]} for c in competitors]
        return game, teams, list(players.values()), notes
    except (KeyError, IndexError, TypeError, AttributeError, StopIteration) as exc:
        raise ValueError(f"Malformed ESPN NBA final summary: {type(exc).__name__}") from exc


def acquire_history(start_date, end_date, output_dir, max_games=30, fetcher=fetch_json):
    start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if end < start or (end - start).days > 30 or not 1 <= max_games <= 100:
        raise ValueError("Use an ordered date range of at most 31 days and max_games between 1 and 100")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"provider": "ESPN", "provider_class": "secondary_statistics_provider",
                "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                "season_scope": "regular_season_only (ESPN season type 2)",
                "sampling_policy": "latest discovered completed games within requested dates, bounded by max_games",
                "started_at": datetime.now(timezone.utc).isoformat(), "max_games": max_games,
                "sources": [], "accepted": [], "rejected": [], "scoreboard_days": [],
                "dataset_path": None,
                "limitations": ["Provider responses are authentic acquired ESPN data, not an official NBA statistical feed.",
                                "known_at is actual summary retrieval time; historical pregame point-in-time knowledge is not established.",
                                "No rotations, current availability, injury status or betting odds are inferred from participation.",
                                "Whole-minute provider values are preserved; minute totals carry one resolution-minute uncertainty per participant. Values are never rescaled."]}
    event_ids = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        url = f"{BASE}/scoreboard?dates={day:%Y%m%d}&limit=100"
        try:
            board, provenance = fetcher(url, directory)
            manifest["sources"].append(provenance)
            events = board.get("events", [])
            final_ids = [str(e["id"]) for e in events if e.get("status", {}).get("type", {}).get("completed") is True]
            manifest["scoreboard_days"].append({"date": day.isoformat(), "events_returned": len(events),
                                                 "completed_games": len(final_ids)})
            event_ids.extend(final_ids)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            manifest["rejected"].append({"stage": "scoreboard", "date": day.isoformat(), "reason": str(exc)})
    games, teams, players = [], {}, {}
    for event_id in list(dict.fromkeys(event_ids))[-max_games:]:
        provenance = None
        try:
            event_id = _provider_id(event_id)
            payload, provenance = fetcher(f"{BASE}/summary?event={event_id}", directory)
            manifest["sources"].append(provenance)
            if payload.get("header", {}).get("season", {}).get("type") != 2:
                raise ValueError("Only explicitly identified regular-season games (season type 2) are accepted")
            game, ts, ps, notes = parse_espn_summary(payload, provenance)
            if game["id"] != event_id:
                raise ValueError("Response event ID differs from requested event")
            games.append(game)
            teams.update({t["id"]: t for t in ts})
            players.update({p["id"]: p for p in ps})
            manifest["accepted"].append({"game_id": event_id, "tipoff": game["tipoff"],
                                         "source_sha256": provenance["sha256"], "notes": notes})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            manifest["rejected"].append({"stage": "summary", "game_id": event_id,
                                         "reason": str(exc), "source": provenance})
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    if games:
        dataset = Dataset.model_validate({"source": "ESPN NBA completed-game box scores (observed at retrieval)",
                    "source_url": BASE, "retrieved_at": manifest["finished_at"], "synthetic": False,
                    "teams": sorted(teams.values(), key=lambda t: t["id"]),
                    "players": sorted(players.values(), key=lambda p: p["id"]),
                    "rotations": [], "games": games}).model_dump(mode="json")
        key = digest(dataset)
        dataset_path = directory / f"dataset-{key}.json"
        dataset_path.write_text(canonical(dataset), encoding="utf-8")
        manifest.update(dataset_path=str(dataset_path.resolve()), dataset_sha256=key,
                        coverage_start=min(g["tipoff"] for g in games), coverage_end=max(g["tipoff"] for g in games))
    manifest.update(status="ACQUIRED" if games else "NO_ACCEPTED_GAMES", games_accepted=len(games),
                    teams_acquired=len(teams), players_acquired=len(players),
                    games_considered=min(len(set(event_ids)), max_games),
                    games_not_requested=max(0, len(set(event_ids)) - max_games))
    run_hash = digest(manifest)
    path = directory / f"manifest-{run_hash}.json"
    manifest["manifest_path"] = str(path.resolve())
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
