"""Shared event simulation with constrained half-minute rotations.

Clock, actions and defensive interaction are intentionally baseline approximations.
The event ledger is authoritative for every simulated box score.
"""
import math
import random
from collections import Counter

import numpy as np

from .model import STAT_KEYS, summarize

BOX_KEYS = ("pts", "oreb", "dreb", "ast", "fg3m", "fg3a", "fgm", "fga", "ftm", "fta", "stl", "blk", "tov", "minutes")
ACTIONS = ("transition", "pick_and_roll", "isolation", "post_up", "spot_up", "handoff")


def choose(rng, players, key):
    return rng.choices(players, weights=[max(.0001, p[key]) for p in players], k=1)[0]


def field_goal_outcome(rng, make_probability, block_probability):
    """Preserve the unconditional make target; blocks are a subset of misses.

    An observed FG% already includes blocked attempts. Sampling an independent
    block before that FG% would penalize shooting twice. The categorical outcomes
    have probabilities q, min(b, 1-q), and 1-q-min(b, 1-q). If a future block model
    requests more blocks than misses, the available miss mass is the hard cap.
    """
    for probability in (make_probability, block_probability):
        if (isinstance(probability, bool) or not isinstance(probability, (int, float))
            or not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError("Field-goal probabilities must be finite numbers in [0, 1]")
    effective_block_probability = min(block_probability, 1 - make_probability)
    draw = rng.random()
    made = draw < make_probability
    blocked = not made and draw < make_probability + effective_block_probability
    return made, blocked


def quotas(players, rng, slots=96, overtime=False, boxes=None):
    """Integer player/stint assignment with five players in every stint."""
    caps = [min(slots, int((p["minutes_cap"] if not overtime else (
        5 if p["minutes_cap"] >= 48 else max(0, p["minutes_cap"] - boxes[p["player_id"]]["minutes"]))) * 2 + 1e-7)) for p in players]
    if sum(caps) < 5 * slots:
        raise ValueError("Available minutes restrictions cannot fill this game period")
    weights = [max(.01, rng.gauss(p["expected_minutes"], 2.5)) for p in players]
    q = [0] * len(players)
    for _ in range(slots * 5):
        i = max((i for i in range(len(players)) if q[i] < caps[i]),
                key=lambda i: weights[i] / (q[i] + 1))
        q[i] += 1
    return q


def rotation(players, q, remaining, rng):
    mandatory = [i for i, n in enumerate(q) if n == remaining]
    candidates = [i for i, n in enumerate(q) if n > 0 and i not in mandatory]
    # Highest residual degree preserves feasible five-player assignments.
    candidates.sort(key=lambda i: (q[i], rng.random()), reverse=True)
    selected = mandatory + candidates[:5 - len(mandatory)]
    if len(selected) != 5:
        raise RuntimeError("Rotation failed to provide five players")
    for i in selected:
        q[i] -= 1
    return [players[i] for i in selected]


def game(prepared, seed, trace=False):
    rng = random.Random(seed)
    teams = prepared["teams"]
    boxes = {p["player_id"]: {k: 0 for k in BOX_KEYS} for t in teams for p in t["players"]}
    active, rejected = [], 0
    for t in teams:
        for attempt in range(100):
            ps = [p for p in t["players"] if p["minutes_cap"] > 0 and rng.random() < p["active_probability"]]
            if len(ps) >= 5 and sum(min(96, math.floor(p["minutes_cap"] * 2)) for p in ps) >= 480:
                active.append(ps)
                break
            rejected += 1
        else:
            raise ValueError("Availability scenarios cannot reliably supply a legal rotation; revise the roster/caps")
    shooting = {}
    for ps in active:
        for p in ps:
            shooting[p["player_id"]] = {
                k: rng.betavariate(p[k] * 120, (1 - p[k]) * 120)
                for k in ("two_pct", "three_pct", "ft_pct")}
    score, possessions, team_rebounds = [0, 0], [0, 0], [0, 0]
    events, lineups = [], []
    action_counts = Counter()
    pace = max(75, min(125, rng.gauss(sum(t["pace"] for t in teams) / 2, 4)))
    offense = rng.randrange(2)
    event_id = 0

    def possession(side, on_court, period, clock):
        nonlocal event_id
        defense = 1 - side
        own, opp = on_court[side], on_court[defense]
        possessions[side] += 1
        for second_chance in range(100):
            event_id += 1
            shooter = choose(rng, own, "usage")
            pid = shooter["player_id"]
            b = boxes[pid]
            action = rng.choices(ACTIONS, (.16, .30, .12, .08, .25, .09))[0]
            action_counts[action] += 1
            e = {"id": event_id, "period": period, "clock_seconds": round(clock, 3),
                 "team_id": teams[side]["id"], "player_id": pid, "action": action,
                 "second_chance": second_chance > 0,
                 "on_court": [[p["player_id"] for p in ps] for ps in on_court]}
            if rng.random() < shooter["turnover"]:
                b["tov"] += 1
                e["type"] = "turnover"
                if rng.random() < .58:
                    thief = choose(rng, opp, "steal_weight")["player_id"]
                    boxes[thief]["stl"] += 1
                    e["steal_id"] = thief
                if trace:
                    events.append(e)
                return
            three = rng.random() < shooter["three_share"]
            zone = "three" if three else ("rim" if rng.random() < .65 else "midrange")
            e["zone"] = zone
            if rng.random() < shooter["foul_draw"]:
                attempts = 3 if three else 2
                made = sum(rng.random() < shooting[pid]["ft_pct"] for _ in range(attempts - 1))
                last_made = rng.random() < shooting[pid]["ft_pct"]
                made += int(last_made)
                b["fta"] += attempts
                b["ftm"] += made
                b["pts"] += made
                score[side] += made
                e.update(type="shooting_foul", fta=attempts, ftm=made, points=made,
                         last_free_throw_made=last_made)
                if last_made:
                    if trace:
                        events.append(e)
                    return
            else:
                b["fga"] += 1
                b["fg3a"] += int(three)
                base = shooting[pid]["three_pct" if three else "two_pct"]
                # Baseline opponent effect, explicitly estimated from historical box scores.
                adjustment = (teams[defense]["defense"] - 114) / 600
                adjustment += .006 if side == 0 else -.006
                target_make_probability = max(.1, min(.85, base + adjustment))
                made, blocked = field_goal_outcome(rng, target_make_probability, .018 if three else .055)
                e.update(type="field_goal", made=bool(made), points=(3 if three else 2) if made else 0)
                if blocked:
                    blocker = choose(rng, opp, "block_weight")["player_id"]
                    boxes[blocker]["blk"] += 1
                    e["block_id"] = blocker
                if made:
                    points = 3 if three else 2
                    b["fgm"] += 1
                    b["fg3m"] += int(three)
                    b["pts"] += points
                    score[side] += points
                    if rng.random() < .61:
                        passer = choose(rng, [p for p in own if p["player_id"] != pid], "assist_weight")["player_id"]
                        boxes[passer]["ast"] += 1
                        e["assist_id"] = passer
                    if trace:
                        events.append(e)
                    return
            # Exactly one live rebound follows a missed field goal or last free throw.
            offensive = rng.random() < .25
            rebound_side = side if offensive else defense
            if rng.random() < .04:
                team_rebounds[rebound_side] += 1
                e["team_rebound_id"] = teams[rebound_side]["id"]
            else:
                rebounder = choose(rng, on_court[rebound_side], "rebound_weight")["player_id"]
                boxes[rebounder]["oreb" if offensive else "dreb"] += 1
                e["rebound_id"] = rebounder
            e["offensive_rebound"] = offensive
            if trace:
                events.append(e)
            # A retained rebound at the period endpoint cannot create another shot
            # after the horn. Clock values otherwise remain a baseline allocation.
            if not offensive or clock <= 0:
                return
        raise RuntimeError("Excessive offensive rebound chain")

    overtime = 0
    while True:
        slots = 96 if overtime == 0 else 10
        target_possessions = round(pace * 2 * (1 if overtime == 0 else 5 / 48))
        count = max(slots, target_possessions)
        schedule = [1] * slots
        for _ in range(count - slots):
            schedule[rng.randrange(slots)] += 1
        qs = [quotas(ps, rng, slots, overtime > 0, boxes) for ps in active]
        for slot in range(slots):
            on_court = [rotation(active[i], qs[i], slots - slot, rng) for i in range(2)]
            period = slot // 24 + 1 if overtime == 0 else 4 + overtime
            start_clock = (720 - (slot % 24) * 30) if overtime == 0 else 300 - slot * 30
            for ps in on_court:
                for p in ps:
                    boxes[p["player_id"]]["minutes"] += .5
            if trace:
                lineups.append({"period": period, "clock_seconds": start_clock,
                               "duration_seconds": 30, "teams": [[p["player_id"] for p in ps] for ps in on_court]})
            for j in range(schedule[slot]):
                possession(offense, on_court, period, start_clock - (j + 1) * 30 / schedule[slot])
                offense = 1 - offense
        if score[0] != score[1]:
            break
        overtime += 1
        if overtime > 20:
            raise RuntimeError("Overtime safety limit reached; no artificial tiebreaker applied")
    for b in boxes.values():
        b["reb"] = b["oreb"] + b["dreb"]
    for i, t in enumerate(teams):
        bs = [boxes[p["player_id"]] for p in t["players"]]
        assert sum(b["pts"] for b in bs) == score[i]
        assert sum(b["minutes"] for b in bs) == 240 + 25 * overtime
        assert sum(b["ast"] for b in bs) <= sum(b["fgm"] for b in bs)
    return {"score": score, "boxes": boxes, "overtime_periods": overtime,
            "active_player_ids": [[p["player_id"] for p in ps] for ps in active],
            "possessions": possessions, "events": events, "lineups": lineups,
            "team_rebounds": team_rebounds, "availability_rejections": rejected,
            "action_counts": dict(action_counts)}


def run(prepared, simulations, seed):
    games = [game(prepared, seed + i, trace=i == 0) for i in range(simulations)]
    scores = np.asarray([g["score"] for g in games])
    margin = scores[:, 0] - scores[:, 1]
    p = float(np.mean(margin > 0))
    # Wilson bounds remain informative when all Monte Carlo draws have one winner.
    # They measure simulation sampling error, not model uncertainty/calibration.
    z = 1.96
    center = (p + z * z / (2 * simulations)) / (1 + z * z / simulations)
    radius = z * math.sqrt(p * (1 - p) / simulations + z * z / (4 * simulations**2)) / (1 + z * z / simulations)
    players = []
    for t in prepared["teams"]:
        for player in t["players"]:
            pid = player["player_id"]
            samples = {k: [g["boxes"][pid][k] for g in games] for k in STAT_KEYS}
            players.append({"id": pid, "name": player["name"], "position": player["position"],
                "team_id": t["id"], "archetypes": player["archetypes"],
                "configured_active_probability": player["active_probability"],
                "sampled_active_probability": sum(pid in ids for g in games for ids in g["active_player_ids"]) / simulations,
                "played_probability": sum(g["boxes"][pid]["minutes"] > 0 for g in games) / simulations,
                "stats": {k: summarize(v) for k, v in samples.items()},
                "thresholds": {"pts_ge_20": float(np.mean(np.asarray(samples["pts"]) >= 20)),
                    "pts_ge_30": float(np.mean(np.asarray(samples["pts"]) >= 30)),
                    "reb_ge_10": float(np.mean(np.asarray(samples["reb"]) >= 10)),
                    "ast_ge_10": float(np.mean(np.asarray(samples["ast"]) >= 10)),
                    "fg3m_ge_3": float(np.mean(np.asarray(samples["fg3m"]) >= 3))},
                "samples": samples})
    joint_keys = ["home_score", "away_score", "margin", "total"]
    joint = np.column_stack((scores, margin, scores.sum(axis=1)))
    for pl in players:
        for stat in ("pts", "reb", "ast"):
            joint_keys.append(f"{pl['id']}:{stat}")
            joint = np.column_stack((joint, pl["samples"][stat]))
    cov = np.cov(joint, rowvar=False)
    sd = np.sqrt(np.maximum(0, np.diag(cov)))
    denom = np.outer(sd, sd)
    corr = np.divide(cov, denom, out=np.zeros_like(cov), where=denom > 0)
    return {"home_win_probability": p, "away_win_probability": 1 - p,
            "monte_carlo_standard_error": math.sqrt(p * (1 - p) / simulations),
            "win_probability_monte_carlo_interval_95": [max(0, center - radius), min(1, center + radius)],
            "home_score": summarize(scores[:, 0]), "away_score": summarize(scores[:, 1]),
            "margin": summarize(margin), "total": summarize(scores.sum(axis=1)),
            "players": players, "score_samples": scores.tolist(),
            "correlations": {"keys": joint_keys, "matrix": np.round(corr, 4).tolist(),
                             "covariance": np.round(cov, 4).tolist()},
            "representative_game": games[0], "overtime_probability": float(np.mean([g["overtime_periods"] > 0 for g in games])),
            "availability_rejections": sum(g["availability_rejections"] for g in games),
            "availability_sampling": "Independent participation draws conditioned on at least five players and 240 feasible regulation minutes; reported sampled participation can differ from configured probabilities.",
            "mean_possessions": np.mean([g["possessions"] for g in games], axis=0).tolist()}
