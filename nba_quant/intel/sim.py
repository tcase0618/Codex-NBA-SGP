"""Vectorized possession-level game simulator.

All simulations advance in lockstep; every array is shaped (sims, players) or
(sims,). A single shared event stream per simulation produces team scores,
quarter scores and every player box score, so combined statistics (PRA,
double-doubles) and the winner come from the same events.

Possession flow: choose ball-handler/finisher by usage among the five on court
-> turnover (optional steal) | shooting-foul trip (FTs) | field-goal attempt
(zone from the player's shot mix, make probability from shrunk zone FG% +
opponent zone defense + team calibration shift, block, assist, and-one) ->
missed shot/last FT -> rebound (offensive continues the possession).

Rotations: 48 one-minute blocks in regulation, 5 per overtime. Each block's five
is drawn by systematic probability-proportional-to-size sampling with inclusion
probability minutes_i / 48, which always yields exactly five distinct players
and exactly 240 team minutes. Documented limitations: no foul-outs, no bonus
free throws, no intentional fouling, no shot clock, no in-game score effects.
"""
import time

import numpy as np

ZONES = ("rim", "sm", "lm", "c3", "ab3")
STATS = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk", "tov", "pf", "minutes")


def _logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def _pick(rng, weights):
    """Vectorized categorical draw over the last axis; rows must have positive mass."""
    cum = np.cumsum(weights, axis=1)
    r = rng.random(weights.shape[0]) * cum[:, -1]
    return np.minimum((cum <= r[:, None]).sum(axis=1), weights.shape[1] - 1)


def _systematic(rng, pi):
    """Systematic PPS sampling: unit i is selected iff an integer k satisfies
    cum_{i-1} <= k + u < cum_i. With rows summing to 5 and every pi <= 1 this
    selects exactly five distinct units with inclusion probabilities pi."""
    cum = np.cumsum(pi, axis=1)
    prev = np.concatenate([np.zeros((pi.shape[0], 1)), cum[:, :-1]], axis=1)
    u = rng.random((pi.shape[0], 1))
    sel = (np.floor(cum - u + 1e-9) - np.floor(prev - u + 1e-9)) >= 1
    count = sel.sum(axis=1)
    if np.any(count != 5):  # floating-point edge: repair deterministically by pi
        for r in np.nonzero(count != 5)[0]:
            order = np.argsort(-pi[r])
            sel[r] = False
            sel[r, order[:5]] = True
    return sel


def _fill_minutes(m, cap=44.0, total=240.0):
    """Allocate exactly `total` team minutes from sampled per-player minutes.

    If the sampled minutes exceed the total, the lowest-ranked players are
    trimmed first (coaches shorten the bench before cutting starters). If they
    fall short, minutes are scaled up proportionally subject to the cap."""
    S, P = m.shape
    order = np.argsort(-m, axis=1)
    ms = np.take_along_axis(m, order, axis=1)
    before = np.cumsum(ms, axis=1) - ms
    trimmed = np.clip(np.minimum(ms, total - before), 0, None)
    out = np.empty_like(m)
    np.put_along_axis(out, order, trimmed, axis=1)
    for _ in range(8):
        s = out.sum(axis=1, keepdims=True)
        short = total - s
        if np.all(np.abs(short) < 1e-6):
            break
        room = np.where((out > 0) & (out < cap), out, 0)
        out = np.minimum(cap, out + short * room / np.maximum(room.sum(axis=1, keepdims=True), 1e-9))
    return out


class GameInputs:
    """Validated per-team arrays. `teams` is [home, away]; each a dict with list
    fields per player (see projection.build_inputs)."""

    def __init__(self, teams, pace, shifts=(0.0, 0.0), league=None, neutral=False, score_effect=0.0):
        self.teams = teams
        self.pace = float(pace)
        self.shifts = list(shifts)
        self.league = league or {}
        self.neutral = neutral
        # Score effect: logit change in make probability per 10 points of lead
        # (negative for the leader). Captures leaders easing off / trailers pressing,
        # which independent possessions miss; tuned so simulated margin spread
        # matches observed forecast residuals.
        self.score_effect = float(score_effect)


def simulate(inp: GameInputs, sims=10000, seed=42, trace=False, max_ot=10):
    t0 = time.time()
    rng = np.random.default_rng(seed)
    S = int(sims)
    lg = inp.league
    oreb_base = np.array([lg.get("oreb_rate", {}).get(z, .28) for z in ZONES])
    ast_base = np.array([lg.get("assist_rate", {}).get(z, .6) for z in ZONES])
    blk_base = np.array([lg.get("block_rate", {}).get(z, .02) for z in ZONES])
    and1_base = np.array([lg.get("and1_rate", {}).get(z, .02) for z in ZONES])
    nonshoot_pf = lg.get("nonshooting_fouls_per_poss", 0.09)
    ft_oreb = lg.get("ft_oreb_rate", 0.12)
    steal_share = lg.get("steal_share_of_tov", 0.55)

    T = []
    for side, t in enumerate(inp.teams):
        P = len(t["ids"])
        arr = {k: np.asarray(t[k], float) for k in ("active_prob", "min_mean", "min_sd", "usage", "tov", "foul_draw",
                                                    "ft_pct", "three_ft", "ast_w", "oreb_w", "dreb_w", "stl_w", "blk_w", "pf_w")}
        arr["zone_mix"] = np.asarray(t["zone_mix"], float)          # (P, 5)
        arr["zone_logit"] = _logit(np.asarray(t["zone_fg"], float))  # (P, 5)
        arr["def_adj"] = np.asarray(t["def_zone_adj"], float)        # (5,) applied to OPPONENT shots
        arr["P"] = P
        arr["ids"] = t["ids"]
        # Availability: Bernoulli draws; guarantee at least 8 (or all) available by
        # activating the highest-probability inactive players (documented).
        act = rng.random((S, P)) < arr["active_prob"][None, :]
        need = min(8, P)
        order = np.argsort(-arr["active_prob"])
        for j in order:
            short = act.sum(axis=1) < need
            if not short.any():
                break
            act[short, j] = True
        m = np.where(act, np.maximum(0.5, rng.normal(arr["min_mean"], arr["min_sd"], (S, P))), 0.0)
        m = _fill_minutes(m)
        arr["active"] = act
        arr["planned_min"] = m
        T.append(arr)

    box = [{k: np.zeros((S, T[s]["P"])) for k in STATS} for s in (0, 1)]
    score = np.zeros((S, 2))
    quarter = np.zeros((S, 2, 4 + max_ot))
    poss_count = np.zeros((S, 2))
    team_reb = np.zeros((S, 2))

    def on_court_masks(blocks, minutes_frac):
        """(blocks, S, P) boolean masks for each team."""
        out = []
        for s in (0, 1):
            pi = np.clip(minutes_frac[s], 0, 1)
            pi = pi * 5 / np.maximum(pi.sum(axis=1, keepdims=True), 1e-9)
            # Iterate cap at 1 to keep systematic sampling valid.
            for _ in range(4):
                over = pi > 1
                if not over.any():
                    break
                excess = np.where(over, pi - 1, 0).sum(axis=1, keepdims=True)
                pi = np.where(over, 1.0, pi)
                room = np.where(pi < 1, pi, 0)
                pi = pi + excess * room / np.maximum(room.sum(axis=1, keepdims=True), 1e-9)
            perm = np.argsort(-T[s]["min_mean"])  # stable ordering keeps stints coherent
            pi_p = pi[:, perm]
            masks = np.zeros((blocks, S, T[s]["P"]), bool)
            for b in range(blocks):
                sel = _systematic(rng, pi_p)
                masks[b][:, perm] = sel
            out.append(masks)
        return out

    def run_period(blocks, n_poss, offense_start, period_index, live, minutes_frac):
        masks = on_court_masks(blocks, minutes_frac)
        block_minutes = 1.0
        for s in (0, 1):
            box[s]["minutes"] += masks[s].sum(axis=0) * block_minutes * live[:, None]
        max_n = int(n_poss.max()) if live.any() else 0
        offense = offense_start.copy()
        for k in range(max_n):
            act = live & (k < n_poss)
            if not act.any():
                break
            b = np.minimum((k * blocks / np.maximum(n_poss, 1)).astype(int), blocks - 1)
            if blocks == 48:
                qi = np.minimum(b // 12, 3)
            else:
                qi = np.full(S, period_index)
            for side in (0, 1):
                sel = act & (offense == side)
                if sel.any():
                    possession(side, sel, b, qi, masks)
            offense = 1 - offense
        return offense

    def possession(side, sel, b, qi, masks):
        o, d = T[side], T[1 - side]
        idx = np.nonzero(sel)[0]
        on_o = masks[side][b[idx], idx]      # (n, Po)
        on_d = masks[1 - side][b[idx], idx]  # (n, Pd)
        poss_count[idx, side] += 1
        alive = np.ones(len(idx), bool)
        shift = inp.shifts[side]
        for stage in range(6):
            if not alive.any():
                break
            ii = idx[alive]
            oo, dd = on_o[alive], on_d[alive]
            n = len(ii)
            actor = _pick(rng, np.where(oo, o["usage"][None, :], 0) + 1e-12 * oo)
            r = rng.random(n)
            tov = r < o["tov"][actor]
            foul = (~tov) & (r < o["tov"][actor] + o["foul_draw"][actor])
            shot = ~(tov | foul)
            ob, db = box[side], box[1 - side]
            # Non-shooting defensive fouls (no free throws in this baseline).
            nsf = rng.random(n) < nonshoot_pf / 2
            if nsf.any():
                f_idx = _pick(rng, np.where(dd[nsf], d["pf_w"][None, :], 0) + 1e-12)
                np.add.at(db["pf"], (ii[nsf], f_idx), 1)
            # Turnovers.
            if tov.any():
                np.add.at(ob["tov"], (ii[tov], actor[tov]), 1)
                st = tov & (rng.random(n) < steal_share)
                if st.any():
                    thief = _pick(rng, np.where(dd[st], d["stl_w"][None, :], 0) + 1e-12)
                    np.add.at(db["stl"], (ii[st], thief), 1)
            cont = np.zeros(n, bool)
            # Shooting-foul trips.
            if foul.any():
                fi = np.nonzero(foul)[0]
                three = rng.random(len(fi)) < o["three_ft"][actor[fi]]
                att = np.where(three, 3, 2)
                pct = o["ft_pct"][actor[fi]]
                made = (rng.random((len(fi), 3)) < pct[:, None]) & (np.arange(3)[None, :] < att[:, None])
                mk = made.sum(axis=1)
                last_miss = ~made[np.arange(len(fi)), att - 1]
                np.add.at(ob["fta"], (ii[fi], actor[fi]), att)
                np.add.at(ob["ftm"], (ii[fi], actor[fi]), mk)
                np.add.at(ob["pts"], (ii[fi], actor[fi]), mk)
                score[ii[fi], side] += mk
                np.add.at(quarter, (ii[fi], side, qi[ii[fi]]), mk)
                fouler = _pick(rng, np.where(dd[fi], d["pf_w"][None, :], 0) + 1e-12)
                np.add.at(db["pf"], (ii[fi], fouler), 1)
                if last_miss.any():
                    lm = fi[last_miss]
                    off_reb = rng.random(len(lm)) < ft_oreb
                    cont[lm[off_reb]] = rebound(side, ii[lm[off_reb]], oo[lm[off_reb]], True)
                    rebound(1 - side, ii[lm[~off_reb]], dd[lm[~off_reb]], False)
            # Field-goal attempts.
            if shot.any():
                si = np.nonzero(shot)[0]
                a = actor[si]
                zone = _pick(rng, o["zone_mix"][a])
                lead = score[ii[si], side] - score[ii[si], 1 - side]
                logit = o["zone_logit"][a, zone] + d["def_adj"][zone] + shift - inp.score_effect * lead / 10.0
                p = 1 / (1 + np.exp(-logit))
                made = rng.random(len(si)) < p
                three = zone >= 3
                pts = np.where(made, np.where(three, 3, 2), 0)
                rows = ii[si]
                np.add.at(ob["fga"], (rows, a), 1)
                np.add.at(ob["fg3a"], (rows, a), three)
                np.add.at(ob["fgm"], (rows, a), made)
                np.add.at(ob["fg3m"], (rows, a), made & three)
                np.add.at(ob["pts"], (rows, a), pts)
                score[rows, side] += pts
                np.add.at(quarter, (rows, side, qi[rows]), pts)
                # Assists on made shots (eligible: four teammates on court).
                am = made & (rng.random(len(si)) < ast_base[zone])
                if am.any():
                    w = np.where(oo[si[am]], o["ast_w"][None, :], 0)
                    w[np.arange(am.sum()), a[am]] = 0
                    ok = w.sum(axis=1) > 0
                    if ok.any():
                        passer = _pick(rng, w[ok] + 1e-12)
                        np.add.at(ob["ast"], (rows[am][ok], passer), 1)
                # And-one free throw.
                a1 = made & (rng.random(len(si)) < and1_base[zone])
                if a1.any():
                    ftm = rng.random(a1.sum()) < o["ft_pct"][a[a1]]
                    np.add.at(ob["fta"], (rows[a1], a[a1]), 1)
                    np.add.at(ob["ftm"], (rows[a1], a[a1]), ftm)
                    np.add.at(ob["pts"], (rows[a1], a[a1]), ftm)
                    score[rows[a1], side] += ftm
                    np.add.at(quarter, (rows[a1], side, qi[rows[a1]]), ftm)
                    fouler = _pick(rng, np.where(dd[si[a1]], d["pf_w"][None, :], 0) + 1e-12)
                    np.add.at(db["pf"], (rows[a1], fouler), 1)
                miss = ~made
                if miss.any():
                    mi = si[miss]
                    bl = rng.random(len(mi)) < blk_base[zone[miss]] / np.maximum(1 - p[miss], 1e-3)
                    if bl.any():
                        blocker = _pick(rng, np.where(dd[mi[bl]], d["blk_w"][None, :], 0) + 1e-12)
                        np.add.at(db["blk"], (ii[mi[bl]], blocker), 1)
                    # Offensive rebound probability: zone base x lineup rebounding balance.
                    ow = (np.where(oo[mi], o["oreb_w"][None, :], 0)).sum(axis=1)
                    dw = (np.where(dd[mi], d["dreb_w"][None, :], 0)).sum(axis=1)
                    base = oreb_base[zone[miss]]
                    odds = base / (1 - base) * (ow / np.maximum(dw, 1e-6)) / lg.get("oreb_balance", 1.0)
                    p_or = odds / (1 + odds)
                    off_reb = rng.random(len(mi)) < p_or
                    cont[mi[off_reb]] = rebound(side, ii[mi[off_reb]], oo[mi[off_reb]], True)
                    rebound(1 - side, ii[mi[~off_reb]], dd[mi[~off_reb]], False)
            new_alive = np.zeros(len(idx), bool)
            new_alive[np.nonzero(alive)[0]] = cont
            alive = new_alive

    def rebound(side, rows, on, offensive):
        if len(rows) == 0:
            return np.zeros(0, bool)
        t = T[side]
        team = rng.random(len(rows)) < lg.get("team_rebound_share", 0.0)
        w = np.where(on, (t["oreb_w"] if offensive else t["dreb_w"])[None, :], 0) + 1e-12 * on
        who = _pick(rng, w)
        pr = ~team
        np.add.at(box[side]["oreb" if offensive else "dreb"], (rows[pr], who[pr]), 1)
        np.add.at(team_reb, (rows[team], side), 1)
        return np.ones(len(rows), bool)

    # Regulation.
    live = np.ones(S, bool)
    n_reg = np.maximum(60, np.round(rng.normal(inp.pace, 3.5, S) * 2)).astype(int)
    frac = [T[s]["planned_min"] / 48.0 for s in (0, 1)]
    offense = rng.integers(0, 2, S)
    run_period(48, n_reg, offense, 0, live, frac)
    ot = np.zeros(S, int)
    for k in range(max_ot):
        tied = score[:, 0] == score[:, 1]
        if not tied.any():
            break
        ot[tied] += 1
        # Overtime: starters-weighted minutes (top 7 by planned minutes share 25 minutes).
        f = []
        for s in (0, 1):
            pm = T[s]["planned_min"].copy()
            rank = np.argsort(np.argsort(-pm, axis=1), axis=1)
            pm = np.where(rank < 7, pm, 0)
            f.append(pm / np.maximum(pm.sum(axis=1, keepdims=True), 1e-9) * 5)
        n_ot = np.maximum(6, np.round(rng.normal(inp.pace * 5 / 48, 1.2, S) * 2)).astype(int)
        run_period(5, n_ot, rng.integers(0, 2, S), 4 + k, tied, f)
    unresolved = int((score[:, 0] == score[:, 1]).sum())
    if unresolved:
        # Coin-flip completion is reported, never hidden.
        flip = (score[:, 0] == score[:, 1])
        winner = rng.integers(0, 2, S)
        score[flip & (winner == 0), 0] += 1
        quarter[flip & (winner == 0), 0, 4 + max_ot - 1] += 1
    for s in (0, 1):
        box[s]["reb"] = box[s]["oreb"] + box[s]["dreb"]
    checks = accounting(box, score, ot, unresolved)
    return {"box": box, "score": score, "quarter": quarter, "overtime": ot, "possessions": poss_count,
            "team_rebounds": team_reb, "active": [T[s]["active"] for s in (0, 1)], "ids": [T[s]["ids"] for s in (0, 1)],
            "checks": checks, "seconds": round(time.time() - t0, 3), "sims": S, "seed": seed,
            "unresolved_ties": unresolved}


def accounting(box, score, ot, unresolved=0):
    """Basketball accounting identities; raise on any violation."""
    out = {}
    for s in (0, 1):
        b = box[s]
        pts = b["pts"].sum(axis=1)
        diff = np.abs(pts - score[:, s])
        if unresolved == 0:
            assert np.all(diff == 0), "team score != sum of player points"
        assert np.all(b["pts"] == 2 * b["fgm"] + b["fg3m"] + b["ftm"]), "points != 2*FGM + 3PM + FTM"
        assert np.all(b["fgm"] <= b["fga"]) and np.all(b["fg3m"] <= b["fg3a"]) and np.all(b["ftm"] <= b["fta"])
        assert np.all(b["fg3a"] <= b["fga"]) and np.all(b["fg3m"] <= b["fgm"])
        assert np.all(b["ast"].sum(axis=1) <= b["fgm"].sum(axis=1)), "assists exceed made FGs"
        minutes = b["minutes"].sum(axis=1)
        assert np.allclose(minutes, 240 + 25 * ot), "team minutes != 240 + 25*OT"
        out[f"team{s}"] = {"points_reconcile": bool(np.all(diff == 0)), "minutes_reconcile": True,
                           "assists_le_fgm": True, "points_identity": True}
    other = [box[1], box[0]]
    for s in (0, 1):
        assert np.all(box[s]["stl"].sum(axis=1) <= other[s]["tov"].sum(axis=1)), "steals exceed opponent turnovers"
        assert np.all(box[s]["blk"].sum(axis=1) <= (other[s]["fga"] - other[s]["fgm"]).sum(axis=1)), "blocks exceed misses"
    return out
