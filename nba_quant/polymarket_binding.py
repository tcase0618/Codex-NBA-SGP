"""Bind verified order-book props to exact current NBA identities, read only."""
from datetime import datetime, timezone

from .published_odds import normalized_name
from .schemas import utc
from .sgp import Leg


def bind_polymarket_offers(report, game, players, as_of=None):
    now = utc(as_of) if as_of else datetime.now(timezone.utc)
    rows = []
    if report.get("stale"):
        return rows
    for row in report.get("offers", report.get("markets", [])):
        try:
            if row.get("executable") is not True or row.get("quote_basis") != "ORDERBOOK_BEST_ASK":
                continue
            if (row.get("game_id"), row.get("home_id"), row.get("away_id"), utc(row["tipoff"])) != (
                game["id"], game["home"]["id"], game["away"]["id"], utc(game["tipoff"])):
                continue
            observed, retrieved = utc(row["observed_at"]), utc(row.get("quote_retrieved_at") or row["retrieved_at"])
            if not observed <= retrieved <= now < utc(game["tipoff"]) or (now-observed).total_seconds() > 300:
                continue
            name = normalized_name(row.get("player_name", ""))
            matches = [p for p in players if normalized_name(p["name"]) == name and p.get("team_id") in {
                game["home"]["id"], game["away"]["id"]}]
            if len(matches) != 1:
                continue
            player = matches[0]
            if row.get("player_id") is not None and row["player_id"] != player["id"]:
                continue
            if row.get("team_id") is not None and row["team_id"] != player["team_id"]:
                continue
            leg = Leg(player_id=player["id"], stat=row["stat"], side=row["side"], line=row["line"])
            rows.append({**row, **leg.model_dump(), "team_id": player["team_id"], "event_id": game["id"],
                "retrieved_at": retrieved.isoformat(), "source_sha256": row.get("quote_source_sha256") or row.get("source_sha256"),
                "sportsbook": "polymarket", "identity_status": "EXACT_EVENT_AND_CURRENT_ROSTER_MATCH",
                "authenticity": "PUBLIC_ORDERBOOK_SOURCE_RECORDED"})
        except (KeyError, TypeError, ValueError):
            continue
    return rows
