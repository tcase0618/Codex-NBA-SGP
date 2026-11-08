"""Empirical-Bayes baseline; all defaults are assumptions, never tracking observations."""
from collections import defaultdict
from datetime import timedelta

import numpy as np

from .schemas import utc
from .store import digest

MODEL_VERSION = "event-baseline-0.1.1"
STAT_KEYS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "fga", "fta", "minutes")
LIMITATIONS = [
    "Experimental baseline; uncalibrated until evaluated on independent historical games.",
    "Rotation, availability, action mix and zone priors are assumptions unless supplied; no fitted availability classifier.",
    "No licensed tracking, defender assignments, coverage, spatial gravity or shot-quality model.",
    "No live feed, in-game conditioning, tactical timeout model or intentional-foul policy.",
    "Empirical shrinkage constants are fixed baseline priors, not optimized hyperparameters.",
    "Pace uses box-score possession estimates; historical play-by-play reconstruction is pending.",
    "Team offense rating is diagnostic only; empirical player shooting and usage drive simulated offense.",
    "No personal-foul, foulout or and-one policy; shooting-foul events produce free throws only.",
    "Action and shot-zone labels use fixed diagnostic priors, not fitted tactical outcome models.",
    "Availability is conditioned on a feasible five-player, 240-minute roster; empirical participation can differ from supplied marginal probabilities.",
    "Rotations use half-minute stints. A minutes_cap of 48 means unrestricted overtime eligibility; lower caps apply to the whole game.",
]


def ratio(n, d, prior, strength):
    return (n + strength * prior) / (d + strength)


def prepare(dataset, request):
    cutoff = request.cutoff
    if utc(dataset["retrieved_at"]) > cutoff or (
            dataset.get("imported_at") and utc(dataset["imported_at"]) > cutoff):
        # Original known_at timestamps permit retrospective evaluation, but must be disclosed.
        retrospective_source = True
    else:
        retrospective_source = False
    games = [g for g in dataset["games"] if utc(g["known_at"]) <= cutoff
             and utc(g["completed_at"]) < cutoff]
    rotations = {}
    for r in sorted(dataset["rotations"], key=lambda r: utc(r["known_at"])):
        if utc(r["known_at"]) <= cutoff:
            rotations[r["player_id"]] = r.copy()
    relevant = {request.home_id, request.away_id}
    teams = {t["id"]: t for t in dataset["teams"]}
    if not relevant <= teams.keys():
        raise ValueError("Unknown team")
    players = {p["id"]: p for p in dataset["players"]}
    for scenario in request.scenarios:
        if scenario.player_id not in rotations or rotations[scenario.player_id]["team_id"] not in relevant:
            raise ValueError("Scenario player has no eligible rotation snapshot")
        rotations[scenario.player_id].update(scenario.model_dump(exclude_none=True, exclude={"player_id"}))
    history = defaultdict(list)
    team_history = defaultdict(list)
    for g in games:
        for b in g["boxes"]:
            history[b["player_id"]].append((g, b))
        for tid in (g["home_id"], g["away_id"]):
            own = [b for b in g["boxes"] if b["team_id"] == tid]
            opp = [b for b in g["boxes"] if b["team_id"] != tid]
            poss = sum(b["fga"] + .44 * b["fta"] - b["oreb"] + b["tov"] for b in own)
            opp_poss = sum(b["fga"] + .44 * b["fta"] - b["oreb"] + b["tov"] for b in opp)
            p = max(1, (poss + opp_poss) / 2)
            team_history[tid].append({"poss": p * 48 / (48 + 5 * g["overtime_periods"]),
                "off": sum(b["pts"] for b in own) / p * 100,
                "def": sum(b["pts"] for b in opp) / p * 100,
                "tipoff": g["tipoff"]})
    profiles = []
    warnings = list(LIMITATIONS)
    if retrospective_source:
        warnings.append("Retrospective source replay: source retrieval or import occurred after cutoff; supplied known_at timestamps are not independently verified.")
    for tid in (request.home_id, request.away_id):
        roster = [r for r in rotations.values() if r["team_id"] == tid]
        if len([r for r in roster if r["active_probability"] > 0 and r["minutes_cap"] > 0]) < 5:
            raise ValueError(f"{tid}: at least five eligible players required")
        if sum(r["minutes_cap"] for r in roster if r["active_probability"] > 0) < 240:
            raise ValueError(f"{tid}: rotation caps cannot supply 240 minutes")
        ps = []
        for r in roster:
            rows = history[r["player_id"]]
            totals = {k: sum(b[k] for _, b in rows) for k in (
                "minutes", "fga", "fgm", "fg3a", "fg3m", "fta", "ftm", "oreb", "dreb", "ast", "stl", "blk", "tov", "pf")}
            m = totals["minutes"]
            two_a = totals["fga"] - totals["fg3a"]
            two_m = totals["fgm"] - totals["fg3m"]
            p = {**players[r["player_id"]], **r, "games": len(rows), "historical_minutes": m,
                "usage": ratio(totals["fga"] + .44 * totals["fta"] + totals["tov"], m, .48, 100),
                "three_share": ratio(totals["fg3a"], totals["fga"], .38, 60),
                "two_pct": ratio(two_m, two_a, .53, 80),
                "three_pct": ratio(totals["fg3m"], totals["fg3a"], .36, 80),
                "ft_pct": ratio(totals["ftm"], totals["fta"], .78, 40),
                "foul_draw": ratio(totals["fta"] / 2, totals["fga"] + totals["fta"] / 2, .10, 80),
                "turnover": ratio(totals["tov"], totals["fga"] + .44 * totals["fta"] + totals["tov"], .12, 80),
                "assist_weight": ratio(totals["ast"], m, .09, 100),
                "rebound_weight": ratio(totals["oreb"] + totals["dreb"], m, .16, 100),
                "steal_weight": ratio(totals["stl"], m, .025, 100),
                "block_weight": ratio(totals["blk"], m, .018, 100)}
            if utc(r["known_at"]) < cutoff - timedelta(hours=24):
                warnings.append(f"Stale rotation: {p['name']} snapshot {r['known_at']}")
            ps.append(p)
        rows = team_history[tid]
        n = len(rows)
        def shrink(key, prior):
            return (sum(x[key] for x in rows) + 10 * prior) / (n + 10)
        profiles.append({"id": tid, "name": teams[tid]["name"], "players": ps,
                         "pace": shrink("poss", 99), "offense": shrink("off", 114),
                         "defense": shrink("def", 114), "games": n})
    snapshot = {"games": games, "rotations": list(rotations.values()), "profiles": profiles}
    return {"teams": profiles, "games_used": len(games), "feature_hash": digest(snapshot),
            "last_game_at": max((g["completed_at"] for g in games), default=None),
            "retrospective_source": retrospective_source, "warnings": list(dict.fromkeys(warnings))}


def summarize(samples):
    x = np.asarray(samples, dtype=float)
    return {"mean": round(float(x.mean()), 3), "median": round(float(np.median(x)), 3),
            "p05": round(float(np.quantile(x, .05)), 3), "p10": round(float(np.quantile(x, .10)), 3),
            "p25": round(float(np.quantile(x, .25)), 3), "p75": round(float(np.quantile(x, .75)), 3),
            "p90": round(float(np.quantile(x, .90)), 3), "p95": round(float(np.quantile(x, .95)), 3)}
