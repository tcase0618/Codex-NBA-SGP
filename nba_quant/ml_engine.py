"""Optional, source-aware tabular research training and immutable local artifacts.

Chronological train/tune/calibration/test folds come from ml_features. Champion
selection uses tuning only; calibration sees its own fold; final test is scored
once after both are frozen. All runs start in shadow mode. AutoGluon artifacts
may contain pickle files: load only locally trained, hash-verified artifacts
inside a user-trusted root. Checksums detect drift, not hostile root compromise.
"""
import hashlib
import importlib.metadata
import json
import math
import os
import re
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np

from .schemas import utc
from .store import canonical, digest

VERSION = "chronological-tabular-research-0.1.0"
_registry_lock = threading.Lock()
UNSAFE_FEATURE = re.compile(r"(^|_)(actual|label|target|outcome|future|postgame)(_|$)")


def _now():
    return datetime.now(timezone.utc)


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _inside(root, path):
    root, result = Path(root).resolve(), Path(path).resolve()
    if not result.is_relative_to(root) or result == root:
        raise ValueError("Model artifact path escapes the trusted root")
    return result


def _json_new(path, payload):
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(canonical(payload))


def _feature_matrix(rows, columns):
    matrix = []
    for row in rows:
        features = row.get("features", {})
        unknown = set(features) - set(columns)
        if unknown:
            raise ValueError(f"Unexpected feature columns: {sorted(unknown)}")
        values = []
        for name in columns:
            value = features.get(name)
            if value is not None and not _number(value):
                raise ValueError(f"Feature {name} must be numeric or explicitly missing")
            values.append(float(value) if value is not None else np.nan)
        matrix.append(values)
    return np.asarray(matrix, dtype=float).reshape(len(rows), len(columns))


def _baseline_keys(target, scope):
    if scope == "player":
        return f"player_{target}_mean", f"player_{target}_recent3_mean"
    mean = {"team_points": "team_points_mean", "opponent_points": "opponent_points_mean",
            "margin": "team_margin_mean", "total": "team_total_mean", "win": "team_win_rate"}.get(target, f"team_{target}_mean")
    return mean, mean.replace("_mean", "_recent3_mean") if mean.endswith("_mean") else "team_win_recent3_rate"


def _fit_baselines(rows, target, scope):
    grouped = defaultdict(list)
    for row in sorted(rows, key=lambda r: (utc(r["cutoff"]), r["game_id"], str(r.get("entity_id")))):
        grouped[str(row.get("entity_id", ""))].append(row["label"])
    mean_key, recent_key = _baseline_keys(target, scope)
    return {"global_mean": float(np.mean([r["label"] for r in rows])),
            "entity_means": {k: float(np.mean(v)) for k, v in grouped.items()},
            "entity_recent3": {k: float(np.mean(v[-3:])) for k, v in grouped.items()},
            "historical_feature": mean_key, "recent_feature": recent_key,
            "fit_scope": "Training labels only; per-row pregame mean/recent features preferred when available"}


def _baseline_predict(params, rows, recent=False):
    key = params["recent_feature" if recent else "historical_feature"]
    table = params["entity_recent3" if recent else "entity_means"]
    result = []
    for row in rows:
        feature = row.get("features", {}).get(key)
        value = float(feature) if _number(feature) else table.get(str(row.get("entity_id", "")), params["global_mean"])
        result.append(value)
    return np.asarray(result)


def _domain(predictions, target, kind):
    values = np.asarray(predictions, dtype=float)
    if kind == "binary":
        return np.clip(values, 0, 1)
    return np.clip(values, 0 if target != "margin" else -np.inf, 100 if target == "minutes" else np.inf)


def _intervals(predictions, radius, target):
    p = np.asarray(predictions, dtype=float)
    lower, upper = p - radius, p + radius
    if target != "margin":
        lower = np.maximum(0, lower)
    if target == "minutes":
        upper = np.minimum(100, upper)
    return lower, upper


def _metrics(y, predictions, kind):
    y, p = np.asarray(y, dtype=float), np.asarray(predictions, dtype=float)
    if y.shape != p.shape or not len(y) or not np.isfinite(y).all() or not np.isfinite(p).all():
        raise ValueError("Scoring requires aligned, finite, nonempty observations and predictions")
    if kind == "binary":
        p = np.clip(p, 1e-8, 1 - 1e-8)
        return {"rows": len(y), "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
                "brier": float(np.mean((p - y) ** 2)), "accuracy_at_0_5": float(np.mean((p >= .5) == y))}
    return {"rows": len(y), "mae": float(np.mean(np.abs(p - y))),
            "rmse": float(np.sqrt(np.mean((p - y) ** 2))), "bias": float(np.mean(p - y))}


def _platt(probabilities, labels):
    p, y = np.asarray(probabilities), np.asarray(labels)
    if len(y) < 20 or min(np.sum(y == 0), np.sum(y == 1)) < 5:
        return {"method": "platt", "status": "INSUFFICIENT_CALIBRATION_CLASS_SUPPORT", "rows": len(y)}
    x = np.column_stack([np.log(np.clip(p, 1e-6, 1 - 1e-6) / np.clip(1 - p, 1e-6, 1)), np.ones(len(p))])
    beta = np.asarray([1., 0.])
    for _ in range(100):
        fitted = 1 / (1 + np.exp(-np.clip(x @ beta, -40, 40)))
        weights = np.maximum(fitted * (1 - fitted), 1e-6)
        ridge = np.diag([1e-3, 1e-6])
        step = np.linalg.solve(x.T @ (weights[:, None] * x) + ridge, x.T @ (fitted - y) + ridge @ beta)
        beta -= step
        if float(np.max(np.abs(step))) < 1e-7:
            break
    return {"method": "platt", "status": "FIT_DEDICATED_CALIBRATION", "rows": len(y),
            "slope": float(beta[0]), "intercept": float(beta[1]), "regularization": .001}


def _calibrate(probabilities, calibration):
    p = np.clip(np.asarray(probabilities, dtype=float), 1e-8, 1 - 1e-8)
    if calibration.get("method") == "platt" and calibration.get("status") == "FIT_DEDICATED_CALIBRATION":
        logits = np.log(p / (1 - p)) * calibration["slope"] + calibration["intercept"]
        return 1 / (1 + np.exp(-np.clip(logits, -40, 40)))
    return p


def _conformal(predictions, labels, alpha):
    residuals = np.sort(np.abs(np.asarray(labels) - np.asarray(predictions)))
    rank = math.ceil((len(residuals) + 1) * (1 - alpha))
    if not len(residuals) or rank > len(residuals):
        return {"method": "split_conformal_absolute_residual", "status": "INSUFFICIENT_FINITE_SAMPLE_SUPPORT", "rows": len(residuals), "alpha": alpha}
    return {"method": "split_conformal_absolute_residual", "status": "FIT_DEDICATED_CALIBRATION", "rows": len(residuals),
            "alpha": alpha, "radius": float(residuals[rank - 1]), "order_statistic": rank,
            "scope": "Marginal row interval under exchangeability assumptions; NBA time drift and within-game dependence may invalidate nominal coverage"}


def _external_fit(backend, run_dir, columns, train, tune, kind, time_limit, num_cpus):
    """Optional adapters. Exceptions are recorded; baselines remain usable."""
    xtrain, xtune = _feature_matrix(train, columns), _feature_matrix(tune, columns)
    ytrain, ytune = np.asarray([r["label"] for r in train]), np.asarray([r["label"] for r in tune])
    if kind == "binary" and len(set(ytrain)) < 2:
        raise ValueError("Classifier training requires both classes")
    if backend == "autogluon":
        import pandas as pd
        from autogluon.tabular import TabularPredictor
        label = "__training_label__"
        df, validation = pd.DataFrame(xtrain, columns=columns), pd.DataFrame(xtune, columns=columns)
        df[label], validation[label] = ytrain, ytune
        path = run_dir / "autogluon"
        predictor = TabularPredictor(label=label, problem_type="binary" if kind == "binary" else "regression",
            eval_metric="log_loss" if kind == "binary" else "mean_absolute_error", path=str(path), verbosity=1)
        predictor.fit(df, tuning_data=validation, time_limit=time_limit, presets="medium_quality",
            hyperparameters={"RF": {"n_estimators": 20}, "XT": {"n_estimators": 20},
                             "GBM": {"num_boost_round": 50, "num_leaves": 7, "max_depth": 3}},
            num_cpus=num_cpus, num_gpus=0, num_bag_folds=0, num_stack_levels=0,
            fit_weighted_ensemble=True, calibrate=False, calibrate_decision_threshold=False)
        diagnostics = {}
        try:
            diagnostics["leaderboard"] = json.loads(predictor.leaderboard(silent=True).to_json(orient="records"))
        except Exception as exc:
            diagnostics["leaderboard_unavailable"] = f"{type(exc).__name__}: {exc}"
        try:
            importance = predictor.feature_importance(data=validation, subsample_size=100,
                time_limit=5, num_shuffle_sets=1, silent=True)
            diagnostics["tuning_feature_importance"] = json.loads(importance.reset_index().to_json(orient="records"))
            diagnostics["feature_importance_scope"] = "TUNING_ONLY, at most 100 rows/5 seconds, one shuffle; descriptive sensitivity, not causal importance"
        except Exception as exc:
            diagnostics["feature_importance_unavailable"] = f"{type(exc).__name__}: {exc}"
        def predict(rows):
            frame = pd.DataFrame(_feature_matrix(rows, columns), columns=columns)
            result = predictor.predict_proba(frame)[1] if kind == "binary" else predictor.predict(frame)
            return np.asarray(result, dtype=float)
        return {"backend": backend, "path": "autogluon", "package_version": importlib.metadata.version("autogluon.tabular"),
                "candidates": "RF/XT 20 trees; GBM <=50 rounds; tuning-only weighted ensemble; no XGB duplicate, neural nets, bagging or stacking",
                "diagnostics": diagnostics}, predict
    if backend == "xgboost":
        import xgboost as xgb
        dtrain = xgb.DMatrix(xtrain, label=ytrain, feature_names=columns)
        dtune = xgb.DMatrix(xtune, label=ytune, feature_names=columns)
        deadline = time.monotonic() + time_limit
        class Deadline(xgb.callback.TrainingCallback):
            def __init__(self, stop_at):
                self.stop_at = stop_at
            def after_iteration(self, model, epoch, evals_log):
                return time.monotonic() >= self.stop_at
        params = {"objective": "binary:logistic" if kind == "binary" else "reg:squarederror",
                  "eval_metric": "logloss" if kind == "binary" else "mae", "nthread": num_cpus,
                  "max_depth": 3, "eta": .05, "tree_method": "hist", "max_bin": 64,
                  "subsample": .9, "colsample_bytree": .9, "seed": 42}
        trials, fitted = [], []
        for index, depth in enumerate((2, 3)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            trial_params = {**params, "max_depth": depth}
            stop_at = time.monotonic() + remaining / (2 - index)
            trial = xgb.train(trial_params, dtrain, num_boost_round=150, evals=[(dtune, "tune")],
                early_stopping_rounds=10, callbacks=[Deadline(stop_at)], verbose_eval=False)
            score = float(trial.best_score)
            fitted.append((score, depth, trial, trial_params))
            trials.append({"max_depth": depth, "tuning_loss": score, "best_iteration_count": int(trial.best_iteration) + 1})
        if not fitted:
            raise ValueError("XGBoost tuning budget expired before a model was fitted")
        _, _, booster, params = min(fitted, key=lambda t: (t[0], t[1]))
        best = int(booster.best_iteration) + 1
        booster.save_model(str(run_dir / "xgboost.json"))
        def predict(rows):
            return booster.predict(xgb.DMatrix(_feature_matrix(rows, columns), feature_names=columns), iteration_range=(0, best))
        return {"backend": backend, "path": "xgboost.json", "best_iteration_count": best,
                "parameters": params, "package_version": xgb.__version__,
                "tuning_trials": trials, "tuning_scope": "Depth 2/3 compared on tuning fold only, bounded by shared backend time budget",
                "feature_importance_gain": {k: float(v) for k, v in booster.get_score(importance_type="gain").items()},
                "feature_importance_scope": "Training-tree split gain; not causal importance"}, predict
    raise ValueError("Unknown optional ML backend")


def train_run(bundle, target, output_dir="output/ml", backends=("autogluon", "xgboost"), time_limit=60,
              num_cpus=1, min_games=20, alpha=.1, split_fractions=(.6, .15, .1, .15)):
    from .ml_features import TARGET_REGISTRY, chronological_splits, rows_for_target
    if target not in TARGET_REGISTRY:
        raise ValueError("Unknown registered ML target")
    if not 1 <= num_cpus <= 2 or not 5 <= time_limit <= 600 or not .01 <= alpha <= .4:
        raise ValueError("Use 1-2 CPU, 5-600 seconds, and alpha in [0.01,0.4]")
    if any(b not in {"autogluon", "xgboost"} for b in backends) or len(set(backends)) != len(backends):
        raise ValueError("Optional backends must be unique autogluon/xgboost entries")
    converted = rows_for_target(bundle, target)
    rows = converted["rows"] if isinstance(converted, dict) else list(converted)
    if len({r["game_id"] for r in rows}) < min_games:
        raise ValueError(f"Insufficient independent game groups: require at least {min_games}")
    kind, scope = TARGET_REGISTRY[target]["kind"], TARGET_REGISTRY[target]["scope"]
    for row in rows:
        if not _number(row.get("label")) or kind == "binary" and row["label"] not in (0, 1):
            raise ValueError("Target labels must be finite; binary labels must be zero or one")
        if kind != "binary" and (target != "margin" and row["label"] < 0 or target == "minutes" and row["label"] > 100):
            raise ValueError("Target label is outside its declared numerical domain")
        if not utc(row["cutoff"]) < utc(row["label_completed_at"]) <= utc(row["label_known_at"]):
            raise ValueError("Require pregame cutoff < label completion <= label knowledge time")
    splits = chronological_splits(rows, fractions=split_fractions)
    if any(not splits.get(k) for k in ("train", "tune", "calibration", "test")):
        raise ValueError(f"Chronological split unavailable after purge: {splits.get('audit')}")
    columns = sorted({name for row in rows for name in row["features"]})
    if not columns or any(UNSAFE_FEATURE.search(name) for name in columns):
        raise ValueError("A nonempty pregame feature allowlist without outcome/label/future fields is required")
    _feature_matrix(rows, columns)
    root = Path(output_dir).resolve()
    run_id = _now().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:12]
    run_dir = root / "models" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    train, tune, calibration_rows, test = (splits[k] for k in ("train", "tune", "calibration", "test"))
    baselines = _fit_baselines(train, target, scope)
    _json_new(run_dir / "baselines.json", baselines)
    predictors = {"historical_baseline": lambda rs: _domain(_baseline_predict(baselines, rs), target, kind),
                  "recent_baseline": lambda rs: _domain(_baseline_predict(baselines, rs, True), target, kind)}
    candidates = {name: {"backend": name, "status": "FIT"} for name in predictors}
    errors = []
    for index, backend in enumerate(backends):
        remaining = max(0, time_limit - (time.monotonic() - started))
        if remaining < 1:
            candidates[backend] = {"backend": backend, "status": "TIME_BUDGET_EXHAUSTED"}
            continue
        budget = max(1, remaining / (len(backends) - index))
        try:
            metadata, predictor = _external_fit(backend, run_dir, columns, train, tune, kind, budget, num_cpus)
            raw_predictor = predictor
            predictor = lambda rs, p=raw_predictor: _domain(p(rs), target, kind)
            probe = np.asarray(predictor(tune), dtype=float)
            if len(probe) != len(tune) or not np.isfinite(probe).all():
                raise ValueError("Backend produced invalid tuning predictions")
            predictors[backend], candidates[backend] = predictor, {**metadata, "status": "FIT"}
        except (Exception, MemoryError) as exc:
            candidates[backend] = {"backend": backend, "status": "UNAVAILABLE_OR_FAILED", "reason": f"{type(exc).__name__}: {exc}"}
            errors.append(candidates[backend])
    metric = "log_loss" if kind == "binary" else "mae"
    tuning_labels = [r["label"] for r in tune]
    for name, predict in predictors.items():
        candidates[name]["tuning_metrics"] = _metrics(tuning_labels, predict(tune), kind)
    champion = min(predictors, key=lambda name: (candidates[name]["tuning_metrics"][metric], list(predictors).index(name)))
    # Champion is frozen before calibration labels or final-test scoring are used.
    predict = predictors[champion]
    cal_predictions = predict(calibration_rows)
    cal_labels = [r["label"] for r in calibration_rows]
    fitted_calibration = _platt(cal_predictions, cal_labels) if kind == "binary" else _conformal(cal_predictions, cal_labels, alpha)
    final_predictions = predict(test)
    if kind == "binary":
        final_predictions = _calibrate(final_predictions, fitted_calibration)
    final_metrics = _metrics([r["label"] for r in test], final_predictions, kind)
    reliability_bins = []
    if kind == "binary":
        labels = np.asarray([r["label"] for r in test])
        bin_ids = np.minimum(9, (np.clip(final_predictions, 0, 1) * 10).astype(int))
        for index in range(10):
            selected = bin_ids == index
            if np.any(selected):
                reliability_bins.append({"lower": index / 10, "upper": (index + 1) / 10,
                    "count": int(np.sum(selected)), "predicted_mean": float(np.mean(final_predictions[selected])),
                    "observed_rate": float(np.mean(labels[selected])),
                    "independent_games": len({r["game_id"] for r, flag in zip(test, selected) if flag})})
        final_metrics["expected_calibration_error"] = sum(b["count"] * abs(b["predicted_mean"] - b["observed_rate"]) for b in reliability_bins) / len(test)
    # The champion and calibration have already been frozen. Challenger scores
    # are diagnostics only and cannot revise the selected champion.
    comparison = {}
    for name, candidate_predict in predictors.items():
        raw_metrics = _metrics([r["label"] for r in test], candidate_predict(test), kind)
        candidates[name]["raw_final_test_metrics"] = raw_metrics
        candidates[name]["final_test_metrics"] = raw_metrics
        comparison[name] = raw_metrics
    if kind == "regression" and fitted_calibration.get("radius") is not None:
        radius = fitted_calibration["radius"]
        lower, upper = _intervals(final_predictions, radius, target)
        test_labels = np.asarray([r["label"] for r in test])
        final_metrics["interval_coverage"] = float(np.mean((lower <= test_labels) & (test_labels <= upper)))
        final_metrics["nominal_interval_coverage"] = 1 - alpha
        final_metrics["interval_mean_width"] = float(np.mean(upper - lower))
    baseline_test = {name: _metrics([r["label"] for r in test], predictors[name](test), kind)
                     for name in ("historical_baseline", "recent_baseline")}
    dispersion = {}
    for name in ("train", "tune", "calibration", "test"):
        labels = np.asarray([r["label"] for r in splits[name]])
        dispersion[name] = {"rows": len(labels), "mean": float(labels.mean()), "variance": float(labels.var()),
                            "zero_fraction": float(np.mean(labels == 0)), "distinct_labels": len(set(labels))}
    training_matrix = _feature_matrix(train, columns)
    feature_summary = {}
    for index, column in enumerate(columns):
        finite = training_matrix[np.isfinite(training_matrix[:, index]), index]
        feature_summary[column] = {"rows": len(train), "observed_rows": len(finite),
            "mean": float(finite.mean()) if len(finite) else None,
            "std": float(finite.std()) if len(finite) else None,
            "min": float(finite.min()) if len(finite) else None,
            "max": float(finite.max()) if len(finite) else None}
    test_games = len({r["game_id"] for r in test})
    final_metrics["independent_games"] = test_games
    for metrics in comparison.values():
        metrics["independent_games"] = test_games
    readiness = {"minimum_heldout_game_groups": 20, "heldout_game_groups": test_games,
        "heldout_group_support_passed": test_games >= 20,
        "dedicated_calibration_fit": fitted_calibration.get("status") == "FIT_DEDICATED_CALIBRATION",
        "interval_coverage_screen_passed": (abs(final_metrics["interval_coverage"] - (1 - alpha)) <= .1)
            if "interval_coverage" in final_metrics else None,
        "binary_calibration_screen_passed": final_metrics["expected_calibration_error"] <= .1 if kind == "binary" else None,
        "prospective_validation": "NOT_ESTABLISHED", "production_promotion": "BLOCKED"}
    _json_new(run_dir / "frozen-evaluation.json", {"champion_selected_using": "TUNE_ONLY", "champion": champion,
        "test": [{"game_id": r["game_id"], "entity_id": r.get("entity_id"), "label": r["label"], "prediction": float(p)}
                 for r, p in zip(test, final_predictions)], "calibration": fitted_calibration})
    files = {}
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            _inside(run_dir, path)
            if path.is_symlink():
                raise ValueError("Symlink model artifacts are not trusted")
            files[str(path.relative_to(run_dir)).replace("\\", "/")] = _sha(path)
    audit = splits["audit"]
    strict_eligible = bool(audit.get("production_eligible")) and not audit.get("knowledge_purge_ignored_for_replay") and all(
        r.get("production_eligible") is True and r.get("point_in_time_mode") == "strict" for r in rows)
    cohort = "strict_point_in_time" if strict_eligible else "retrospective_research"
    if any(r.get("synthetic") is True or r.get("provenance", {}).get("synthetic") is True for r in rows):
        cohort = "synthetic_software_fixture"
    manifest = {"id": run_id, "version": run_id, "engine_version": VERSION, "created_at": _now().isoformat(), "target": target,
        "scope": scope, "kind": kind, "status": "SHADOW", "production_eligible": False,
        "training_point_in_time_eligible": strict_eligible and cohort != "synthetic_software_fixture",
        "feature_columns": columns, "feature_schema_hash": digest(columns), "input_hash": digest(rows),
        "feature_audit": bundle.get("audit", {}) if isinstance(bundle, dict) else {},
        "training_source_code_sha256": {name: _sha(Path(__file__).with_name(name)) for name in ("ml_engine.py", "ml_features.py")},
        "feature_training_summary": feature_summary,
        "split_audit": audit, "splits": {k: {"rows": len(splits[k]), "games": len({r['game_id'] for r in splits[k]}),
            "first_cutoff": min(r["cutoff"] for r in splits[k]), "last_cutoff": max(r["cutoff"] for r in splits[k])}
            for k in ("train", "tune", "calibration", "test")},
        "champion": champion, "selection_metric": metric, "selection_scope": "TUNING_ONLY",
        "candidates": candidates, "calibration": fitted_calibration,
        "final_test_metrics": final_metrics, "metrics": final_metrics, "comparison": comparison,
        "baseline_final_test_metrics": baseline_test, "evaluation_cohort": cohort,
        "label_dispersion": dispersion, "readiness_gates": readiness,
        "reliability_bins": reliability_bins,
        "target_modeling_note": "Target-specific squared-error regression or binary logistic; no universal Poisson/count independence assumption. Dispersion/zero mass are reported for count-target review.",
        "prediction_bounds": {"lower": None if target == "margin" else 0, "upper": 1 if kind == "binary" else 100 if target == "minutes" else None},
        "files": files, "origin": "LOCAL_TRAINING_ONLY", "time_limit_seconds": time_limit,
        "elapsed_seconds": time.monotonic() - started, "num_cpus": num_cpus, "errors": errors,
        "limitations": ["Retrospective scores are not prospective calibration or strategy approval.",
            "Split conformal relies on exchangeability; time drift and correlated within-game rows limit nominal coverage.",
            "Classifier calibration requires independent calibration class support; otherwise probabilities remain uncalibrated.",
            "Training clocks and hashes provide local provenance, not independently authenticated upstream data.",
            "CPU/time limits constrain training work; optional package imports and serialization can add wall time.",
            "AutoGluon may deserialize trusted local pickle files; never load downloaded or untrusted model directories.",
            "Targets are trained independently; their predictions are not a jointly coherent player box score, team score or SGP distribution.",
            "No joint SGP price, prospective edge, trade authority or automatic production promotion."]}
    _json_new(run_dir / "manifest.json", manifest)
    _json_new(run_dir / "manifest.sha256.json", {"sha256": _sha(run_dir / "manifest.json")})
    registry_event(output_dir, "shadow", run_id)
    return manifest


def load_manifest(output_dir, model_id):
    if not isinstance(model_id, str) or not re.fullmatch(r"\d{8}T\d{6}-[a-f0-9]{12}", model_id):
        raise ValueError("Invalid local model version ID")
    root = Path(output_dir).resolve()
    run_dir = _inside(root, root / "models" / model_id)
    manifest_path = _inside(run_dir, run_dir / "manifest.json")
    receipt = json.loads((run_dir / "manifest.sha256.json").read_text())
    if _sha(manifest_path) != receipt["sha256"]:
        raise ValueError("Model manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("id") != model_id or manifest.get("origin") != "LOCAL_TRAINING_ONLY":
        raise ValueError("Only matching locally trained artifacts may be loaded")
    for relative, expected in manifest["files"].items():
        path = _inside(run_dir, run_dir / relative)
        if path.is_symlink() or _sha(path) != expected:
            raise ValueError("Model artifact checksum/path verification failed")
    return manifest


def list_runs(output_dir="output/ml"):
    directory = Path(output_dir) / "models"
    if not directory.exists():
        return []
    if directory.is_symlink():
        return [{"id": None, "status": "CORRUPT", "reason": "Models directory is a symlink and cannot be trusted", "production_eligible": False}]
    runs = []
    for path in sorted(directory.iterdir(), reverse=True):
        if not path.is_dir() and not path.is_symlink():
            continue
        if not path.is_symlink() and not (path / "manifest.json").exists() and not (path / "manifest.sha256.json").exists():
            continue  # A failed/interrupted fit is not a registered usable run.
        try:
            if path.is_symlink():
                raise ValueError("Symlink model directories are not trusted")
            runs.append(load_manifest(output_dir, path.name))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            runs.append({"id": path.name, "status": "CORRUPT", "reason": f"{type(exc).__name__}: {exc}",
                         "production_eligible": False, "origin": "UNVERIFIED_ARTIFACT_DO_NOT_LOAD"})
    return runs


def list_run_failures(output_dir="output/ml"):
    """Inventory incomplete fits separately, without loading their artifacts."""
    directory = Path(output_dir) / "models"
    if not directory.exists() or directory.is_symlink():
        return []
    return [{"id": path.name, "status": "INCOMPLETE", "production_eligible": False,
             "reason": "Training did not publish an immutable manifest/receipt; partial files are not loadable"}
            for path in sorted(directory.iterdir(), reverse=True)
            if path.is_dir() and not path.is_symlink() and not (path / "manifest.json").exists()
            and not (path / "manifest.sha256.json").exists()]


def predict_run(output_dir, model_id, rows, candidate=None):
    manifest = load_manifest(output_dir, model_id)
    columns = manifest["feature_columns"]
    _feature_matrix(rows, columns)
    run_dir = Path(output_dir).resolve() / "models" / model_id
    champion = candidate or manifest["champion"]
    if champion not in manifest["candidates"] or manifest["candidates"][champion].get("status") != "FIT":
        raise ValueError("Requested model candidate is unavailable or was not fitted")
    if champion in {"historical_baseline", "recent_baseline"}:
        params = json.loads((run_dir / "baselines.json").read_text())
        predictions = _baseline_predict(params, rows, champion == "recent_baseline")
    elif champion == "autogluon":
        import pandas as pd
        from autogluon.tabular import TabularPredictor
        predictor = TabularPredictor.load(str(_inside(run_dir, run_dir / manifest["candidates"][champion]["path"])))
        frame = pd.DataFrame(_feature_matrix(rows, columns), columns=columns)
        predictions = np.asarray(predictor.predict_proba(frame)[1] if manifest["kind"] == "binary" else predictor.predict(frame))
    elif champion == "xgboost":
        import xgboost as xgb
        booster = xgb.Booster(params={"nthread": manifest["num_cpus"]})
        booster.load_model(str(_inside(run_dir, run_dir / manifest["candidates"][champion]["path"])))
        predictions = booster.predict(xgb.DMatrix(_feature_matrix(rows, columns), feature_names=columns),
            iteration_range=(0, manifest["candidates"][champion]["best_iteration_count"]))
    else:
        raise ValueError("Unknown saved champion backend")
    calibration = manifest["calibration"] if champion == manifest["champion"] else {
        "status": "UNAVAILABLE_CANDIDATE_NOT_CALIBRATED", "note": "Dedicated calibration belongs to the frozen champion only"}
    if manifest["kind"] == "binary":
        predictions = _calibrate(predictions, calibration)
    predictions = _domain(predictions, manifest["target"], manifest["kind"])
    if not np.isfinite(predictions).all():
        raise ValueError("Model produced nonfinite predictions")
    radius = calibration.get("radius")
    intervals = list(zip(*_intervals(predictions, radius, manifest["target"]))) if radius is not None else [None] * len(rows)
    return {"model_id": model_id, "target": manifest["target"], "status": "SHADOW_RESEARCH_ONLY",
        "champion": manifest["champion"], "candidate": champion, "candidate_override": candidate is not None,
        "predictions": [{"game_id": r.get("game_id"), "entity_id": r.get("entity_id"),
            "prediction": float(p), "probability": float(p) if manifest["kind"] == "binary" else None,
            "interval": [float(v) for v in interval] if interval is not None else None}
            for r, p, interval in zip(rows, predictions, intervals)], "calibration": calibration,
        "production_eligible": False, "execution": "NONE"}


def registry_events(output_dir="output/ml"):
    path = Path(output_dir) / "registry.jsonl"
    events, previous = [], None
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        given = event.pop("hash")
        if digest(event) != given or event.get("previous_hash") != previous:
            raise ValueError("Model registry hash chain verification failed")
        event["hash"], previous = given, given
        events.append(event)
    return events


@contextmanager
def _registry_file_lock(output_dir):
    path = Path(output_dir) / "registry.lock"
    with path.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            if not stream.read(1):
                stream.write(b"0"); stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("Another process is updating the model registry") from exc
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def registry_event(output_dir, action, model_id, evidence=None):
    if action not in {"shadow", "promote", "rollback"}:
        raise ValueError("Unknown model registry action")
    manifest = load_manifest(output_dir, model_id)
    evidence_summary = None
    if action == "promote":
        gates = manifest.get("readiness_gates", {})
        if not manifest.get("training_point_in_time_eligible") or manifest["calibration"].get("status") != "FIT_DEDICATED_CALIBRATION" or not gates.get("heldout_group_support_passed") or gates.get("interval_coverage_screen_passed") is False or gates.get("binary_calibration_screen_passed") is False:
            raise ValueError("Production promotion requires strict point-in-time training and supported dedicated calibration")
        records = evidence.get("records", []) if isinstance(evidence, dict) else []
        evidence_summary = promotion_evidence_summary(manifest, records)
    with _registry_lock, _registry_file_lock(output_dir):
        events = registry_events(output_dir)
        if action == "rollback" and not any(e["action"] == "promote" and e["model_id"] == model_id for e in events):
            raise ValueError("Rollback target must be a previously promoted model version")
        event = {"action": action, "model_id": model_id, "created_at": _now().isoformat(),
            "previous_hash": events[-1]["hash"] if events else None,
            "evidence_hash": digest(evidence) if evidence is not None else None,
            "evidence_summary": evidence_summary,
            "promotion_policy": "Explicit operator action only: strict training/calibration/heldout gates, >=100 prospective game groups, >=20 Eastern game days, >=5% paired mean loss improvement, day-block bootstrap 95% lower improvement >0. Local operator evidence is trusted input, not authenticated upstream proof or trading approval."}
        event["hash"] = digest(event)
        path = Path(output_dir) / "registry.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(canonical(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return event


def promotion_evidence_summary(manifest, records):
    """Conservative fixed research gate; never called to auto-promote a run."""
    if len({r.get("game_id") for r in records}) < 100:
        raise ValueError("Promotion requires at least 100 distinct prospectively frozen audited game outcomes")
    grouped = defaultdict(list)
    for row in records:
        if not row.get("game_id") or row.get("model_id") != manifest["id"] or row.get("production_eligible") is not True or not (
            utc(manifest["created_at"]) <= utc(row["prediction_created_at"]) < utc(row["tipoff"]) < utc(row["outcome_known_at"]) <= _now()):
            raise ValueError("Invalid prospective promotion evidence timing or source eligibility")
        if not all(_number(row.get(k)) and row[k] >= 0 for k in ("model_loss", "baseline_loss")):
            raise ValueError("Promotion evidence requires finite nonnegative paired losses")
        grouped[row["game_id"]].append(row)
    days = defaultdict(list)
    for game_id, rows in grouped.items():
        day_set = {utc(r["tipoff"]).astimezone(ZoneInfo("America/New_York")).date().isoformat() for r in rows}
        if len(day_set) != 1:
            raise ValueError("One promotion game cannot span conflicting Eastern game-day identities")
        days[next(iter(day_set))].append((float(np.mean([r["model_loss"] for r in rows])),
                                         float(np.mean([r["baseline_loss"] for r in rows]))))
    if len(days) < 20:
        raise ValueError("Promotion requires at least 20 independent Eastern game-day cohorts")
    day_values = np.asarray([np.mean(days[day], axis=0) for day in sorted(days)])
    model_loss, baseline_loss = day_values.mean(axis=0)
    if model_loss > .95 * baseline_loss or baseline_loss <= 0:
        raise ValueError("Promotion evidence has not passed the fixed paired baseline improvement gate")
    improvements = day_values[:, 1] - day_values[:, 0]
    draws = np.random.default_rng(42).integers(0, len(improvements), size=(5000, len(improvements)))
    bootstrap = improvements[draws].mean(axis=1)
    lower, upper = (float(v) for v in np.quantile(bootstrap, [.025, .975]))
    if lower <= 0:
        raise ValueError("Day-block bootstrap uncertainty gate failed: 95% lower paired loss improvement must exceed zero")
    return {"distinct_game_groups": len(grouped), "eastern_game_day_cohorts": len(days),
        "input_rows": len(records), "model_loss_day_weighted": float(model_loss),
        "baseline_loss_day_weighted": float(baseline_loss),
        "relative_loss_improvement": float(1 - model_loss / baseline_loss),
        "paired_loss_improvement": float(improvements.mean()),
        "paired_loss_improvement_interval_95": [lower, upper],
        "uncertainty_method": "Paired Eastern-day block percentile bootstrap; game-mean then equal-day weighted",
        "bootstrap_draws": 5000, "bootstrap_seed": 42,
        "source_trust": "LOCAL_OPERATOR_INPUT_NOT_INDEPENDENTLY_AUTHENTICATED",
        "limitations": "Fixed research approval screen; day dependence, drift, multiplicity and source assertions still limit inference. No strategy or trading authorization."}
