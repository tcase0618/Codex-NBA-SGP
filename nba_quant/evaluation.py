"""Frozen-forecast scoring; keep synthetic and retrospective cohorts separate."""
import math

import numpy as np

from .schemas import utc
from .store import digest


def crps(samples, observed):
    x = np.sort(np.asarray(samples, dtype=float))
    if x.ndim != 1 or not len(x) or not np.isfinite(x).all() or not math.isfinite(observed):
        raise ValueError("CRPS requires finite, nonempty one-dimensional samples and a finite observation")
    n = len(x)
    return float(np.mean(abs(x - observed)) - np.sum((2 * np.arange(1, n + 1) - n - 1) * x) / n**2)


def score_forecast(forecast, dataset, game):
    request, result = forecast["request"], forecast["result"]
    if (game["home_id"], game["away_id"], utc(game["tipoff"])) != (
        request["home_id"], request["away_id"], utc(request["tipoff"])):
        raise ValueError("Outcome game must match teams and exact tipoff")
    if utc(game["known_at"]) <= utc(request["cutoff"]):
        raise ValueError("Outcome must be unknown at forecast cutoff")
    if dataset["synthetic"] != forecast["synthetic"]:
        raise ValueError("Cannot mix synthetic forecasts and observed outcomes")
    h, a = game["home_score"], game["away_score"]
    probability = result["home_win_probability"]
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("Win probability must be finite and between zero and one")
    p = min(1 - 1e-12, max(1e-12, probability))
    y = int(h > a)
    predictions = {"home_score": h, "away_score": a, "margin": h - a, "total": h + a}
    scores = np.asarray(result["score_samples"])
    samples = {"home_score": scores[:, 0], "away_score": scores[:, 1],
               "margin": scores[:, 0] - scores[:, 1], "total": scores.sum(axis=1)}
    metrics = {"brier": (probability - y)**2, "log_loss": -(y * math.log(p) + (1 - y) * math.log(1 - p))}
    for key, observed in predictions.items():
        summary = result[key]
        metrics[key] = {"absolute_error": abs(summary["mean"] - observed),
                        "squared_error": (summary["mean"] - observed)**2,
                        "coverage_80": int(summary["p10"] <= observed <= summary["p90"]),
                        "crps": crps(samples[key], observed)}
    actual_players = {b["player_id"]: {**b, "reb": b["oreb"] + b["dreb"]} for b in game["boxes"]}
    player_scores = []
    missing_player_ids = []
    for player in result["players"]:
        actual = actual_players.get(player["id"])
        if actual is None:
            missing_player_ids.append(player["id"])
            continue  # missing outcome row is not a zero-stat DNP
        if actual["team_id"] != player["team_id"]:
            raise ValueError("Outcome player team differs from frozen forecast roster")
        for stat, distribution in player["stats"].items():
            player_scores.append({"player_id": player["id"], "stat": stat,
                "absolute_error": abs(distribution["mean"] - actual[stat]),
                "coverage_80": int(distribution["p10"] <= actual[stat] <= distribution["p90"]),
                "crps": crps(player["samples"][stat], actual[stat])})
    prospective = utc(forecast["created_at"]) < utc(request["tipoff"]) and not forecast["model"]["retrospective_source"]
    game_key = digest({"home_id": request["home_id"], "away_id": request["away_id"],
                       "tipoff": utc(request["tipoff"]).isoformat()})
    return {"game_id": game["id"], "game_key": game_key, "dataset_id": dataset["id"],
            "source": dataset["source"], "source_url": dataset["source_url"],
            "known_at": game["known_at"], "synthetic": forecast["synthetic"],
            "cohort": "synthetic" if forecast["synthetic"] else "prospective" if prospective else "retrospective",
            "home_win_probability": result["home_win_probability"], "home_won": y,
            "actual": predictions, "metrics": metrics, "player_scores": player_scores,
            "missing_player_ids": missing_player_ids,
            "players_scored": len(result["players"]) - len(missing_player_ids)}


def aggregate(outcomes):
    if not outcomes:
        return {"status": "NOT MEASURABLE", "forecasts_scored": 0, "distinct_games": 0, "metrics": {}, "cohorts": {},
                "note": "No matching outcomes recorded. No accuracy or calibration claim is supported."}
    cohorts = {}
    for cohort in ("synthetic", "retrospective", "prospective"):
        rows = [o for o in outcomes if o["cohort"] == cohort]
        if not rows:
            continue
        metrics = {k: float(np.mean([o["metrics"][k] for o in rows])) for k in ("brier", "log_loss")}
        for key in ("home_score", "away_score", "margin", "total"):
            metrics[key] = {"mae": float(np.mean([o["metrics"][key]["absolute_error"] for o in rows])),
                "rmse": float(np.sqrt(np.mean([o["metrics"][key]["squared_error"] for o in rows]))),
                "coverage_80": float(np.mean([o["metrics"][key]["coverage_80"] for o in rows])),
                "crps": float(np.mean([o["metrics"][key]["crps"] for o in rows]))}
        player_metrics = {}
        for stat in ("pts", "reb", "ast", "fg3m", "minutes", "stl", "blk", "tov", "fga", "fta"):
            ps = [p for o in rows for p in o["player_scores"] if p["stat"] == stat]
            if ps:
                player_metrics[stat] = {key: float(np.mean([p[key] for p in ps])) for key in ("absolute_error", "coverage_80", "crps")}
        bins = []
        for index in range(10):
            selected = [o for o in rows if min(9, int(o["home_win_probability"] * 10)) == index]
            if selected:
                bins.append({"lower": index / 10, "count": len(selected),
                    "predicted": float(np.mean([o["home_win_probability"] for o in selected])),
                    "observed": float(np.mean([o["home_won"] for o in selected]))})
        distinct = len({o.get("game_key", (o["dataset_id"], o["game_id"])) for o in rows})
        cohorts[cohort] = {"count": len(rows), "distinct_games": distinct,
                          "metrics": metrics, "players": player_metrics, "reliability_bins": bins}
    distinct = len({o.get("game_key", (o["dataset_id"], o["game_id"])) for o in outcomes})
    return {"status": "EXPERIMENTAL", "forecasts_scored": len(outcomes), "distinct_games": distinct,
            "metrics": {}, "cohorts": cohorts,
            "note": "Cohorts are reported separately. Metrics weight frozen forecast runs equally; repeated scenarios for the same target game are not independent games. Synthetic self-generated outcomes do not demonstrate predictive skill. Retrospective source timing is caller-supplied and unauthenticated. No calibration approval gate has been passed."}
