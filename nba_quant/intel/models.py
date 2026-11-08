"""Statistical models fitted only on games completed before a cutoff.

* Shot quality (xFG): L2-regularized logistic regression (Newton/IRLS) on
  location, zone, shot family, possession context and game state.
* RAPM: possession-weighted ridge regression on lineup stints with separate
  offensive and defensive coefficients per player and a home-court term.
* Team ratings: time-decayed ridge regression of per-100 scoring on offense,
  opponent defense and home court (opponent-adjusted ORtg/DRtg/Net).
* Lineups: empirical-Bayes shrinkage of observed lineup net rating toward the
  sum of its players' RAPM (talent), giving a separate interaction estimate.

All coefficients are MODELED values and carry a training window.
"""
import math
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np

ZONES = ["rim", "short_midrange", "long_midrange", "corner_three", "above_break_three", "unknown"]
FAMILIES = ["dunk", "tip", "alley_oop", "hook", "layup", "floater", "stepback", "fadeaway", "pullup",
            "heave", "jumper", "other"]
DIST_KNOTS = [2, 5, 9, 14, 19, 23, 26, 30]


def iso(value):
    """Normalize to the warehouse tipoff format 'YYYY-MM-DDTHH:MMZ' for string comparison."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timezone-aware cutoff required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


# ----------------------------------------------------------------- shot model
def shot_features(rows):
    n = len(rows)
    d = np.array([r["distance_ft"] if r["distance_ft"] is not None else 15.0 for r in rows], float)
    cols = [np.ones(n), d / 10]
    cols += [np.maximum(0, d - k) / 10 for k in DIST_KNOTS]
    zone = [r["zone"] for r in rows]
    fam = [r["family"] for r in rows]
    cols += [np.array([z == k for z in zone], float) for k in ZONES[1:]]
    cols += [np.array([f == k for f in fam], float) for k in FAMILIES[1:]]
    sip = np.array([r["seconds_into_possession"] or 0 for r in rows], float)
    cols += [np.array([r["second_chance"] for r in rows], float), np.array([r["transition"] for r in rows], float),
             np.minimum(sip, 24) / 24, (sip > 20).astype(float),
             np.array([r["period"] >= 4 for r in rows], float),
             np.minimum(np.abs(np.array([r["score_margin"] or 0 for r in rows], float)), 25) / 25,
             np.array([r["home"] for r in rows], float),
             np.array([(r["angle_deg"] or 0) / 90 for r in rows], float)]
    return np.column_stack(cols)


FEATURE_NAMES = (["intercept", "distance"] + [f"dist_hinge_{k}" for k in DIST_KNOTS]
                 + [f"zone_{z}" for z in ZONES[1:]] + [f"family_{f}" for f in FAMILIES[1:]]
                 + ["second_chance", "transition", "possession_clock", "late_clock", "fourth_or_ot",
                    "abs_margin", "home", "angle"])


def fit_logistic(X, y, l2=1.0, iters=30):
    w = np.zeros(X.shape[1])
    reg = np.full(X.shape[1], l2)
    reg[0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        g = X.T @ (p - y) + reg * w
        H = (X * (p * (1 - p))[:, None]).T @ X + np.diag(reg)
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return w


def reliability(p, y, bins=10):
    out = []
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    for i in range(bins):
        m = (p >= edges[i]) & ((p < edges[i + 1]) if i < bins - 1 else (p <= edges[i + 1]))
        if m.sum():
            out.append({"predicted": round(float(p[m].mean()), 4), "observed": round(float(y[m].mean()), 4),
                        "count": int(m.sum())})
    return out


def train_shot_model(wh, cutoff, holdout_fraction=0.15):
    """Fit xFG on shots before cutoff; report calibration on the most recent
    `holdout_fraction` of shots (fit on the earlier window), then refit on all."""
    cutoff = iso(cutoff)
    rows = wh.query("""SELECT distance_ft, zone, family, seconds_into_possession, second_chance, transition,
        period, score_margin, home, angle_deg, made, tipoff FROM shots WHERE tipoff < ? AND team_attempt_heave = 0""",
                    (cutoff,))
    if len(rows) < 2000:
        return None
    X, y = shot_features(rows), np.array([r["made"] for r in rows], float)
    tips = np.array([r["tipoff"] for r in rows])
    order = np.argsort(tips)
    cut = order[int(len(order) * (1 - holdout_fraction))]
    train = tips < tips[cut]
    evaluation = None
    if train.sum() > 1000 and (~train).sum() > 500:
        w0 = fit_logistic(X[train], y[train])
        p = 1 / (1 + np.exp(-(X[~train] @ w0)))
        yt = y[~train]
        ll = float(-np.mean(yt * np.log(p) + (1 - yt) * np.log(1 - p)))
        base = yt.mean()
        evaluation = {"holdout_shots": int((~train).sum()), "log_loss": round(ll, 4),
                      "baseline_log_loss": round(float(-np.mean(yt * np.log(base) + (1 - yt) * np.log(1 - base))), 4),
                      "brier": round(float(np.mean((p - yt) ** 2)), 4),
                      "holdout_start": str(tips[cut]), "reliability": reliability(p, yt)}
    w = fit_logistic(X, y)
    return {"family": "shot_quality_logistic", "trained_through": cutoff, "n_shots": len(rows),
            "weights": w.tolist(), "features": FEATURE_NAMES, "evaluation": evaluation}


def predict_shots(model, rows):
    if not rows:
        return np.zeros(0)
    w = np.asarray(model["weights"])
    return 1 / (1 + np.exp(-(shot_features(rows) @ w)))


# ----------------------------------------------------------------------- RAPM
def fit_rapm(wh, cutoff, lam=3000.0, since=None, min_poss=0):
    cutoff = iso(cutoff)
    params = [cutoff]
    sql = """SELECT s.home_lineup, s.away_lineup, s.home_pts, s.away_pts, s.home_poss, s.away_poss FROM lineup_stints s
             JOIN games g ON g.id = s.game_id WHERE s.tipoff < ? AND g.pbp_valid = 1 AND s.valid = 1"""
    if since:
        sql += " AND s.tipoff >= ?"
        params.append(iso(since))
    stints = wh.query(sql, params)
    if len(stints) < 500:
        return None
    ids = sorted({p for s in stints for side in ("home_lineup", "away_lineup") for p in s[side].split(",") if p})
    index = {p: i for i, p in enumerate(ids)}
    P = len(ids)
    K = 2 * P + 2  # offense, defense, intercept, home
    A = np.zeros((K, K))
    b = np.zeros(K)
    poss_off = np.zeros(P)
    poss_def = np.zeros(P)
    total_pts = total_poss = 0.0
    for s in stints:
        h = [index[p] for p in s["home_lineup"].split(",") if p]
        a = [index[p] for p in s["away_lineup"].split(",") if p]
        if len(h) != 5 or len(a) != 5:
            continue
        for off, de, pts, poss, home in ((h, a, s["home_pts"], s["home_poss"], 1.0), (a, h, s["away_pts"], s["away_poss"], 0.0)):
            if not poss:
                continue
            idx = off + [P + j for j in de] + [2 * P]
            vals = [1.0] * 10 + [1.0]
            if home:
                idx.append(2 * P + 1)
                vals.append(1.0)
            y = 100.0 * pts / poss
            idx = np.array(idx)
            vals = np.array(vals)
            A[np.ix_(idx, idx)] += poss * np.outer(vals, vals)
            b[idx] += poss * y * vals
            poss_off[off] += poss
            poss_def[de] += poss
            total_pts += pts
            total_poss += poss
    reg = np.full(K, lam)
    reg[2 * P:] = 1e-6
    coef = np.linalg.solve(A + np.diag(reg), b)
    # Approximate posterior SD under ridge: per-possession points variance ~1.2,
    # i.e. 1.2 * 100^2 in per-100 units, scaled by the possession-weighted precision.
    try:
        cov_diag = np.diag(np.linalg.inv(A + np.diag(reg))) * 1.2 * 100 ** 2
    except np.linalg.LinAlgError:
        cov_diag = np.full(K, np.nan)
    players = {}
    for p, i in index.items():
        o, d = coef[i], -coef[P + i]  # positive defense = suppresses opponent scoring
        players[p] = {"orapm": round(float(o), 3), "drapm": round(float(d), 3), "rapm": round(float(o + d), 3),
                      "orapm_sd": round(float(math.sqrt(max(cov_diag[i], 0))), 3),
                      "drapm_sd": round(float(math.sqrt(max(cov_diag[P + i], 0))), 3),
                      "poss_off": int(poss_off[i]), "poss_def": int(poss_def[i])}
    return {"family": "rapm_ridge", "trained_through": cutoff, "lambda": lam, "stints": len(stints),
            "league_ortg": round(100 * total_pts / max(total_poss, 1), 3),
            "home_court_per100": round(float(coef[2 * P + 1]), 3), "players": players}


# ---------------------------------------------------------------- team ratings
def fit_team_ratings(wh, cutoff, lam=12.0, half_life_days=60.0, since=None):
    cutoff = iso(cutoff)
    params = [cutoff]
    sql = """SELECT id, tipoff, home_id, away_id, home_score, away_score, home_poss, away_poss, neutral_site, periods
             FROM games WHERE tipoff < ?"""
    if since:
        sql += " AND tipoff >= ?"
        params.append(iso(since))
    games = wh.query(sql, params)
    if len(games) < 30:
        return None
    teams = sorted({g["home_id"] for g in games} | {g["away_id"] for g in games})
    ti = {t: i for i, t in enumerate(teams)}
    T = len(teams)
    K = 2 * T + 2
    A, b = np.zeros((K, K)), np.zeros(K)
    ref = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    pace_num = defaultdict(float)
    pace_den = defaultdict(float)
    residual_rows = []
    for g in games:
        age = (ref - datetime.fromisoformat(g["tipoff"].replace("Z", "+00:00"))).days
        w = 0.5 ** (age / half_life_days)
        poss = ((g["home_poss"] or 0) + (g["away_poss"] or 0)) / 2 or 100.0
        minutes = 48 + 5 * max(0, (g["periods"] or 4) - 4)
        for off, de, pts, home in ((g["home_id"], g["away_id"], g["home_score"], not g["neutral_site"]),
                                   (g["away_id"], g["home_id"], g["away_score"], False)):
            idx = [ti[off], T + ti[de], 2 * T] + ([2 * T + 1] if home else [])
            y = 100 * pts / poss
            for i in idx:
                b[i] += w * y
                for j in idx:
                    A[i, j] += w
        for t in (g["home_id"], g["away_id"]):
            pace_num[t] += w * poss * 48 / minutes
            pace_den[t] += w
        residual_rows.append(g)
    reg = np.full(K, lam)
    reg[2 * T:] = 1e-6
    coef = np.linalg.solve(A + np.diag(reg), b)
    league_pace = sum(pace_num.values()) / max(sum(pace_den.values()), 1e-9)
    ratings = {}
    for t, i in ti.items():
        o, d = coef[i], coef[T + i]
        ratings[t] = {"ortg_adj": round(float(coef[2 * T] + o), 2), "drtg_adj": round(float(coef[2 * T] + d), 2),
                      "net_adj": round(float(o - d), 2), "pace": round(pace_num[t] / pace_den[t], 2) if pace_den[t] else league_pace}
    hca = float(coef[2 * T + 1])
    # Margin residual SD for the win-probability link, unweighted, in-sample.
    res = []
    for g in residual_rows:
        m = expected_margin(ratings, g["home_id"], g["away_id"], hca, league_pace, g["neutral_site"])
        res.append((g["home_score"] - g["away_score"]) - m)
    sigma = float(np.std(res)) if res else 12.0
    return {"family": "team_ridge", "trained_through": cutoff, "lambda": lam, "half_life_days": half_life_days,
            "games": len(games), "league_ortg": round(float(coef[2 * T]), 2), "home_court_per100": round(hca, 3),
            "league_pace": round(league_pace, 2), "margin_sigma": round(sigma, 3), "teams": ratings}


def expected_margin(ratings, home, away, hca_per100, league_pace, neutral=False):
    h, a = ratings[home], ratings[away]
    pace = (h["pace"] + a["pace"]) / 2 if league_pace is None else (h["pace"] * a["pace"]) / league_pace
    return ((h["net_adj"] - a["net_adj"]) + (0 if neutral else hca_per100)) * pace / 100


def win_probability(model, home, away, neutral=False):
    m = expected_margin(model["teams"], home, away, model["home_court_per100"], model["league_pace"], neutral)
    return m, 0.5 * (1 + math.erf(m / (model["margin_sigma"] * math.sqrt(2))))


# -------------------------------------------------------------------- lineups
def lineup_table(wh, cutoff, team_id, rapm=None, k=400.0, min_seconds=0, since=None):
    cutoff = iso(cutoff)
    params = [cutoff, team_id, team_id]
    sql = """SELECT s.* FROM lineup_stints s JOIN games g ON g.id = s.game_id
             WHERE s.tipoff < ? AND (s.home_id = ? OR s.away_id = ?) AND s.valid = 1 AND g.pbp_valid = 1"""
    if since:
        sql += " AND s.tipoff >= ?"
        params.append(iso(since))
    agg = defaultdict(lambda: defaultdict(float))
    for s in wh.query(sql, params):
        side, opp = ("home", "away") if s["home_id"] == team_id else ("away", "home")
        key = s[f"{side}_lineup"]
        a = agg[key]
        a["seconds"] += s["seconds"]
        a["pts_for"] += s[f"{side}_pts"]
        a["pts_against"] += s[f"{opp}_pts"]
        a["poss_off"] += s[f"{side}_poss"]
        a["poss_def"] += s[f"{opp}_poss"]
        for stat in ("fga", "fg3a", "fta", "oreb", "dreb", "tov"):
            a[f"{stat}_for"] += s[f"{side}_{stat}"]
            a[f"{stat}_against"] += s[f"{opp}_{stat}"]
        a["stints"] += 1
    players = (rapm or {}).get("players", {})
    out = []
    for key, a in agg.items():
        if a["seconds"] < min_seconds:
            continue
        ids = key.split(",")
        po, pd = max(a["poss_off"], 1), max(a["poss_def"], 1)
        ortg, drtg = 100 * a["pts_for"] / po, 100 * a["pts_against"] / pd
        prior = sum(players.get(p, {}).get("rapm", 0.0) for p in ids)
        prior_o = sum(players.get(p, {}).get("orapm", 0.0) for p in ids)
        prior_d = sum(players.get(p, {}).get("drapm", 0.0) for p in ids)
        poss = (po + pd) / 2
        net = ortg - drtg
        adj = (poss * net + k * prior) / (poss + k)
        out.append({"lineup": ids, "seconds": round(a["seconds"]), "minutes": round(a["seconds"] / 60, 1),
                    "possessions": round(poss), "ortg": round(ortg, 1), "drtg": round(drtg, 1), "net": round(net, 1),
                    "talent_prior": round(prior, 2), "talent_prior_off": round(prior_o, 2), "talent_prior_def": round(prior_d, 2),
                    "adjusted_net": round(adj, 2), "interaction": round(adj - prior, 2),
                    "uncertainty_sd": round(150 / math.sqrt(poss + k) * math.sqrt(2), 2),
                    "oreb_pct": round(100 * a["oreb_for"] / max(a["oreb_for"] + a["dreb_against"], 1), 1),
                    "dreb_pct": round(100 * a["dreb_for"] / max(a["dreb_for"] + a["oreb_against"], 1), 1),
                    "three_rate": round(100 * a["fg3a_for"] / max(a["fga_for"], 1), 1),
                    "ft_rate": round(100 * a["fta_for"] / max(a["fga_for"], 1), 1),
                    "tov_pct": round(100 * a["tov_for"] / po, 1), "opp_tov_pct": round(100 * a["tov_against"] / pd, 1)})
    out.sort(key=lambda r: -r["seconds"])
    return out
