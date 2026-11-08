"""Freeze current source-backed shadow prediction combinations; no orders or P(hit)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from nba_quant.board import context_for_game, merged_history
from nba_quant.construction import construct_game_drafts, _offered_alternative
from nba_quant.context import refresh_context
from nba_quant.ml_engine import list_runs, predict_run
from nba_quant.ml_features import features_for_game
from nba_quant.polymarket import CLOB, fetch_public_json, parse_buy_book
from nba_quant.prediction_sweep import select_prediction_family, VERSION
from nba_quant.schemas import utc
from nba_quant.slate import load_slate
from nba_quant.store import Store, digest, canonical


def main():
    root = Path("output/nightly-priority")
    base_path = root / "lays-2026-11-07T004705Z.json"
    base = json.loads(base_path.read_text())
    slate = load_slate(refresh=True)
    now = datetime.now(timezone.utc)
    slate = {**slate, "games": [g for g in slate["games"] if g.get("phase") == "pre" and utc(g["tipoff"]) > now]}
    context = refresh_context(slate)
    if context.get("stale") or context["status"] not in {"AVAILABLE", "PARTIAL"}:
        raise ValueError("Current source-verified roster context unavailable")
    store = Store("data/nba_quant.sqlite")
    datasets, history = store.datasets(), merged_history(store)
    manifests = list_runs("output/ml")
    models = {target: next(m for m in manifests if m.get("target") == target and m.get("status") == "SHADOW")
              for target in ("pts", "reb", "ast", "fg3m", "minutes")}
    forecast_cutoff = datetime.now(timezone.utc)
    projections, raw_forecasts = {}, []
    for game in slate["games"]:
        if utc(game["tipoff"]) <= datetime.now(timezone.utc):
            continue
        roster = [{**p, "known_at": context["retrieved_at"], "source": {
            "retrieved_at": context["retrieved_at"], "sha256": context.get("hash")}}
            for p in context["games"][game["id"]]["players"]]
        feature_bundle = features_for_game(datasets, game, roster=roster, cutoff=forecast_cutoff)
        rows = [r for r in feature_bundle["rows"] if r["scope"] == "player"]
        per_game = {}
        for target, manifest in models.items():
            candidates = {}
            for backend in ("autogluon", "xgboost", "historical_baseline"):
                if manifest["candidates"].get(backend, {}).get("status") != "FIT":
                    raise ValueError(f"Required {target} {backend} forecast unavailable")
                prediction = predict_run("output/ml", manifest["id"], rows, candidate=backend)
                candidates[backend] = prediction
            rmse = max(manifest["candidates"][b]["tuning_metrics"]["rmse"] for b in candidates)
            champion = predict_run("output/ml", manifest["id"], rows)
            for index, row in enumerate(rows):
                per_game[(row["player_id"], target)] = {
                    "model_id": manifest["id"], "point_predictions": {
                        backend: result["predictions"][index]["prediction"] for backend, result in candidates.items()},
                    "tuning_rmse_scale": rmse, "champion": champion["predictions"][index],
                    "calibration_status": champion["calibration"]["status"],
                    "feature_provenance": row["provenance"], "feature_hash": digest(row),
                    "forecast_cutoff": forecast_cutoff.isoformat(), "cohort": manifest["evaluation_cohort"],
                    "production_eligible": False}
            raw_forecasts.append({"game_id": game["id"], "target": target, "model_id": manifest["id"],
                "candidates": candidates, "champion": champion, "cutoff": forecast_cutoff.isoformat(),
                "generated_at": datetime.now(timezone.utc).isoformat()})
        projections[game["id"]] = per_game
        print(json.dumps({"stage": "predicted", "game_id": game["id"], "players": len(rows)}), flush=True)
    # Refresh the original broad scan's qualified over alternatives. Old prices
    # are never carried forward merely because an ML model likes a threshold.
    live_ids = {g["id"] for g in slate["games"]}
    offers = {a["offer"]["asset_id"]: a["offer"] for x in base["games"] if x["game"]["id"] in live_ids
              for c in x["bundle"]["candidates"] for a in c.get("offered_alt_ladder", []) if a.get("evidence_eligible")}
    stopped, quote_errors = threading.Event(), []
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
            quote_errors.append(str(exc))
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        fresh = [o for o in pool.map(refresh, offers.values()) if o]
    results, closed = [], []
    for game in slate["games"]:
        created = datetime.now(timezone.utc)
        if utc(game["tipoff"]) <= created:
            closed.append(game["id"])
            continue
        draft = construct_game_drafts(game, history, context_for_game(context, game)["players"],
            offered_props=fresh, as_of=created, selection_mode="aggressive")
        candidates = []
        for candidate in draft["candidates"]:
            forecast = projections.get(game["id"], {}).get((candidate["player_id"], candidate["stat"]))
            if forecast is None:
                continue
            for alternative in candidate.get("offered_alt_ladder", []):
                if alternative.get("evidence_eligible"):
                    leg = _offered_alternative(candidate, alternative)
                    leg["prediction_evidence"] = forecast
                    leg["minutes_forecast"] = projections[game["id"]].get((candidate["player_id"], "minutes"))
                    candidates.append(leg)
        families = [select_prediction_family(candidates, size, (game["home"]["id"], game["away"]["id"])) for size in (3, 5, 8)]
        frozen = datetime.now(timezone.utc)
        if frozen >= utc(game["tipoff"]):
            closed.append(game["id"])
            continue
        result = {"game": game, "families": families, "created_at": frozen.isoformat(),
            "input_candidate_count": len(candidates), "context_hash": context.get("hash"), "history_hash": history["id"]}
        results.append(result)
        print(json.dumps({"stage": "frozen", "game_id": game["id"], "families": [(f["leg_count"], len(f["legs"])) for f in families]}), flush=True)
    report = {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "forecast_cutoff": forecast_cutoff.isoformat(), "base_report": str(base_path),
        "model_versions": {t: m["id"] for t, m in models.items()}, "games": results,
        "forecasts": raw_forecasts, "closed_games": closed, "quote_errors": quote_errors,
        "quotes_attempted": len(offers), "quotes_refreshed": len(fresh), "production_eligible": False,
        "joint_probability": None, "combined_odds": None,
        "limitations": ["Shadow models trained on retrospective source replay, not validated prospective NBA prediction skill",
            "Three point forecasts are correlated and are not independent evidence or calibrated hit probabilities",
            "Point-gap divided by tuning RMSE is a ranking heuristic, not a calibrated z-score",
            "Minutes are model estimates, not confirmed rotations; existing joint simulator cannot be run without supported rotations",
            "Only source-qualified offered alternatives from the broad base scan were refreshed; no exhaustive market claim",
            "Positive point agreement does not establish positive expected value or a guaranteed sweep"]}
    key = digest(report)
    path = root / ("prediction-sweep-"+key+".json")
    with path.open("x", encoding="utf-8") as file:
        file.write(canonical(report))
    print("SAVED "+str(path), flush=True)


if __name__ == "__main__":
    main()
