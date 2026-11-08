"""Current ESPN NBA schedule with verified archives and explicit cache age."""
import hashlib
import json
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .providers import BASE, fetch_json
from .schemas import utc
from .store import canonical, digest

EASTERN = ZoneInfo("America/New_York")
TTL_SECONDS = 300
_locks = {}
_locks_guard = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


def _lock_for(directory, day):
    key = (str(directory.resolve()), day.isoformat())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _numeric_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value):
        raise ValueError("Missing or invalid ESPN schedule identity")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid schedule {label}")
    return value


def _parse(payload, provenance, day, url):
    try:
        if provenance.get("provider") != "ESPN" or provenance.get("url") != url:
            raise ValueError("Schedule provider or URL mismatch")
        if provenance.get("content_sha256") != digest(payload):
            raise ValueError("Schedule content hash mismatch")
        observed = utc(provenance["retrieved_at"])
        if observed > _now():
            raise ValueError("Schedule retrieval timestamp is in the future")
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError("Provider response has no events list")
        games, seen = [], set()
        for event in events:
            event_id = _numeric_id(event["id"])
            if event_id in seen:
                raise ValueError("Duplicate provider schedule event")
            seen.add(event_id)
            tipoff = utc(event["date"])
            competitions = event["competitions"]
            if len(competitions) != 1:
                raise ValueError("Expected one schedule competition")
            competition = competitions[0]
            if _numeric_id(competition["id"]) != event_id:
                raise ValueError("Schedule competition/event identity mismatch")
            if utc(competition["date"]) != tipoff:
                raise ValueError("Schedule event and competition tipoff mismatch")
            competitors = competition["competitors"]
            if len(competitors) != 2 or {c["homeAway"] for c in competitors} != {"home", "away"}:
                raise ValueError("Schedule requires distinct home and away competitors")
            mapped = {}
            for competitor in competitors:
                team = competitor["team"]
                mapped[competitor["homeAway"]] = {"id": _numeric_id(team["id"]),
                    "name": _text(team["displayName"], "team name"),
                    "abbreviation": _text(team["abbreviation"], "team abbreviation")}
            if mapped["home"]["id"] == mapped["away"]["id"]:
                raise ValueError("Schedule has the same team on both sides")
            status = event["status"]["type"]
            if not isinstance(status.get("completed"), bool):
                raise ValueError("Schedule completion status must be boolean")
            status_name = _text(status["name"], "status")
            phase = status.get("state")
            if phase not in {"pre", "in", "post"}:
                phase = {"STATUS_SCHEDULED": "pre", "STATUS_IN_PROGRESS": "in",
                         "STATUS_HALFTIME": "in", "STATUS_END_PERIOD": "in",
                         "STATUS_FINAL": "post"}.get(status_name, "unknown")
            status_detail = _text(status.get("detail") or status.get("description"), "status detail")
            if tipoff.astimezone(EASTERN).date() != day:
                continue
            games.append({"id": event_id, **mapped, "tipoff": tipoff.isoformat(),
                          "status": status_name, "phase": phase, "status_detail": status_detail,
                          "completed": status["completed"],
                          "source_url": f"https://www.espn.com/nba/game/_/gameId/{event_id}"})
        games.sort(key=lambda game: (game["tipoff"], game["id"]))
        result = {"date": day.isoformat(), "timezone": "America/New_York",
                  "status": "AVAILABLE" if games else "EMPTY_PROVIDER_SLATE",
                  "retrieved_at": observed.isoformat(), "source_url": url,
                  "provider": "ESPN", "games": games,
                  "provider_events_returned": len(events), "source_sha256": provenance["sha256"],
                  "source_content_sha256": provenance["content_sha256"],
                  "source_raw_path": provenance["raw_path"]}
        if not games:
            result["reason"] = "ESPN returned no matching events for this Eastern date; this does not independently establish the league has no scheduled games."
        return result
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError(f"Malformed ESPN NBA schedule: {type(exc).__name__}") from exc


def _verify(provenance):
    raw = Path(provenance["raw_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != provenance["sha256"]:
        raise ValueError("Schedule raw archive SHA256 mismatch")
    payload = json.loads(raw)
    if digest(payload) != provenance["content_sha256"]:
        raise ValueError("Schedule raw archive content hash mismatch")
    return payload


def _cached(pointer, day, url):
    if not pointer.exists():
        return None
    provenance = json.loads(pointer.read_text(encoding="utf-8"))
    return _parse(_verify(provenance), provenance, day, url)


def _aged(result, cache_status, stale=False):
    return {**result, "cache_status": cache_status, "stale": stale,
            "age_seconds": round((_now() - utc(result["retrieved_at"])).total_seconds(), 3),
            "cache_ttl_seconds": TTL_SECONDS}


def load_slate(output_dir="output/slates", day=None, refresh=False, fetcher=fetch_json):
    """Fetch a dated slate; a failed refresh may return verified LAST_GOOD data.

    Day defaults to the current America/New_York calendar date. Network response
    archives and the latest pointer are local, auditable artifacts; no schedule,
    lineup, injury status or odds are invented when the provider is unavailable.
    """
    selected_day = _now().astimezone(EASTERN).date() if day is None else date.fromisoformat(str(day))
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    pointer = directory / f"latest-{selected_day.isoformat()}.json"
    url = f"{BASE}/scoreboard?dates={selected_day:%Y%m%d}&limit=100"
    generation_before = pointer.read_bytes() if pointer.exists() else None
    with _lock_for(directory, selected_day):
        cached, cache_error = None, None
        try:
            cached = _cached(pointer, selected_day, url)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            cache_error = str(exc)
        generation_after = pointer.read_bytes() if pointer.exists() else None
        refreshed_by_peer = generation_before != generation_after
        if cached:
            age = (_now() - utc(cached["retrieved_at"])).total_seconds()
            if 0 <= age < TTL_SECONDS and (not refresh or refreshed_by_peer):
                return _aged(cached, "FRESH_CACHE")
        try:
            payload, provenance = fetcher(url, directory)
            # Verify both the parsed result and its persisted source bytes before
            # publishing the latest pointer or displaying the schedule.
            archived = _verify(provenance)
            if digest(payload) != digest(archived):
                raise ValueError("Schedule fetch payload differs from archived bytes")
            result = _parse(payload, provenance, selected_day, url)
            temp = pointer.with_suffix(".tmp")
            temp.write_text(canonical(provenance), encoding="utf-8")
            temp.replace(pointer)
            age = (_now() - utc(result["retrieved_at"])).total_seconds()
            if age >= TTL_SECONDS:
                return {**_aged(result, "EXPIRED_PROVIDER_RESPONSE", True), "status": "LAST_GOOD",
                        "last_good_status": result["status"],
                        "reason": "Provider response observation time is expired; current schedule freshness is not established."}
            return _aged(result, "FETCHED")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if cached:
                return {**_aged(cached, "REFRESH_FAILED", True), "status": "LAST_GOOD",
                        "last_good_status": cached["status"], "reason": f"Schedule refresh failed: {exc}"}
            reason = f"Schedule unavailable: {exc}"
            if cache_error:
                reason += f"; cached source rejected: {cache_error}"
            return {"date": selected_day.isoformat(), "timezone": "America/New_York",
                    "status": "UNAVAILABLE", "retrieved_at": None, "source_url": url,
                    "provider": "ESPN", "games": [], "stale": False,
                    "cache_status": "NO_VERIFIED_SOURCE", "age_seconds": None,
                    "cache_ttl_seconds": TTL_SECONDS, "reason": reason}
