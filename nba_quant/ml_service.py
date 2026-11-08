"""Versioned local ML research serving. Training never runs in an HTTP request."""
from datetime import datetime, timezone
from pathlib import Path
import json
import time
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .schemas import Record, utc
from .store import canonical, digest


class MLRequest(Record):
    sport: str = "NBA"
    model_id: str = Field(min_length=1, max_length=100)
    game_id: str = Field(min_length=1, max_length=80)
    home_id: str
    away_id: str
    team_id: str
    player_id: str | None = None
    target: str
    tipoff: datetime
    as_of: datetime
    horizon: str = "pregame"
    threshold: float | None = None
    _times = field_validator("tipoff", "as_of")(utc)

    @model_validator(mode="after")
    def validate_horizon(self):
        now = datetime.now(timezone.utc)
        if self.sport != "NBA" or self.horizon != "pregame":
            raise ValueError("Only NBA pregame forecasts are supported")
        if self.as_of >= self.tipoff or self.as_of > now or self.tipoff <= now:
            raise ValueError("A current pregame cutoff and future tipoff are required")
        if self.home_id == self.away_id or self.team_id not in {self.home_id, self.away_id}:
            raise ValueError("Invalid game/team identity")
        return self


class MLService:
    def __init__(self, store, root="output/ml"):
        self.store, self.root = store, Path(root)
        self._status_cache = None
        with store.connect() as db:
            for table in ("ml_predictions", "ml_operations", "ml_outcomes"):
                db.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
                db.executescript(f"""
                    CREATE TRIGGER IF NOT EXISTS immutable_{table} BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT,'Immutable ML audit record'); END;
                    CREATE TRIGGER IF NOT EXISTS no_delete_{table} BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT,'Immutable ML audit record'); END;
                """)
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ml_outcomes_prediction ON ml_outcomes(json_extract(payload,'$.prediction_id'))")

    def append(self, table, value):
        if table not in {"ml_predictions", "ml_operations", "ml_outcomes"}:
            raise ValueError("Unknown audit table")
        value = {**value, "id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat()}
        with self.store.connect() as db:
            db.execute(f"INSERT INTO {table} VALUES(?,?,?)", (value["id"], value["created_at"], canonical(value)))
        return value

    def history(self, table="ml_predictions", limit=100):
        if table not in {"ml_predictions", "ml_operations", "ml_outcomes"}:
            raise ValueError("Unknown audit table")
        with self.store.connect() as db:
            return [json.loads(r[0]) for r in db.execute(f"SELECT payload FROM {table} ORDER BY created_at DESC LIMIT ?", (min(1000, max(1, limit)),))]

    def models(self):
        from .ml_engine import list_runs, list_run_failures
        return {"runs": list_runs(self.root), "incomplete_runs": list_run_failures(self.root), "api_version": "ml/v1"}

    def backtests(self):
        reports, failures = [], []
        directory = (self.root / "backtests").resolve()
        for path in sorted(directory.glob("backtest-*.json")):
            try:
                if path.is_symlink() or path.resolve().parent != directory:
                    raise ValueError("Untrusted backtest report path")
                value = json.loads(path.read_text(encoding="utf-8"))
                if path.stem != "backtest-" + digest(value):
                    raise ValueError("Backtest report checksum mismatch")
                reports.append({**value, "report_id": path.stem})
            except (OSError, ValueError) as exc:
                failures.append({"report_id": path.stem, "status": "CORRUPT", "reason": str(exc)})
        return {"reports": reports, "failures": failures, "api_version": "ml/v1"}

    def status(self):
        from .ml_features import TARGET_REGISTRY, build_training_rows
        datasets = self.store.datasets()
        key = digest([d["id"] for d in datasets])
        if not self._status_cache or self._status_cache[0] != key:
            bundle = build_training_rows(datasets, mode="strict")
            counts = {t: sum(t in r["targets"] for r in bundle["rows"]) for t in TARGET_REGISTRY}
            self._status_cache = key, counts, bundle["audit"]
        runs = self.models()["runs"]
        targets = []
        for name, config in TARGET_REGISTRY.items():
            relevant = [r for r in runs if r.get("target") == name]
            targets.append({**config, "target": name, "data_rows": self._status_cache[1][name],
                "status": "SHADOW_TRAINED" if relevant else "UNTRAINED",
                "prediction_horizon": "NBA pregame, next single game",
                "required_inputs": ["prior completed and observed canonical box scores",
                    "exact game/team identity"] + (["timestamped pregame roster"] if config["scope"] == "player" else []),
                "trained_versions": [r["id"] for r in relevant],
                "validation_metrics": {r["id"]: r.get("final_test_metrics") for r in relevant},
                "feature_schemas": {r["id"]: r.get("feature_columns") for r in relevant},
                "production_eligible": False, "source_status": "STRICT_KNOWLEDGE_TIME",
                "uncertainty_status": "See version-specific held-out calibration; no joint probabilities"})
        predictions, operations = self.history(), self.history("ml_operations")
        latencies = [p["latency_ms"] for p in predictions if "latency_ms" in p]
        monitoring = {"window": "Latest 100 persisted predictions and operations",
            "prediction_volume": len(predictions), "failure_count": sum(o.get("status") == "FAILED" for o in operations),
            "mean_success_latency_ms": sum(latencies)/len(latencies) if latencies else None,
            "last_prediction_refresh": predictions[0]["created_at"] if predictions else None,
            "last_successful_training": next((o["created_at"] for o in operations if o.get("operation") == "training" and o.get("status") == "COMPLETED"), None),
            "last_failure": next((o for o in operations if o.get("status") == "FAILED"), None)}
        return {"api_version": "ml/v1", "sport": "NBA", "targets": targets,
                "data_audit": self._status_cache[2], "models": runs,
                "monitoring": monitoring,
                "training": "Separate CLI process", "execution": "NONE",
                "production_status": "BLOCKED_PENDING_PROSPECTIVE_VALIDATION"}

    def predict(self, request):
        from .ml_engine import load_manifest, predict_run
        from .ml_features import features_for_game, TARGET_REGISTRY
        started = time.monotonic()
        req = request.model_dump(mode="json")
        try:
            if request.target not in TARGET_REGISTRY:
                raise ValueError("Unsupported target")
            manifest = load_manifest(self.root, request.model_id)
            if manifest["target"] != request.target:
                raise ValueError("Model target differs from request")
            game = {"id": request.game_id, "home_id": request.home_id,
                    "away_id": request.away_id, "tipoff": request.tipoff.isoformat()}
            # Identity is anchored in the source-backed current schedule, not caller assertions.
            from .slate import load_slate
            slate = load_slate()
            exact = [g for g in slate.get("games", []) if g["id"] == request.game_id
                     and g["home"]["id"] == request.home_id and g["away"]["id"] == request.away_id
                     and utc(g["tipoff"]) == request.tipoff]
            if slate.get("stale") or slate.get("status") not in {"AVAILABLE", "PARTIAL"} or not exact:
                raise ValueError("Fresh exact-game schedule evidence is required")
            if not slate.get("retrieved_at") or utc(slate["retrieved_at"]) > request.as_of:
                raise ValueError("Schedule observation follows the requested prediction cutoff")
            roster = None
            if request.player_id:
                from .context import load_context
                from .board import validate_game_context
                context = load_context()
                validate_game_context(context, exact[0])
                if utc(context["retrieved_at"]) > request.as_of:
                    raise ValueError("Roster snapshot was observed after the requested cutoff")
                roster = [{**p, "known_at": context["retrieved_at"],
                    "source": {"sha256": context.get("hash"), "retrieved_at": context["retrieved_at"]}}
                    for p in context["games"][request.game_id]["players"]]
            bundle = features_for_game(self.store.datasets(), game, cutoff=request.as_of, mode="strict", roster=roster)
            rows = bundle["rows"] if isinstance(bundle, dict) else bundle
            entity = request.player_id or request.team_id
            rows = [r for r in rows if r["entity_id"] == entity and r["team_id"] == request.team_id]
            if len(rows) != 1:
                raise ValueError("Required point-in-time features or roster are unavailable")
            result = predict_run(self.root, request.model_id, rows)
            summary = manifest.get("feature_training_summary", {})
            drift = []
            for name, observed in rows[0]["features"].items():
                training = summary.get(name)
                if not training:
                    continue
                sd = training.get("std", 0)
                z = (observed-training["mean"])/sd if sd else None
                if observed < training["min"] or observed > training["max"] or (z is not None and abs(z) > 3):
                    drift.append({"feature": name, "observed": observed, "training_min": training["min"],
                        "training_max": training["max"], "z_score": z})
            value = {"api_version": "ml/v1", "status": "SHADOW_RESEARCH", "request": req,
                "model_id": request.model_id, "model_version": manifest.get("version", request.model_id),
                "result": result, "input_hash": digest(rows), "input_provenance": rows[0].get("provenance"),
                "as_of": request.as_of.isoformat(), "production_eligible": False,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "feature_drift": {"status": "DESCRIPTIVE_RANGE_SCREEN" if summary else "UNAVAILABLE",
                    "flags": drift, "interpretation": "Training-range checks do not prove statistical distribution drift"},
                "input_data_freshness": {"schedule_retrieved_at": slate.get("retrieved_at"),
                    "latest_stat_observation": rows[0].get("provenance", {}).get("feature_latest_known_at"),
                    "roster_known_at": rows[0].get("provenance", {}).get("roster_known_at")},
                "threshold_probability": None,
                "threshold_status": "UNAVAILABLE: no validated threshold distribution",
                "latency_ms": round(1000 * (time.monotonic() - started), 3)}
            return self.append("ml_predictions", value)
        except Exception as exc:
            self.append("ml_operations", {"operation": "inference", "status": "FAILED", "request": req,
                "reason": str(exc), "latency_ms": round(1000 * (time.monotonic() - started), 3)})
            raise

    def reconcile(self):
        """Append outcomes only; never mutate the original pregame prediction."""
        from .ml_features import target_values
        datasets = self.store.datasets()
        games = {g["id"]: g for d in datasets if not d["synthetic"] for g in d["games"]}
        saved = []
        for pred in self.history(limit=1000):
            req = pred["request"]
            game = games.get(req["game_id"])
            with self.store.connect() as db:
                recorded = db.execute("SELECT 1 FROM ml_outcomes WHERE json_extract(payload,'$.prediction_id')=?", (pred["id"],)).fetchone()
            if recorded or not game:
                continue
            if (game["home_id"], game["away_id"], utc(game["tipoff"])) != (req["home_id"], req["away_id"], utc(req["tipoff"])):
                continue
            if max(utc(game["completed_at"]), utc(game["known_at"])) > datetime.now(timezone.utc):
                continue
            if utc(pred["created_at"]) >= utc(game["tipoff"]):
                continue
            if req.get("player_id"):
                box = next((b for b in game["boxes"] if b["player_id"] == req["player_id"] and b["team_id"] == req["team_id"]), None)
                values = target_values(box) if box else {}
            else:
                own = game["home_score"] if req["team_id"] == game["home_id"] else game["away_score"]
                other = game["away_score"] if req["team_id"] == game["home_id"] else game["home_score"]
                values = {"team_points": own, "opponent_points": other, "margin": own-other, "total": own+other, "win": int(own>other)}
            if req["target"] in values:
                saved.append(self.append("ml_outcomes", {"prediction_id": pred["id"], "target": req["target"],
                    "actual": values[req["target"]], "game_id": game["id"], "source_hash": digest(game)}))
        return {"recorded": len(saved), "outcomes": saved}
