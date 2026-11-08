"""Bind published FanDuel research quotes without asserting live freshness."""
import re
import unicodedata
from zoneinfo import ZoneInfo

from .schemas import utc


def normalized_name(value):
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]", "", text)


def bind_published_offers(snapshot, game, context_players):
    mapped = []
    for offer in snapshot.get("offers", []):
        name = offer.get("player_name")
        matches = [p for p in context_players if normalized_name(p["name"]) == normalized_name(name)]
        if len(matches) != 1:
            continue
        player = matches[0]
        if player["team_id"] not in {game["home"]["id"], game["away"]["id"]}:
            continue
        source_time = offer.get("event_tipoff")
        if not source_time:
            continue
        original, target = utc(source_time), utc(game["tipoff"])
        if original.astimezone(ZoneInfo("America/New_York")).date() != target.astimezone(ZoneInfo("America/New_York")).date():
            continue
        delta = (original - target).total_seconds()
        if abs(delta) > 900:
            continue
        event_name = str(offer.get("event_name", ""))
        event_normal = normalized_name(event_name)
        # Explicit mascot or abbreviation tokens corroborate each team; a player
        # name alone must never bind a quote from a different event.
        def mentioned(team):
            mascot = normalized_name(team["name"].split()[-1])
            abbreviation = team.get("abbreviation", "")
            return mascot in event_normal or (bool(abbreviation) and re.search(r"\b" + re.escape(abbreviation) + r"\b", event_name, re.I) is not None)
        if not all(mentioned(game[s]) for s in ("home", "away")):
            continue
        mapped.append({**offer, "player_id": player["id"], "team_id": player["team_id"],
            "canonical_game_id": game["id"], "canonical_tipoff": game["tipoff"],
            "event_time_discrepancy_seconds": delta, "event_mapping": "EXACT_PLAYER_ROSTER_AND_TEAM_PAIR_DATE_WITHIN_15_MINUTES",
            "price_freshness": "UNKNOWN", "eligible_for_current_pricing": False,
            "actionable": False})
    return mapped
