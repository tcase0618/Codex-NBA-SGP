"""Highest component-price trio inside a bounded, fresh two-model NBA screen.

Never treats the ordering score as a quoted parlay price or hit probability.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import combinations
import json
import math
from pathlib import Path
import threading

from nba_quant.board import merged_history, context_for_game
from nba_quant.construction import construct_game_drafts, _offered_alternative
from nba_quant.context import refresh_context
from nba_quant.ml_engine import list_runs, predict_run
from nba_quant.ml_features import features_for_game
from nba_quant.polymarket import CLOB, fetch_public_json, parse_buy_book, refresh_polymarket_odds
from nba_quant.polymarket_binding import bind_polymarket_offers
from nba_quant.published_odds import normalized_name
from nba_quant.schemas import utc
from nba_quant.slate import load_slate
from nba_quant.store import Store, canonical, digest


def main():
    slate = load_slate(refresh=True)
    now = datetime.now(timezone.utc)
    late = {"401909294", "401909295"}
    slate = {**slate, "games": [g for g in slate["games"] if g["id"] in late
        and g.get("phase") == "pre" and utc(g["tipoff"]) > now]}
    if len(slate["games"]) != 2:
        raise ValueError("Both exact late games must still be pregame")
    context = refresh_context(slate)
    if context.get("stale") or context["status"] not in {"AVAILABLE", "PARTIAL"}:
        raise ValueError("Fresh roster context unavailable")
    store = Store("data/nba_quant.sqlite")
    datasets, history = store.datasets(), merged_history(store)
    manifests = list_runs("output/ml")
    models = {stat: next(m for m in manifests if m.get("target") == stat and m.get("status") == "SHADOW")
              for stat in ("pts", "reb", "ast", "fg3m")}
    cutoff = datetime.now(timezone.utc)
    forecasts, players_by_game, histories = {}, {}, {}
    for game in slate["games"]:
        roster = [{**p, "known_at": context["retrieved_at"], "source": {"sha256": context["hash"],
            "retrieved_at": context["retrieved_at"]}} for p in context["games"][game["id"]]["players"]]
        players = {normalized_name(p["name"]): p for p in roster}
        players_by_game[game["id"]] = players
        bundle = features_for_game(datasets, game, cutoff=cutoff, roster=roster)
        rows = [r for r in bundle["rows"] if r["scope"] == "player"]
        for stat, manifest in models.items():
            outputs = {backend: predict_run("output/ml", manifest["id"], rows, candidate=backend)
                       for backend in ("autogluon", "xgboost")}
            for i, row in enumerate(rows):
                forecasts[(game["id"], row["player_id"], stat)] = {
                    "model_id": manifest["id"], "point_predictions": {
                        backend: result["predictions"][i]["prediction"] for backend, result in outputs.items()},
                    "feature_hash": digest(row), "feature_provenance": row["provenance"]}
        for player in roster:
            entries = sorted([(g, b) for g in history["games"] if utc(g["completed_at"]) < cutoff
                and utc(g["known_at"]) <= cutoff for b in g["boxes"]
                if b["player_id"] == player["id"] and b["team_id"] == player["team_id"] and b["minutes"] > 0],
                key=lambda pair: utc(pair[0]["tipoff"]), reverse=True)[:10]
            histories[(game["id"], player["id"])] = entries
    # Fetch fresh catalog metadata; the single initial market request also
    # establishes public CLOB access. Quote only double-supported player lines.
    catalog = refresh_polymarket_odds(slate=slate, max_markets=1)
    if catalog["status"] == "QUOTE_ACCESS_UNAVAILABLE":
        raise ValueError("Public quote access denied; no further requests")
    eligible, metadata_qualified = [], 0
    for offer in catalog["markets"]:
        gid = offer.get("game_id")
        if gid not in players_by_game or offer.get("side") != "over" or offer.get("stat") not in models:
            continue
        if not (offer.get("active") is True and offer.get("closed") is False and offer.get("accepting_orders") is True):
            continue
        player = players_by_game[gid].get(normalized_name(offer.get("player_name", "")))
        if not player or player.get("availability") in {"OUT", "DOUBTFUL", "QUESTIONABLE"}:
            continue
        evidence = forecasts.get((gid, player["id"], offer["stat"]))
        if not evidence or min(evidence["point_predictions"].values()) <= offer["line"]:
            continue
        rows = histories[(gid, player["id"])]
        if len(rows) < 5 or sum(b["minutes"] for _, b in rows)/len(rows) < 18:
            continue
        values = [(b["oreb"]+b["dreb"] if offer["stat"] == "reb" else b[offer["stat"]]) for _, b in rows]
        hits = sum(v > offer["line"] for v in values)
        if hits < 2 or hits/len(rows) < .4:
            continue
        metadata_qualified += 1
        eligible.append({**offer, "prediction_evidence": evidence})
    print(json.dumps({"stage": "double_supported_metadata", "outcomes": len(eligible)}), flush=True)
    stopped, errors = threading.Event(), []
    def refresh(offer):
        if stopped.is_set():
            return None
        try:
            book, source = fetch_public_json(CLOB+"/book?token_id="+offer["asset_id"], Path("output/polymarket"))
            parsed = parse_buy_book(book, offer["asset_id"], source, offer["condition_id"])
            if parsed["quote_status"] != "ASK_DEPTH_AVAILABLE" or not parsed.get("best_ask_has_requested_depth"):
                return None
            return {**offer, **{k: parsed[k] for k in ("observed_at", "american_odds", "decimal_odds", "best_ask", "quote_status")},
                "orderbook": parsed, "book_source": source, "source_sha256": source["sha256"],
                "quote_source_sha256": source["sha256"], "quote_retrieved_at": source["retrieved_at"],
                "retrieved_at": source["retrieved_at"], "executable": True,
                "eligible_for_constructor": True, "quote_basis": "ORDERBOOK_BEST_ASK"}
        except Exception as exc:
            if getattr(exc, "http_status", None) in (401, 403):
                stopped.set()
            errors.append(str(exc))
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        fresh = [o for o in pool.map(refresh, eligible) if o]
    bests, candidate_archive = [], []
    for game in slate["games"]:
        created = datetime.now(timezone.utc)
        if created >= utc(game["tipoff"]):
            continue
        players = context_for_game(context, game)["players"]
        bound = bind_polymarket_offers({"offers": fresh}, game, players, created)
        draft = construct_game_drafts(game, history, players, offered_props=bound, as_of=created, selection_mode="aggressive")
        reduced = {}
        for candidate in draft["candidates"]:
            for alt in candidate.get("offered_alt_ladder", []):
                if not alt.get("evidence_eligible"):
                    continue
                leg = _offered_alternative(candidate, alt)
                evidence = forecasts[(game["id"], leg["player_id"], leg["stat"])]
                if min(evidence["point_predictions"].values()) <= leg["line"]:
                    continue
                leg["prediction_evidence"] = evidence
                key = leg["player_id"], leg["stat"]
                prior = reduced.get(key)
                if prior is None or leg["offer"]["decimal_odds"] > prior["offer"]["decimal_odds"]:
                    reduced[key] = leg
        candidates = list(reduced.values())
        candidate_archive.append({"game_id": game["id"], "candidates": candidates})
        feasible = [trio for trio in combinations(candidates, 3) if len({l["player_id"] for l in trio}) == 3
                    and max(sum(l["stat"] == s for l in trio) for s in models) <= 2]
        if feasible:
            legs = max(feasible, key=lambda trio: (sum(math.log(l["offer"]["decimal_odds"]) for l in trio),
                tuple(sorted(l["id"] for l in trio))))
            bests.append({"game": game, "legs": list(legs), "feasible_trios": len(feasible),
                "component_price_ordering_score": sum(math.log(l["offer"]["decimal_odds"]) for l in legs)})
    if not bests:
        raise ValueError("No source-qualified same-game trio clears both model estimates")
    best = max(bests, key=lambda item: item["component_price_ordering_score"])
    created = datetime.now(timezone.utc)
    if created >= utc(best["game"]["tipoff"]):
        raise ValueError("Pregame window closed before result could be frozen")
    report = {"created_at": created.isoformat(), "forecast_cutoff": cutoff.isoformat(), "best": best,
        "per_game_best": bests, "candidate_archive": candidate_archive, "models": {s: m["id"] for s, m in models.items()},
        "catalog_outcomes": len(catalog["markets"]), "double_supported_metadata_outcomes": metadata_qualified,
        "depth_backed_quotes": len(fresh), "quote_errors": errors,
        "selection": "Maximum sum log individual decimal prices, three distinct players within one exact game, at most two legs of one statistic; exhaustive within retained eligible alternatives",
        "scope": "Two late games, supported points/rebounds/assists/threes; five prior appearances, two hits, >=40% historical frequency and source quality gates retained",
        "double_supported": "Both AutoGluon and XGBoost point estimates strictly exceed each over line; baseline point agreement not required",
        "production_eligible": False, "joint_probability": None, "quoted_combined_odds": None,
        "limitations": ["Ordering score is not a combined ticket quote or a joint hit probability",
            "Point agreement does not establish P(over)>0.5, positive EV, model independence, or prospective predictive skill",
            "Shadow models trained retrospectively; participation/minutes and settlement terms remain unconfirmed"]}
    path = Path("output/nightly-priority")/("highest-double-supported-"+digest(report)+".json")
    with path.open("x", encoding="utf-8") as file:
        file.write(canonical(report))
    print("SAVED "+str(path), flush=True)
    print(json.dumps({"created_at": report["created_at"], "game": best["game"], "legs": [{
        "player": l["player_name"], "line": l["line"], "stat": l["stat"], "american": l["offer"]["american_odds"],
        "predictions": l["prediction_evidence"]["point_predictions"]} for l in best["legs"]],
        "compared": [(b["game"]["id"], b["feasible_trios"]) for b in bests]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
