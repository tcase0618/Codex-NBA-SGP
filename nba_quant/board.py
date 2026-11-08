"""Nightly source context and deterministic construction, separate from HTTP."""
from datetime import datetime, timezone
from uuid import uuid4

from .schemas import utc
from .store import digest


def merged_history(store):
    datasets = [d for d in store.datasets() if not d["synthetic"]]
    if not datasets:
        return None
    teams, players, games = {}, {}, {}
    game_sources = {}
    for d in datasets:
        teams.update({p["id"]: p for p in d["teams"]})
        players.update({p["id"]: p for p in d["players"]})
        for game in d["games"]:
            if game["id"] not in games or utc(game["known_at"]) >= utc(games[game["id"]]["known_at"]):
                games[game["id"]] = game
                game_sources[game["id"]] = d["id"]
    result = {"source": "Merged immutable imported NBA history", "source_url": "local://immutable-datasets",
        "retrieved_at": max(d["retrieved_at"] for d in datasets), "synthetic": False,
        "teams": list(teams.values()), "players": list(players.values()), "rotations": [],
        "games": list(games.values()), "dataset_ids": [d["id"] for d in datasets], "game_sources": game_sources}
    result["id"] = digest(result)
    return result


def player_history(dataset, player_id, team_id, as_of):
    rows = [b for g in dataset["games"] if utc(g["known_at"]) <= as_of and utc(g["completed_at"]) < as_of
            for b in g["boxes"] if b["player_id"] == player_id and b["team_id"] == team_id and b["minutes"] > 0]
    n = len(rows)
    if not n:
        return {"games": 0, "minutes": None, "pts": None, "reb": None, "ast": None, "fg3m": None}
    return {"games": n, **{k: round(sum((b["oreb"] + b["dreb"] if k == "reb" else b[k]) for b in rows) / n, 2)
                           for k in ("minutes", "pts", "reb", "ast", "fg3m")}}


def context_for_game(context, game, dataset=None):
    entry = context.get("games", {}).get(game["id"], {})
    try:
        matches = (entry.get("home_id"), entry.get("away_id"), utc(entry["tipoff"])) == (
            game["home"]["id"], game["away"]["id"], utc(game["tipoff"]))
    except (KeyError, ValueError, TypeError):
        matches = False
    if entry and not matches:
        return {"status": "GAME_IDENTITY_MISMATCH", "players": [],
                "errors": ["Roster context does not match the current home/away pair and exact tipoff"]}
    players = entry.get("players", [])
    now = datetime.now(timezone.utc)
    enriched = [{**p, "historical": player_history(dataset, p["id"], p["team_id"], now) if dataset else {"games": 0}} for p in players]
    return {**entry, "players": enriched, "status": context.get("status", "UNAVAILABLE"),
        "retrieved_at": context.get("retrieved_at"), "context_hash": context.get("hash") or context.get("id"),
        "errors": context.get("errors", [])}


def validate_game_context(context, game):
    if context.get("stale") or context.get("status") not in {"AVAILABLE", "PARTIAL"}:
        raise ValueError("Fresh verified current roster context is required")
    entry = context.get("games", {}).get(game["id"], {})
    if entry.get("status") != "AVAILABLE":
        raise ValueError("Complete current roster context for this exact game is unavailable")
    try:
        matches = (entry.get("home_id"), entry.get("away_id"), utc(entry["tipoff"])) == (
            game["home"]["id"], game["away"]["id"], utc(game["tipoff"]))
    except (KeyError, ValueError, TypeError):
        matches = False
    if not matches:
        raise ValueError("Roster context identity or tipoff differs from the current scheduled game")


def build_game(store, game, context, dataset=None, selection_mode="standard"):
    from .construction import construct_game_drafts
    dataset = dataset or merged_history(store)
    if dataset is None:
        raise ValueError("Authentic historical data is required")
    now = datetime.now(timezone.utc)
    if game.get("phase", game.get("status")) != "pre" or game.get("completed") or utc(game["tipoff"]) <= now:
        raise ValueError("Pregame construction is unavailable after tipoff or for an unconfirmed game phase")
    validate_game_context(context, game)
    players = context_for_game(context, game)["players"]
    if not players:
        raise ValueError("Current roster context for this exact game is unavailable")
    market = store.latest_market(game["id"], game["tipoff"])
    offered = []
    if market and 0 <= (now - utc(market["observed_at"])).total_seconds() <= 300:
        offered = market["offered_rows"]
    from .polymarket import load_polymarket_odds
    from .polymarket_binding import bind_polymarket_offers
    polymarket = load_polymarket_odds()
    poly_offered = bind_polymarket_offers(polymarket, game, players, now)
    # A source is selected as a whole: exchange and sportsbook legs are never
    # silently mixed into a supposedly purchasable combined ticket.
    if poly_offered:
        offered = poly_offered
    result = construct_game_drafts(game, dataset, players, offered_props=offered, as_of=now,
                                  selection_mode=selection_mode)
    result["market_source"] = "polymarket" if poly_offered else market.get("sportsbook") if offered and market else None
    result["polymarket_quote_status"] = polymarket.get("status", "UNAVAILABLE")
    result.update(id=str(uuid4()), game_id=game["id"], tipoff=game["tipoff"],
                  created_at=now.isoformat(), context_hash=context.get("hash") or context.get("id"),
                  history_hash=dataset["id"], historical_dataset_ids=dataset["dataset_ids"])
    store.save_drafts(result)
    return result
