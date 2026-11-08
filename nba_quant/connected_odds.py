"""Bind an authenticated odds-provider response to current NBA identities.

Published editorial odds remain separate. This path runs only when an existing
The Odds API credential is configured, and never invents a combined SGP price.
"""
import math
import re
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from .board import validate_game_context
from .odds import OddsAPI
from .published_odds import normalized_name
from .schemas import utc
from .sgp import Leg


def team_name(value):
    name = normalized_name(value)
    return {"laclippers": "losangelesclippers"}.get(name, name)


def event_matches(event, game):
    try:
        return (team_name(event["home_team"]) == team_name(game["home"]["name"])
                and team_name(event["away_team"]) == team_name(game["away"]["name"])
                and utc(event["commence_time"]).astimezone(ZoneInfo("America/New_York")).date() == utc(game["tipoff"]).astimezone(ZoneInfo("America/New_York")).date()
                and abs((utc(event["commence_time"]) - utc(game["tipoff"])).total_seconds()) <= 900)
    except (KeyError, TypeError, ValueError):
        return False


def refresh_connected_odds(store, slate, context, client=None):
    client = client if client is not None else OddsAPI()
    report = {"status": "NOT_CONFIGURED", "events_matched": 0, "lines_imported": 0,
              "unmapped": [], "errors": [], "combined_sgp_quote": None,
              "provider": "The Odds API", "sportsbook": "fanduel"}
    if not client.configured:
        return report
    if slate.get("status") != "AVAILABLE" or slate.get("stale"):
        raise ValueError("Fresh schedule required for automatic odds identity mapping")
    now = datetime.now(timezone.utc)
    try:
        events = client.events()["events"]
    except (ValueError, KeyError, TypeError) as exc:
        return {**report, "status": "PROVIDER_UNAVAILABLE", "errors": [{"reason": str(exc)}]}
    if not isinstance(events, list):
        raise ValueError("Invalid provider event list")
    for game in slate["games"]:
        if game.get("completed") or game.get("phase", game.get("status")) != "pre" or utc(game["tipoff"]) <= now:
            continue
        matches = [e for e in events if isinstance(e, dict) and event_matches(e, game)]
        if len(matches) != 1:
            report["unmapped"].append({"game_id": game["id"], "reason": "No unique exact team-pair and tipoff-window event match"})
            continue
        try:
            validate_game_context(context, game)
            entry = context["games"][game["id"]]
            mapped_event = matches[0]
            result = client.props(mapped_event["id"], bookmakers="fanduel")
            if not event_matches(result["event"], game) or result["event"].get("id") != mapped_event["id"]:
                raise ValueError("Prop response event identity differs from selected event")
            rows = []
            for line in result["lines"]:
                if line.get("sportsbook") != "fanduel":
                    continue
                candidates = [p for p in entry["players"] if normalized_name(p["name"]) == normalized_name(line.get("source_player_name", ""))]
                if len(candidates) != 1:
                    report["unmapped"].append({"game_id": game["id"], "source_player_name": line.get("source_player_name"),
                                                "reason": "No unique exact current-roster player name match"})
                    continue
                player = candidates[0]
                if player["team_id"] not in {game["home"]["id"], game["away"]["id"]}:
                    continue
                observed, retrieved = utc(line["observed_at"]), utc(line["retrieved_at"])
                received_now = datetime.now(timezone.utc)
                if not observed <= retrieved <= received_now or (received_now - observed).total_seconds() > 300:
                    report["unmapped"].append({"game_id": game["id"], "source_player_name": line.get("source_player_name"), "reason": "Stale or future provider quote"})
                    continue
                price = line["decimal_odds"]
                if type(price) not in (int, float) or not math.isfinite(price) or price <= 1:
                    raise ValueError("Invalid automatic provider price")
                if not re.fullmatch(r"[0-9a-f]{64}", line.get("source_sha256", "")):
                    raise ValueError("Automatic quote is missing archived source hash")
                leg = Leg(player_id=player["id"], stat=line["stat"], side=line["side"], line=line["line"])
                rows.append({**line, **leg.model_dump(), "team_id": player["team_id"],
                    "game_id": game["id"], "home_id": game["home"]["id"], "away_id": game["away"]["id"],
                    "tipoff": game["tipoff"], "identity_status": "EXACT_CURRENT_ROSTER_MATCH",
                    "provider_event_id": mapped_event["id"], "provider_tipoff": mapped_event["commence_time"],
                    "event_time_discrepancy_seconds": (utc(mapped_event["commence_time"]) - utc(game["tipoff"])).total_seconds(),
                    "authenticity": "PROVIDER_REPORTED_NOT_INDEPENDENTLY_VERIFIED"})
            report["events_matched"] += 1
            if rows:
                record = {"id": str(uuid4()), "game_id": game["id"], "home_id": game["home"]["id"],
                    "away_id": game["away"]["id"], "tipoff": game["tipoff"], "created_at": datetime.now(timezone.utc).isoformat(),
                    "observed_at": min(utc(r["observed_at"]) for r in rows).isoformat(), "sportsbook": "fanduel",
                    "source_reference": "Automatically acquired The Odds API FanDuel event market response",
                    "offered_rows": rows, "authenticity": "PROVIDER_REPORTED_NOT_INDEPENDENTLY_VERIFIED"}
                store.save_market(record)
                report["lines_imported"] += len(rows)
        except (ValueError, KeyError, TypeError) as exc:
            report["errors"].append({"game_id": game["id"], "reason": str(exc)})
    report["status"] = "ACQUIRED" if report["lines_imported"] and not report["errors"] and not report["unmapped"] else "PARTIAL" if report["lines_imported"] else "NO_MATCHED_CURRENT_QUOTES"
    return report
