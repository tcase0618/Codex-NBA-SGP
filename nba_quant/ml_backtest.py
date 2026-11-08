"""Reproducible expanding/rolling NBA research evaluation with disjoint test days."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import math

from .schemas import utc
from .store import digest

EASTERN = ZoneInfo("America/New_York")


def game_day(row):
    return utc(row["tipoff"]).astimezone(EASTERN).date().isoformat()


def walk_forward_plan(rows, initial_days=20, test_days=5, step_days=None, window_days=None):
    step_days = test_days if step_days is None else step_days
    for value in (initial_days, test_days, step_days):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("Fold sizes must be positive integers")
    if initial_days < 6 or step_days < test_days:
        raise ValueError("At least six initial days and non-overlapping test windows are required")
    if window_days is not None and (isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 6):
        raise ValueError("A rolling training window requires at least six days")
    days = sorted({game_day(row) for row in rows})
    folds = []
    for start in range(initial_days, len(days)-test_days+1, step_days):
        first = max(0, start-window_days) if window_days is not None else 0
        selected_days = days[first:start+test_days]
        historical_count = start-first
        training_count = max(1, int(historical_count*.7))
        tuning_count = max(1, int(historical_count*.15))
        calibration_count = historical_count-training_count-tuning_count
        if calibration_count < 1:
            raise ValueError("Insufficient calibration days")
        # Nudge division upward so integer rounding in the shared splitter keeps
        # the requested whole-day counts. The residual final fraction sums to1.
        fractions = [math.nextafter(n/len(selected_days), math.inf)
                     for n in (training_count, tuning_count, calibration_count)]
        fractions.append(1-sum(fractions))
        folds.append({"index": len(folds), "days": selected_days,
            "test_days": days[start:start+test_days], "fractions": fractions,
            "mode": "rolling" if window_days is not None else "expanding"})
    return folds


def run_walk_forward(bundle, target, output_dir="output/ml/backtests", initial_days=20,
                     test_days=5, step_days=None, window_days=None, backends=("autogluon", "xgboost"),
                     time_limit=60, num_cpus=1):
    from .ml_features import rows_for_target, chronological_splits
    from .ml_engine import train_run
    rows = rows_for_target(bundle, target)
    plan = walk_forward_plan(rows, initial_days, test_days, step_days, window_days)
    if not plan:
        raise ValueError("Insufficient observed game-day cohorts for a complete walk-forward fold")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    results, tested_games = [], set()
    for fold in plan:
        fold_rows = [r for r in bundle["rows"] if game_day(r) in fold["days"]]
        target_rows = rows_for_target(fold_rows, target)
        split = chronological_splits(target_rows, fractions=fold["fractions"])
        actual_days = sorted({game_day(r) for r in split["test"]})
        if actual_days != fold["test_days"]:
            raise ValueError("Chronological fold allocation differs from requested holdout days")
        test_games = {r["game_id"] for r in split["test"]}
        if tested_games & test_games:
            raise ValueError("Repeated final-test games cannot be pooled as independent evidence")
        tested_games.update(test_games)
        manifest = train_run({**bundle, "rows": fold_rows}, target, output_dir=root,
            backends=backends, time_limit=time_limit, num_cpus=num_cpus, split_fractions=fold["fractions"])
        results.append({"plan": fold, "model_id": manifest["id"],
            "champion": manifest["champion"], "metrics": manifest["final_test_metrics"],
            "comparison": manifest["comparison"], "splits": manifest["splits"],
            "cohort": manifest["evaluation_cohort"], "production_eligible": False})
    # Every fold retains its own calibration and metrics. Do not average row
    # counts into an inflated game denominator or select a model from test wins.
    result = {"target": target, "folds": results, "independent_test_games": len(tested_games),
        "test_games": sorted(tested_games), "overlapping_holdouts": False,
        "input_hash": digest(bundle), "mode": "rolling" if window_days else "expanding",
        "status": "RESEARCH_ONLY", "production_eligible": False,
        "note": "Later training can use earlier completed test games; champions are selected within each tuning fold."}
    key = digest(result)
    path = root / f"backtest-{key}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    return {**result, "report_path": str(path)}
