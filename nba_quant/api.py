import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .demo import make_demo
from .evaluation import aggregate
from .metrics import CATALOG, metric_values, profiles
from .model import LIMITATIONS, MODEL_VERSION
from .schemas import Dataset, ForecastRequest, Record, utc
from .service import forecast, get_dataset, record_outcome
from .store import Store
from .injuries import latest_report
from .odds import OddsAPI
from .sgp import Leg, TicketQuote, evaluate_sgp
from .slate import load_slate
from .context import load_context, refresh_context
from .board import build_game, context_for_game, merged_history, validate_game_context
from .markets import MarketSnapshot
from .fanduel import load_fanduel_odds, refresh_fanduel_odds
from .published_odds import bind_published_offers
from .connected_odds import refresh_connected_odds
from .construction import VERSION as CONSTRUCTION_VERSION
from .aggressive import VERSION as AGGRESSIVE_VERSION
from .family_optimizer import VERSION as FAMILY_OPTIMIZER_VERSION
from .line_policy import VERSION as LINE_POLICY_VERSION
from .polymarket import load_polymarket_odds, refresh_polymarket_odds
from pydantic import Field


class OutcomeRequest(Record):
    dataset_id: str
    game_id: str


class SGPRequest(Record):
    forecast_id: str
    legs: list[Leg] = Field(min_length=2, max_length=12)
    quote: TicketQuote | None = None
    as_of: datetime | None = None


def nightly_requirements(families):
    counts = {str(size): sum(f.get("leg_count") == size and len(f.get("legs", [])) == size
                            for f in families) for size in (5, 8)}
    missing = [size for size in (5, 8) if counts[str(size)] == 0]
    return {"required_sizes": [5, 8], "available_by_size": counts, "missing_sizes": missing,
            "status": "AVAILABLE" if not missing else "PARTIAL" if len(missing) == 1 else "UNAVAILABLE",
            "scope": "Saved research draft coverage, not a live combined quote, guaranteed win or scheduled wager"}


def create_app(db_path=None):
    app = FastAPI(title="NBA Quant", version="0.1.0", description="Experimental local research engine")
    store = Store(db_path or os.environ.get("NBA_QUANT_DB", "data/nba_quant.sqlite"))
    app.state.store = store
    from .ml_service import MLService, MLRequest
    ml = MLService(store, os.environ.get("NBA_QUANT_ML_ROOT", "output/ml"))
    app.state.ml = ml

    @app.get("/api/ml/v1/status")
    def ml_status():
        return ml.status()

    @app.get("/api/ml/v1/models")
    def ml_models():
        return ml.models()

    @app.get("/api/ml/v1/backtests")
    def ml_backtests():
        return ml.backtests()

    @app.post("/api/ml/v1/predict")
    def ml_predict(payload: MLRequest):
        try:
            return ml.predict(payload)
        except ImportError:
            raise HTTPException(503, "ML dependency unavailable: start the server using .venv-ml")
        except FileNotFoundError:
            raise HTTPException(404, "A complete verified local model artifact is unavailable")

    @app.get("/api/ml/v1/history")
    def ml_history():
        return {"predictions": ml.history(), "outcomes": ml.history("ml_outcomes"),
                "operations": ml.history("ml_operations")}

    @app.post("/api/ml/v1/reconcile")
    def ml_reconcile():
        return ml.reconcile()
    simulation_lock = threading.Lock()
    jobs = {}
    job_lock = threading.Lock()
    odds_lock = threading.Lock()
    polymarket_lock = threading.Lock()

    def public_polymarket(report):
        # Full depth and raw receipts stay in the hash-verified archive. The
        # dashboard needs a compact view, not two copies of every order book.
        result = {k: v for k, v in report.items() if k not in {"markets", "offers", "sources"}}
        rows = []
        for row in report.get("offers", report.get("markets", [])):
            compact = {k: v for k, v in row.items() if k not in {"metadata_source", "book_source", "description", "orderbook"}}
            if row.get("orderbook"):
                compact["orderbook"] = {k: v for k, v in row["orderbook"].items() if k != "ask_depth"}
                compact["orderbook"]["ask_depth"] = row["orderbook"].get("ask_depth", [])[:5]
            rows.append(compact)
        return {**result, "markets": rows, "source_receipts_count": len(report.get("sources", [])), "actionable": False}

    @app.middleware("http")
    async def local_writes(request: Request, call_next):
        # No cross-site writes into the user's local research database.
        origin = request.headers.get("origin")
        if request.method in {"POST", "PUT", "DELETE", "PATCH"} and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin writes are disabled"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site" and request.method != "GET":
            return JSONResponse({"detail": "Cross-site writes are disabled"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        policy = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
        if request.url.path in {"/docs", "/docs/oauth2-redirect", "/redoc"}:
            # FastAPI's built-in reference pages load their own CDN renderer.
            policy = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
        response.headers["Content-Security-Policy"] = policy
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({"detail": "Immutable record already exists or source reference is invalid"}, status_code=409)

    @app.get("/api/status")
    def status():
        datasets = [{"id": d["id"], "source": d["source"], "source_url": d["source_url"],
                     "synthetic": d["synthetic"], "teams": d["teams"], "players": d["players"],
                     "games_count": len(d["games"]), "rotation_count": len(d["rotations"]),
                     "retrieved_at": d["retrieved_at"], "imported_at": d["imported_at"]} for d in store.datasets()]
        return {"status": "EXPERIMENTAL" if datasets else "NO DATA", "datasets": datasets,
                "forecasts_count": len(store.forecasts(100000)), "model_version": MODEL_VERSION,
                "construction_version": CONSTRUCTION_VERSION, "aggressive_policy_version": AGGRESSIVE_VERSION,
                "family_optimizer_version": FAMILY_OPTIMIZER_VERSION, "nightly_priority_sizes": [5, 8],
                "line_policy": {"version": LINE_POLICY_VERSION, "under": "standard_source_identified_only", "alternate_lines": "over_only"},
                "limitations": LIMITATIONS,
                "catalog_counts": {s: sum(m["scope"] == s for m in CATALOG) for s in ("player", "PG", "SG", "SF", "PF", "C", "team")},
                "live": "UNAVAILABLE", "tracking": "UNAVAILABLE", "execution": "NONE"}

    @app.get("/api/sources")
    def source_readiness():
        datasets = [d for d in store.datasets() if not d["synthetic"]]
        games = {g["id"]: g for d in datasets for g in d["games"]}
        odds = OddsAPI()
        published = load_fanduel_odds()
        try:
            injury = latest_report("output/official-injuries")
        except ValueError as exc:
            injury = {"status": "HASH_VERIFICATION_FAILED", "reason": str(exc)}
        reasons = ["Independent prospective NBA prop and SGP calibration has not been established.",
            "Current player eligibility and minutes restrictions require verified game-specific snapshots.",
            "Exact offered FanDuel combined ticket quote is required; individual prices are not multiplied.",
            "Sportsbook void, DNP and settlement rules require explicit validation."]
        if not odds.configured:
            reasons.append("No odds subscription configured; current FanDuel prop prices are unavailable.")
        if not games:
            reasons.append("No authentic historical games have been imported.")
        return {"stats_provider": "ESPN (secondary statistics provider)",
            "target_sportsbook": "FanDuel", "history": {"snapshots": len(datasets), "games": len(games),
                "teams": len({t["id"] for d in datasets for t in d["teams"]}),
                "players": len({p["id"] for d in datasets for p in d["players"]}),
                "latest_game_at": max((g["tipoff"] for g in games.values()), default=None)},
            "odds": {"configured": odds.configured, "status": "CONFIGURED_NOT_VERIFIED" if odds.configured else "UNAVAILABLE",
                "combined_sgp_quote": "UNAVAILABLE", "subscription_required": True,
                "automatic_published_source": published["status"], "published_quotes": published.get("offers_count", 0),
                "published_price_freshness": "UNKNOWN", "verified_current_quotes": 0},
            "injuries": injury, "readiness": {"actionable": False, "status": "RESEARCH_ONLY", "reasons": reasons}}

    def tonight_payload(refresh=False):
        slate = load_slate(refresh=refresh)
        context = load_context(day=slate["date"])
        history_dataset = merged_history(store)
        published = load_fanduel_odds()
        datasets = {d["id"]: d for d in store.datasets() if not d["synthetic"]}
        historical = {g["id"]: g for d in datasets.values() for g in d["games"]}
        forecasts = store.forecasts(limit=None)
        # A historical or synthetic forecast is never substituted for tonight.
        result_games = []
        for game in slate.get("games", []):
            home, away = game["home"]["id"], game["away"]["id"]
            matches = []
            for f in forecasts:
                req = f["request"]
                dataset = datasets.get(req.get("dataset_id"))
                if f.get("synthetic") or dataset is None:
                    continue
                if not {home, away} <= {t["id"] for t in dataset["teams"]}:
                    continue
                if (req["home_id"], req["away_id"], utc(req["tipoff"])) == (home, away, utc(game["tipoff"])):
                    matches.append({"id": f["id"], "created_at": f["created_at"], "synthetic": False, "request": req})
            tickets = store.sgps([f["id"] for f in matches])
            bundle = store.latest_drafts(game["id"], game["tipoff"])
            current_market = store.latest_market(game["id"], game["tipoff"])
            market_fresh = current_market and 0 <= (datetime.now(timezone.utc) - utc(current_market["observed_at"])).total_seconds() <= 300
            offered = current_market["offered_rows"] if market_fresh else []
            current_context = context_for_game(context, game, history_dataset)
            published_props = bind_published_offers(published, game, current_context["players"])
            reasons = ["FanDuel offered prop feed and exact combined SGP quotes are not connected.",
                       "Independent NBA SGP calibration remains unverified."]
            if not matches:
                reasons.insert(0, "No current, non-synthetic frozen forecast matches this exact matchup and tipoff.")
            if game.get("completed") or game.get("phase", game.get("status")) in {"in", "post"}:
                reasons.append("This game's pregame window has closed; live SGP evaluation is unavailable.")
            result_games.append({**game, "context": current_context, "history": {
                "home_games": sum(home in {g["home_id"], g["away_id"]} for g in historical.values()),
                "away_games": sum(away in {g["home_id"], g["away_id"]} for g in historical.values())},
                "sgp": {"status": "RESEARCH_ONLY" if matches else "MISSING_FORECAST", "forecasts": matches,
                        "tickets": tickets, "offered_props": offered, "block_reasons": reasons,
                        "published_props": published_props,
                        "draft_families": bundle["families"] if bundle else [],
                        "candidates": bundle["candidates"] if bundle else [],
                        "draft_bundle_id": bundle["id"] if bundle else None,
                        "draft_selection_mode": bundle.get("selection_mode", "standard") if bundle else None,
                        "draft_market_source": bundle.get("market_source") if bundle else None,
                        "polymarket_quote_status": bundle.get("polymarket_quote_status") if bundle else None,
                        "draft_created_at": bundle["created_at"] if bundle else None,
                        "draft_stale": bool(bundle and (context.get("status") not in {"AVAILABLE", "PARTIAL"} or
                            bundle.get("provenance", {}).get("construction_version") != CONSTRUCTION_VERSION or
                            bundle.get("context_hash") != context.get("hash") or
                            (history_dataset and bundle.get("history_hash") != history_dataset["id"]) or
                            (datetime.now(timezone.utc) - utc(bundle["created_at"])).total_seconds() > 3600))}})
        return {**slate, "games": result_games, "target_sportsbook": "FanDuel",
                "nightly_requirements": nightly_requirements([f for g in result_games for f in g["sgp"]["draft_families"]]),
                "published_odds": {"status": published["status"], "offers_count": published.get("offers_count", 0),
                    "retrieved_at": published.get("retrieved_at"), "price_freshness": "UNKNOWN", "verified_current_quotes": 0,
                    "note": published.get("note")},
                "actionable": False, "ticket_scope": "All saved evaluations matching the exact game; not every sportsbook combination",
                "saved_tickets_count": sum(len(g["sgp"]["tickets"]) for g in result_games)}

    def find_tonight_game(game_id):
        slate = load_slate()
        if slate.get("status") != "AVAILABLE" or slate.get("stale"):
            raise ValueError("A fresh schedule is required to build or import markets")
        game = next((g for g in slate["games"] if g["id"] == game_id), None)
        if game is None:
            raise HTTPException(404, "Game not found in tonight's verified slate")
        return slate, game

    @app.post("/api/games/{game_id}/drafts")
    def generate_game_drafts(game_id: str, selection_mode: Literal["standard", "aggressive"] = "standard"):
        slate, game = find_tonight_game(game_id)
        return build_game(store, game, load_context(day=slate["date"]), selection_mode=selection_mode)

    @app.get("/api/polymarket")
    def get_polymarket():
        result = load_polymarket_odds()
        return public_polymarket(result)

    @app.post("/api/polymarket/refresh")
    def refresh_polymarket():
        if not polymarket_lock.acquire(blocking=False):
            raise HTTPException(409, "A Polymarket refresh is already running")
        try:
            slate = load_slate(refresh=True)
            result = refresh_polymarket_odds(slate=slate)
            return public_polymarket(result)
        finally:
            polymarket_lock.release()

    @app.post("/api/markets/import")
    def import_markets(payload: MarketSnapshot):
        slate, game = find_tonight_game(payload.game_id)
        if (payload.home_id, payload.away_id, payload.tipoff) != (game["home"]["id"], game["away"]["id"], utc(game["tipoff"])):
            raise ValueError("Offered markets must match the exact scheduled game")
        context = load_context(day=slate["date"])
        validate_game_context(context, game)
        roster = {p["id"]: p for p in context_for_game(context, game)["players"]}
        for leg in payload.legs:
            if leg.player_id not in roster or roster[leg.player_id]["team_id"] != leg.team_id:
                raise ValueError("A market player could not be matched to this game's current roster")
        record = {**payload.model_dump(mode="json"), "id": str(uuid4()),
                  "created_at": datetime.now(timezone.utc).isoformat(), "offered_rows": payload.offered_rows(),
                  "authenticity": "CALLER_ASSERTION_UNVERIFIED"}
        store.save_market(record)
        return {"id": record["id"], "lines_imported": len(record["offered_rows"]),
                "authenticity": record["authenticity"], "actionable": False}

    @app.post("/api/tonight/build", status_code=202)
    def build_tonight(selection_mode: Literal["standard", "aggressive"] = "standard"):
        with job_lock:
            existing = next((j for j in jobs.values() if j["status"] == "RUNNING"), None)
            if existing:
                return dict(existing)
            key = str(uuid4())
            job = {"job_id": key, "status": "RUNNING", "stage": "Fetching current schedule", "games_built": 0, "errors": [], "selection_mode": selection_mode,
                   "nightly_requirements": nightly_requirements([])}
            jobs[key] = job
        def work():
            try:
                slate = load_slate(refresh=True)
                if slate.get("status") != "AVAILABLE" or slate.get("stale"):
                    raise ValueError("Current schedule could not be verified")
                job.update(stage="Refreshing current team rosters and reported injuries")
                context = refresh_context(slate)
                job.update(stage="Fetching published FanDuel market prices")
                with odds_lock:
                    price_snapshot = refresh_fanduel_odds()
                    connected = refresh_connected_odds(store, slate, context)
                job.update(published_odds_status=price_snapshot["status"], published_quotes=price_snapshot.get("offers_count", 0))
                job.update(connected_odds=connected)
                poly_report = load_polymarket_odds()
                if selection_mode == "aggressive":
                    job.update(stage="Refreshing Polymarket NBA prices")
                    with polymarket_lock:
                        poly_report = refresh_polymarket_odds(slate=slate)
                job.update(polymarket_status=poly_report.get("status"))
                dataset = merged_history(store)
                built_families = []
                for game in slate["games"]:
                    if game.get("completed") or utc(game["tipoff"]) <= datetime.now(timezone.utc):
                        continue
                    job.update(stage=f"Constructing {game['away']['name']} @ {game['home']['name']}")
                    try:
                        bundle = build_game(store, game, context, dataset, selection_mode=selection_mode)
                        built_families.extend(bundle["families"])
                        job["nightly_requirements"] = nightly_requirements(built_families)
                        job["games_built"] += 1
                    except ValueError as exc:
                        job["errors"].append({"game_id": game["id"], "reason": str(exc)})
                job.update(status="COMPLETE", stage="Draft construction complete",
                           context_status=context.get("status"), context_counts=context.get("counts"))
            except Exception as exc:
                job.update(status="FAILED", stage="Build failed", reason=str(exc))
            finally:
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
        threading.Thread(target=work, name=f"nba-build-{key[:8]}", daemon=True).start()
        return dict(job)

    @app.post("/api/odds/refresh")
    def refresh_automatic_odds():
        if not odds_lock.acquire(blocking=False):
            raise HTTPException(409, "An automatic odds refresh is already running")
        try:
            result = refresh_fanduel_odds()
            slate = load_slate()
            connected = refresh_connected_odds(store, slate, load_context(day=slate["date"]))
            return {"status": result["status"], "offers_count": result.get("offers_count", 0),
                "retrieved_at": result.get("retrieved_at"), "price_freshness": "UNKNOWN", "verified_current_quotes": 0,
                "connected_feed": connected,
                "joint_sgp_quote": None, "errors": result.get("errors", []), "note": result.get("note")}
        finally:
            odds_lock.release()

    @app.get("/api/build-jobs/{job_id}")
    def get_build_job(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "Build job not found (jobs are process-local)")
        return dict(jobs[job_id])

    @app.get("/api/tonight")
    def tonight():
        return tonight_payload()

    @app.post("/api/tonight/refresh")
    def refresh_tonight():
        return tonight_payload(refresh=True)

    @app.post("/api/sgp/evaluate")
    def sgp_evaluate(payload: SGPRequest):
        f = store.forecast(payload.forecast_id)
        if f is None:
            raise HTTPException(404, "Unknown forecast")
        result = evaluate_sgp(f, payload.legs, payload.quote, payload.as_of)
        result.update(id=str(uuid4()), created_at=datetime.now(timezone.utc).isoformat())
        store.save_sgp(result)
        return result

    @app.get("/api/sgp/{key}")
    def sgp_detail(key: str):
        result = store.sgp(key)
        if result is None:
            raise HTTPException(404, "Unknown SGP evaluation")
        return result

    @app.post("/api/datasets")
    def import_dataset(payload: Dataset):
        return {"dataset_id": store.import_dataset(payload)}

    @app.get("/api/datasets/{key}/export")
    def export_dataset(key: str):
        d = get_dataset(store, key)
        return {k: v for k, v in d.items() if k not in {"id", "imported_at"}}

    @app.post("/api/demo")
    def demo():
        return {"dataset_id": store.import_dataset(make_demo())}

    @app.post("/api/forecasts")
    def new_forecast(payload: ForecastRequest):
        if not simulation_lock.acquire(blocking=False):
            raise HTTPException(409, "A simulation is already running; retry after it finishes")
        try:
            return forecast(store, payload)
        finally:
            simulation_lock.release()

    @app.get("/api/forecasts")
    def forecast_list():
        return [{"id": f["id"], "created_at": f["created_at"], "synthetic": f["synthetic"],
                 "request": f["request"], "home_win_probability": f["result"]["home_win_probability"]} for f in store.forecasts()]

    @app.get("/api/forecasts/{key}")
    @app.get("/api/forecasts/{key}/export")
    def forecast_detail(key: str, request: Request):
        result = store.forecast(key)
        if result is None:
            raise HTTPException(404, "Unknown forecast")
        return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="forecast-{result["id"]}.json"'} if request.url.path.endswith("/export") else {})

    @app.post("/api/forecasts/{key}/outcome")
    def add_outcome(key: str, payload: OutcomeRequest):
        return record_outcome(store, key, payload.dataset_id, payload.game_id)

    @app.get("/api/evaluation")
    def evaluation():
        return aggregate(store.outcomes())

    @app.get("/api/metrics")
    def metrics(dataset_id: str | None = None, scope: str = "player", entity_id: str = "", cutoff: str | None = None):
        if scope not in {"player", "team", "PG", "SG", "SF", "PF", "C"}:
            raise ValueError("Unknown metric scope")
        if dataset_id:
            return metric_values(get_dataset(store, dataset_id), scope, entity_id, utc(cutoff) if cutoff else datetime.now(timezone.utc))
        ms = [m for m in CATALOG if m["scope"] == scope]
        return {"metrics": ms, "coverage": {"available": 0, "total": len(ms)}}

    @app.get("/api/profiles")
    def entity_profiles(dataset_id: str, cutoff: str | None = None):
        return profiles(get_dataset(store, dataset_id), utc(cutoff) if cutoff else datetime.now(timezone.utc))

    from .intel.api import register as register_intel
    register_intel(app)
    app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="dashboard")
    return app
