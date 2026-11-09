"""Walk-forward backtest of the full projection stack.

Protocol (no future information):
* Models and rate aggregates are refit weekly at Monday 12:00 UTC using only
  games that tipped off before that time.
* Each game's roster, minutes and availability inputs use only games that
  tipped off at least one hour before its own tipoff.
* Every forecast is frozen (stored) before its outcome is attached.
* Market lines are attached afterwards as a benchmark and never used as inputs.

Win-probability recalibration (Platt scaling) is itself walk-forward: games in
week k are recalibrated with a logistic fit on forecasts from weeks < k only.
"""
import json
import math
import time
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import models as M

PLAYER_STATS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "minutes")


def week_cutoffs(start, end):
    d = datetime.fromisoformat(start.replace("Z", "+00:00"))
    d = (d - timedelta(days=d.weekday())).replace(hour=12, minute=0, second=0, microsecond=0)
    out = []
    stop = datetime.fromisoformat(end.replace("Z", "+00:00"))
    while d < stop:
        out.append(d)
        d += timedelta(days=7)
    return out


def _run_week(args):
    db_path, model_cutoff, games, sims, seed, config = args
    from .engine import Engine
    from .projection import Projector
    from .warehouse import Warehouse
    wh = Warehouse(db_path)
    pj = Projector(Engine(wh), **(config or {}))
    out = []
    for g in games:
        tip = datetime.fromisoformat(g["tipoff"].replace("Z", "+00:00"))
        cutoff = M.iso(tip - timedelta(hours=1))
        t0 = time.time()
        try:
            r = pj.project(g["home_id"], g["away_id"], cutoff, sims=sims, seed=seed, model_cutoff=model_cutoff, pilot=800,
                           tipoff=g["tipoff"])
        except Exception as error:  # record; never substitute a fabricated forecast
            out.append({"game_id": g["id"], "error": repr(error)[:300]})
            continue
        frozen = {"game_id": g["id"], "tipoff": g["tipoff"], "cutoff": cutoff, "model_cutoff": model_cutoff,
                  "home_id": g["home_id"], "away_id": g["away_id"], "season": g["season"], "season_type": g["season_type"],
                  "sims": sims, "seed": seed, "seconds": round(time.time() - t0, 2),
                  "p_home": r["home_win_probability"], "p_home_ratings": r["ratings_model_win_probability"],
                  "margin": r["margin"], "total": r["total"],
                  "home_pts": r["teams"][0]["points"], "away_pts": r["teams"][1]["points"],
                  "team_stats": [{k: t[k]["mean"] for k in ("reb", "ast", "tov", "fg3m", "fg3a")} for t in r["teams"]],
                  "shifts": r["calibration"]["shifts"],
                  "players": [{"id": p["id"], "team_id": p["team_id"], "position": p["position"], "start_prob": p["start_prob"],
                               "played_prob": p["played_prob"], "active_prob": p["active_prob_input"],
                               "mean": {k: p["stats"][k]["mean"] for k in PLAYER_STATS},
                               "p10": {k: p["stats"][k]["p10"] for k in PLAYER_STATS},
                               "p90": {k: p["stats"][k]["p90"] for k in PLAYER_STATS},
                               "cond_mean": {k: v["mean"] for k, v in p["conditional_on_playing"].items()},
                               "cdf": p.get("cdf_conditional"),
                               "baseline": p["baseline_last10"]} for p in r["players"]]}
        out.append(frozen)
    return out


def run(wh, start, end, sims=2000, seed=11, workers=4, progress=print, max_games=None, config=None, stride=1):
    """Produce frozen walk-forward forecasts for completed games in [start, end)."""
    start, end = M.iso(start), M.iso(end)
    weeks = week_cutoffs(start, end)
    tasks = []
    total = 0
    for i, wk in enumerate(weeks):
        lo = M.iso(wk)
        hi = M.iso(wk + timedelta(days=7))
        games = wh.query("SELECT * FROM games WHERE tipoff >= ? AND tipoff < ? AND tipoff >= ? AND tipoff < ? ORDER BY tipoff",
                         (lo, hi, start, end))
        if stride > 1:
            games = games[::stride]
        if max_games:
            games = games[:max(0, max_games - total)]
        total += len(games)
        if games:
            tasks.append((wh.path, lo, games, sims, seed, config))
    progress(f"{len(tasks)} weekly refits, {total} games")
    forecasts = []
    t0 = time.time()
    with ProcessPoolExecutor(workers) as pool:
        for i, res in enumerate(pool.map(_run_week, tasks)):
            forecasts.extend(res)
            progress(f"  week {i + 1}/{len(tasks)} done ({len(forecasts)} forecasts, {time.time() - t0:.0f}s)")
    run_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with wh.connect() as db:
        for f in forecasts:
            if "error" not in f:
                db.execute("INSERT OR REPLACE INTO model_predictions VALUES(?,?,?,?,?,?)", (
                    f"{run_id}:{f['game_id']}", "possession-sim-1.2", f["game_id"], created, f["cutoff"], json.dumps(f)))
    return {"run_id": run_id, "created_at": created, "forecasts": forecasts, "start": start, "end": end, "sims": sims,
            "config": config or {}}


# ----------------------------------------------------------------- scoring
def _ll(p, y):
    p = min(1 - 1e-6, max(1e-6, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _platt_fit(p, y):
    x = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    X = np.column_stack([np.ones_like(x), x])
    return M.fit_logistic(X, np.asarray(y, float), l2=1.0)


def _platt_apply(w, p):
    x = math.log(min(1 - 1e-6, max(1e-6, p)) / (1 - min(1 - 1e-6, max(1e-6, p))))
    return 1 / (1 + math.exp(-(w[0] + w[1] * x)))


def _ml_prob(home_ml, away_ml):
    def imp(ml):
        return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)
    if home_ml is None or away_ml is None:
        return None
    a, b = imp(home_ml), imp(away_ml)
    return a / (a + b)  # proportional de-vig


def bins(p, y, n=10):
    out = []
    p, y = np.asarray(p), np.asarray(y)
    for i in range(n):
        m = (p >= i / n) & (p < (i + 1) / n if i < n - 1 else p <= 1)
        if m.sum():
            out.append({"lower": i / n, "upper": (i + 1) / n, "count": int(m.sum()),
                        "predicted": round(float(p[m].mean()), 4), "observed": round(float(y[m].mean()), 4)})
    return out


def score(wh, forecasts):
    games = {g["id"]: g for g in wh.query("SELECT * FROM games")}
    lines = {l["game_id"]: l for l in wh.query("SELECT * FROM market_lines")}
    boxes = defaultdict(dict)
    for r in wh.query("SELECT game_id, player_id, minutes, dnp, pts, reb, ast, fg3m, stl, blk, tov, starter FROM player_games"):
        boxes[r["game_id"]][r["player_id"]] = r
    ok = sorted([f for f in forecasts if "error" not in f], key=lambda f: f["tipoff"])
    errors = [f for f in forecasts if "error" in f]
    rows = []
    # Walk-forward Platt: by model week.
    by_week = defaultdict(list)
    for f in ok:
        by_week[f["model_cutoff"]].append(f)
    history_p, history_y = [], []
    for wk in sorted(by_week):
        w = _platt_fit(history_p, history_y) if len(history_p) >= 150 else None
        for f in by_week[wk]:
            g = games[f["game_id"]]
            y = int(g["home_score"] > g["away_score"])
            margin, total = g["home_score"] - g["away_score"], g["home_score"] + g["away_score"]
            line = lines.get(f["game_id"]) or {}
            rows.append({"f": f, "g": g, "y": y, "margin": margin, "total": total, "line": line,
                         "p_cal": _platt_apply(w, f["p_home"]) if w is not None else None})
        for f in by_week[wk]:
            history_p.append(f["p_home"])
            history_y.append(int(games[f["game_id"]]["home_score"] > games[f["game_id"]]["away_score"]))
    if not rows:
        return {"status": "NOT MEASURABLE", "games": 0, "errors": errors}
    y = np.array([r["y"] for r in rows])
    p = np.array([r["f"]["p_home"] for r in rows])
    pr = np.array([r["f"]["p_home_ratings"] for r in rows])
    home_rate = float(np.mean(y))
    game = {
        "games": len(rows), "home_win_rate": round(home_rate, 4),
        "win_prob": {
            "simulation": {"log_loss": _mean([_ll(a, b) for a, b in zip(p, y)]), "brier": _mean((p - y) ** 2),
                           "accuracy": _mean((p > .5) == y)},
            "ratings_model": {"log_loss": _mean([_ll(a, b) for a, b in zip(pr, y)]), "brier": _mean((pr - y) ** 2),
                              "accuracy": _mean((pr > .5) == y)},
            "coin_flip": {"log_loss": round(math.log(2), 4), "brier": 0.25},
        },
        "calibration_bins": bins(p, y),
        "score": {
            "home_points_mae": _mean([abs(r["f"]["home_pts"]["mean"] - r["g"]["home_score"]) for r in rows]),
            "away_points_mae": _mean([abs(r["f"]["away_pts"]["mean"] - r["g"]["away_score"]) for r in rows]),
            "margin_mae": _mean([abs(r["f"]["margin"]["mean"] - r["margin"]) for r in rows]),
            "total_mae": _mean([abs(r["f"]["total"]["mean"] - r["total"]) for r in rows]),
            "margin_bias": _mean([r["f"]["margin"]["mean"] - r["margin"] for r in rows]),
            "total_bias": _mean([r["f"]["total"]["mean"] - r["total"] for r in rows]),
            "margin_80_coverage": _mean([r["f"]["margin"]["p10"] <= r["margin"] <= r["f"]["margin"]["p90"] for r in rows]),
            "total_80_coverage": _mean([r["f"]["total"]["p10"] <= r["total"] <= r["f"]["total"]["p90"] for r in rows]),
        },
    }
    # Live recalibration parameters: Platt fit on all scored forecasts (applied only to future games).
    game["platt_live"] = {"weights": [round(float(x), 5) for x in _platt_fit(p, y)], "games": len(rows),
                          "trained_through": rows[-1]["f"]["tipoff"]}
    cal = [r for r in rows if r["p_cal"] is not None]
    if cal:
        pc, yc = np.array([r["p_cal"] for r in cal]), np.array([r["y"] for r in cal])
        praw = np.array([r["f"]["p_home"] for r in cal])
        game["win_prob"]["simulation_platt_walkforward"] = {
            "games": len(cal), "log_loss": _mean([_ll(a, b) for a, b in zip(pc, yc)]), "brier": _mean((pc - yc) ** 2),
            "raw_log_loss_same_games": _mean([_ll(a, b) for a, b in zip(praw, yc)])}
        game["calibration_bins_platt"] = bins(pc, yc)
    mk = [r for r in rows if r["line"].get("home_spread") is not None]
    if mk:
        mkt = {"games": len(mk),
               "market_margin_mae": _mean([abs(-r["line"]["home_spread"] - r["margin"]) for r in mk]),
               "model_margin_mae_same_games": _mean([abs(r["f"]["margin"]["mean"] - r["margin"]) for r in mk]),
               "market_total_mae": _mean([abs(r["line"]["total"] - r["total"]) for r in mk if r["line"].get("total")]),
               "model_total_mae_same_games": _mean([abs(r["f"]["total"]["mean"] - r["total"]) for r in mk if r["line"].get("total")]),
               "model_vs_spread_corr": round(float(np.corrcoef([r["f"]["margin"]["mean"] for r in mk], [-r["line"]["home_spread"] for r in mk])[0, 1]), 4) if len(mk) > 2 else None}
        mlp = [(r, _ml_prob(r["line"].get("home_moneyline"), r["line"].get("away_moneyline"))) for r in mk]
        mlp = [(r, q) for r, q in mlp if q is not None]
        if mlp:
            mkt["market_moneyline_log_loss"] = _mean([_ll(q, r["y"]) for r, q in mlp])
            mkt["model_log_loss_same_games"] = _mean([_ll(r["f"]["p_home"], r["y"]) for r, q in mlp])
            mkt["moneyline_games"] = len(mlp)
        mkt["note"] = "Lines are provider-reported in ESPN summaries without an observation timestamp; benchmark only."
        game["market_benchmark"] = mkt
    # Players.
    prow = []
    for r in rows:
        bx = boxes[r["f"]["game_id"]]
        for pl in r["f"]["players"]:
            act = bx.get(pl["id"])
            played = bool(act and not act["dnp"] and act["minutes"] > 0)
            prow.append({"pl": pl, "act": act, "played": played, "g": r})
    pm = {"rostered_player_forecasts": len(prow), "played": sum(x["played"] for x in prow)}
    pm["availability"] = {"brier": _mean([(x["pl"]["played_prob"] - x["played"]) ** 2 for x in prow]),
                          "log_loss": _mean([_ll(x["pl"]["played_prob"], x["played"]) for x in prow]),
                          "base_rate": _mean([x["played"] for x in prow]),
                          "bins": bins([x["pl"]["played_prob"] for x in prow], [int(x["played"]) for x in prow])}
    stats = {}
    played = [x for x in prow if x["played"] and x["pl"]["cond_mean"]]
    for k in PLAYER_STATS:
        cond = [x for x in played if k in x["pl"]["cond_mean"]]
        model_err = [abs(x["pl"]["cond_mean"][k] - x["act"][k]) for x in cond]
        base = [x for x in cond if x["pl"]["baseline"]]
        stats[k] = {"n": len(cond), "model_mae_conditional": _mean(model_err),
                    "baseline_last10_mae": _mean([abs(x["pl"]["baseline"][k] - x["act"][k]) for x in base]),
                    "model_mae_same_rows_as_baseline": _mean([abs(x["pl"]["cond_mean"][k] - x["act"][k]) for x in base]),
                    "unconditional_mae_all_rostered": _mean([abs(x["pl"]["mean"][k] - ((x["act"] or {}).get(k) or 0 if x["played"] else 0)) for x in prow]),
                    "interval_80_coverage_all_rostered": _mean([x["pl"]["p10"][k] <= (((x["act"] or {}).get(k) or 0) if x["played"] else 0) <= x["pl"]["p90"][k] for x in prow])}
    pm["stats"] = stats
    pm["distribution"] = distribution_scores(played)
    # Segments for points (conditional MAE).
    seg = defaultdict(list)
    for x in played:
        err = abs(x["pl"]["cond_mean"].get("pts", 0) - x["act"]["pts"])
        pos = {"PG": "G", "SG": "G", "G": "G", "SF": "F", "PF": "F", "F": "F", "C": "C"}.get(x["pl"]["position"], "F")
        seg[f"position:{pos}"].append(err)
        seg["role:projected_starter" if x["pl"]["start_prob"] >= .5 else "role:projected_reserve"].append(err)
        m = x["pl"]["cond_mean"].get("minutes", 0)
        seg["minutes:30+" if m >= 30 else "minutes:20-30" if m >= 20 else "minutes:<20"].append(err)
        seg[f"season:{x['g']['g']['season']}"].append(err)
        seg["game:competitive(|margin|<=10)" if abs(x["g"]["margin"]) <= 10 else "game:blowout(|margin|>10)"].append(err)
    pm["points_mae_segments"] = {k: {"n": len(v), "mae": _mean(v)} for k, v in sorted(seg.items())}
    gseg = defaultdict(list)
    for r in rows:
        gseg[f"season:{r['g']['season']}:{'regular' if r['g']['season_type'] == 2 else 'playoffs'}"].append(r)
    game["segments"] = {k: {"games": len(v), "log_loss": _mean([_ll(r["f"]["p_home"], r["y"]) for r in v]),
                            "brier": _mean([(r["f"]["p_home"] - r["y"]) ** 2 for r in v]),
                            "margin_mae": _mean([abs(r["f"]["margin"]["mean"] - r["margin"]) for r in v])} for k, v in sorted(gseg.items())}
    return {"status": "MEASURED_WALK_FORWARD", "game": game, "players": pm, "errors": len(errors),
            "error_examples": errors[:5],
            "notes": ["Retrospective walk-forward on archived ESPN data: features use only games before each cutoff, but this is not a live prospective test.",
                      "No historical injury reports: availability uses only prior-game participation.",
                      "Player MAE is conditional on the player actually playing (prop-style); unconditional MAE includes DNP zeros."]}


def _F(cdf, k):
    if k < 0:
        return 0.0
    return cdf[k] if k < len(cdf) else 1.0


def _poisson_cdf(lam, kmax):
    out, term, acc = [], math.exp(-lam), 0.0
    for k in range(kmax + 1):
        acc += term
        out.append(min(1.0, acc))
        term *= lam / (k + 1)
    return out


def distribution_scores(played, seed=7):
    """Randomized PIT calibration and ranked probability score for integer stats,
    conditional on playing, vs a Poisson(last-10 mean) baseline on the same rows.

    PIT: u = F(y-1) + V * (F(y) - F(y-1)), V ~ U(0,1); calibrated forecasts give
    u ~ Uniform(0,1), so the central 80% interval should hold 80% of outcomes.
    The inclusive check p10 <= y <= p90 overstates coverage for integer counts."""
    rng = np.random.default_rng(seed)
    out = {}
    from .projection import CDF_STATS
    for k in CDF_STATS:
        rows = [x for x in played if x["pl"].get("cdf") and x["pl"]["cdf"].get(k) and x["pl"]["baseline"]]
        if not rows:
            continue
        u, rps_m, rps_b, ub = [], [], [], []
        for x in rows:
            cdf, y = x["pl"]["cdf"][k], int(x["act"][k])
            v = rng.random()
            u.append(_F(cdf, y - 1) + v * (_F(cdf, y) - _F(cdf, y - 1)))
            kmax = max(len(cdf), y + 1, 15)
            rps_m.append(sum((_F(cdf, j) - (y <= j)) ** 2 for j in range(kmax)))
            pc = _poisson_cdf(max(x["pl"]["baseline"][k], 0.05), kmax)
            rps_b.append(sum((pc[j] - (y <= j)) ** 2 for j in range(kmax)))
            ub.append(_F(pc, y - 1) + v * (_F(pc, y) - _F(pc, y - 1)))
        u, ub = np.array(u), np.array(ub)
        hist = np.histogram(u, bins=10, range=(0, 1))[0] / len(u)
        out[k] = {"n": len(rows), "pit_central80_coverage": round(float(np.mean((u >= .1) & (u <= .9))), 4),
                  "pit_mean": round(float(u.mean()), 4), "pit_histogram": [round(float(h), 4) for h in hist],
                  "pit_sd": round(float(u.std()), 4),
                  "rps_model": round(float(np.mean(rps_m)), 4), "rps_poisson_last10": round(float(np.mean(rps_b)), 4),
                  "baseline_pit_central80_coverage": round(float(np.mean((ub >= .1) & (ub <= .9))), 4)}
    return out


def _mean(x):
    x = list(x)
    return round(float(np.mean(x)), 4) if x else None


def load_forecasts(wh, run_id):
    return [json.loads(r["payload"]) for r in wh.query("SELECT payload FROM model_predictions WHERE id LIKE ?", (run_id + ":%",))]


def latest_platt(wh, model_version=None):
    """Most recent stored live Platt parameters fitted on the given simulator
    version (calibration is only valid for the model it was fitted on)."""
    from .projection import SIM_VERSION
    version = model_version or SIM_VERSION
    for r in wh.query("SELECT payload FROM validation_results WHERE kind='walk_forward_backtest' AND model_version=? ORDER BY created_at DESC", (version,)):
        game = json.loads(r["payload"])["report"].get("game", {})
        live = game.get("platt_live")
        wf = game.get("win_prob", {}).get("simulation_platt_walkforward")
        # Deployment gate: only if walk-forward Platt beat the raw simulation on the same games.
        if not live or live["games"] < 300 or not wf:
            continue  # too small to decide; fall back to an older, larger run
        if wf["log_loss"] < wf["raw_log_loss_same_games"]:
            return {**live, "walk_forward_log_loss": wf["log_loss"], "raw_log_loss": wf["raw_log_loss_same_games"]}
        return None
    return None


def apply_platt(live, p):
    return round(_platt_apply(live["weights"], p), 4) if live else None


def save(wh, run, report, out_dir="output/backtest"):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run["run_id"], "created_at": run["created_at"], "start": run["start"], "end": run["end"],
               "sims_per_game": run["sims"], "model_version": "possession-sim-1.2", "report": report}
    with wh.connect() as db:
        db.execute("INSERT OR REPLACE INTO validation_results VALUES(?,?,?,?,?)", (
            run["run_id"], run["created_at"], "walk_forward_backtest", "possession-sim-1.2", json.dumps(payload)))
    path = Path(out_dir) / f"backtest-{run['run_id'][:8]}.json"
    path.write_text(json.dumps(payload, indent=1))
    return path
