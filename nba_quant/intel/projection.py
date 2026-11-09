"""Projection engine: availability -> minutes -> opportunity -> efficiency ->
possession simulation -> calibrated outputs.

Only information available before the cutoff is used: games completed before
it, models fitted on those games, and explicit user scenarios. Historical
injury reports are not ingested; availability is a fitted model of prior-game
participation (missed-game streak x recent role), labeled MODELED.
"""
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np

from . import models as M
from .engine import project_minutes
from .sim import GameInputs, ZONES, simulate

ZONE_KEYS = ("rim", "sm", "lm", "c3", "ab3")
ZONE_DB = {"rim": "rim", "short_midrange": "sm", "long_midrange": "lm", "corner_three": "c3", "above_break_three": "ab3"}
ZONE_PRIOR_K = {"rim": 50, "sm": 80, "lm": 80, "c3": 150, "ab3": 200}  # heavy shrinkage on threes
SIM_VERSION = "possession-sim-1.1"
# Walk-forward-tuned calibration of the simulator inputs (see docs, section 8):
# STRENGTH_STRETCH widens the two teams' efficiency targets about their mean
# (time-decayed ridge ratings shrink team differences); SCORE_EFFECT is the
# logit change per 10-point lead that brings simulated margin spread down to
# observed forecast residuals. Both were set on Dec 2025-Feb 2026 games only.
STRENGTH_STRETCH = 1.17
SCORE_EFFECT = 0.035
COMBOS = {"pra": ("pts", "reb", "ast"), "pr": ("pts", "reb"), "pa": ("pts", "ast"), "ra": ("reb", "ast"),
          "stocks": ("stl", "blk"), "fg3m_pts": ("pts",)}
THRESHOLDS = {"pts": [9.5, 14.5, 19.5, 24.5, 29.5, 34.5], "reb": [3.5, 5.5, 7.5, 9.5, 11.5], "ast": [2.5, 4.5, 6.5, 8.5, 10.5],
              "fg3m": [0.5, 1.5, 2.5, 3.5, 4.5], "stl": [0.5, 1.5, 2.5], "blk": [0.5, 1.5, 2.5], "tov": [1.5, 2.5, 3.5, 4.5],
              "pra": [19.5, 24.5, 29.5, 34.5, 39.5, 44.5], "pr": [14.5, 19.5, 24.5, 29.5], "pa": [14.5, 19.5, 24.5, 29.5],
              "ra": [5.5, 8.5, 11.5, 14.5], "stocks": [0.5, 1.5, 2.5, 3.5], "minutes": [19.5, 24.5, 29.5, 34.5]}


def _shrink(n, d, prior, k):
    return (n + k * prior) / (d + k)


class Projector:
    def __init__(self, engine, strength_stretch=None, score_effect=None):
        self.engine = engine
        self.strength_stretch = STRENGTH_STRETCH if strength_stretch is None else strength_stretch
        self.score_effect = SCORE_EFFECT if score_effect is None else score_effect
        self.wh = engine.wh
        self._cache = {}

    # ----------------------------------------------------------- league context
    def league(self, cutoff):
        cutoff = M.iso(cutoff)
        key = ("league", cutoff)
        if key in self._cache:
            return self._cache[key]
        since = M.iso(datetime.fromisoformat(cutoff.replace("Z", "+00:00")) - timedelta(days=400))
        zrows = self.wh.query("""SELECT zone, COUNT(*) n, SUM(made) m, SUM(CASE WHEN made=1 AND assist_id IS NOT NULL THEN 1 ELSE 0 END) ast,
            SUM(CASE WHEN block_id IS NOT NULL THEN 1 ELSE 0 END) blk FROM shots WHERE tipoff < ? AND tipoff >= ? AND team_attempt_heave=0
            GROUP BY zone""", (cutoff, since))
        z = {ZONE_DB[r["zone"]]: r for r in zrows if r["zone"] in ZONE_DB}
        if not z:
            raise ValueError("No shots before cutoff")
        # Sparse windows can lack a zone: fall back to a weak documented prior and flag it.
        fallback = {"rim": (.66, .28), "sm": (.44, .14), "lm": (.41, .16), "c3": (.38, .10), "ab3": (.35, .32)}
        missing = [k for k in ZONE_KEYS if k not in z or not z[k]["n"]]
        for k in missing:
            fg, share = fallback[k]
            z[k] = {"n": 10 * share, "m": 10 * share * fg, "ast": 6 * share * fg, "blk": .2 * share}
        and1 = {ZONE_DB[r["zone"]]: r["n"] for r in self.wh.query("""SELECT s.zone, COUNT(*) n FROM shots s JOIN play_events e
            ON e.game_id = s.game_id AND abs(e.elapsed - s.elapsed) < 0.05 AND e.team_id = s.team_id
            WHERE s.made = 1 AND e.type = 'Free Throw - 1 of 1' AND s.tipoff < ? AND s.tipoff >= ? GROUP BY s.zone""", (cutoff, since))
            if r["zone"] in ZONE_DB}
        reb = self.wh.query("""WITH seqd AS (SELECT game_id, seq, type, team_id, shooting, scoring, points_attempted, x, y, p1,
              LEAD(type) OVER (PARTITION BY game_id ORDER BY seq) nxt, LEAD(team_id) OVER (PARTITION BY game_id ORDER BY seq) nxt_team,
              LEAD(p1) OVER (PARTITION BY game_id ORDER BY seq) nxt_p1
            FROM play_events WHERE game_id IN (SELECT id FROM games WHERE tipoff < ? AND tipoff >= ?))
            SELECT CASE WHEN type LIKE 'Free Throw%' THEN 'ft' WHEN points_attempted = 3 THEN 'three'
                        WHEN x IS NOT NULL AND ((x-25)*(x-25) + y*y) <= 16 THEN 'rim' ELSE 'two' END kind,
                   SUM(CASE WHEN nxt = 'Offensive Rebound' THEN 1 ELSE 0 END) orb,
                   SUM(CASE WHEN nxt IN ('Offensive Rebound','Defensive Rebound') THEN 1 ELSE 0 END) reb,
                   SUM(CASE WHEN nxt IN ('Offensive Rebound','Defensive Rebound') AND nxt_p1 IS NULL THEN 1 ELSE 0 END) team_reb
            FROM seqd WHERE scoring = 0 AND (shooting = 1 OR (type LIKE 'Free Throw%' AND (type LIKE '%2 of 2' OR type LIKE '%1 of 1' OR type LIKE '%3 of 3')))
            GROUP BY kind""", (cutoff, since))
        rb = {r["kind"]: r for r in reb}
        orr = lambda k: rb[k]["orb"] / rb[k]["reb"] if k in rb and rb[k]["reb"] else .27
        tot = self.wh.query("""SELECT SUM(pf) pf, SUM(fta) fta, SUM(tov) tov, SUM(stl) stl, SUM(oreb) oreb, SUM(dreb) dreb
            FROM player_games WHERE tipoff < ? AND tipoff >= ?""", (cutoff, since))[0]
        poss = self.wh.scalar("SELECT SUM(home_poss + away_poss) FROM games WHERE tipoff < ? AND tipoff >= ?", (cutoff, since)) or 1
        trips = .44 * (tot["fta"] or 0)
        team_reb_share = sum(r["team_reb"] for r in reb) / max(sum(r["reb"] for r in reb), 1)
        out = {"zone_fg": {k: z[k]["m"] / z[k]["n"] for k in ZONE_KEYS},
               "zone_share": {k: z[k]["n"] / sum(v["n"] for v in z.values()) for k in ZONE_KEYS},
               "assist_rate": {k: z[k]["ast"] / max(z[k]["m"], 1) for k in ZONE_KEYS},
               "block_rate": {k: z[k]["blk"] / z[k]["n"] for k in ZONE_KEYS},
               "and1_rate": {k: and1.get(k, 0) / max(z[k]["m"], 1) for k in ZONE_KEYS},
               "oreb_rate": {"rim": orr("rim"), "sm": orr("two"), "lm": orr("two"), "c3": orr("three"), "ab3": orr("three")},
               "ft_oreb_rate": orr("ft"),
               "and1_fta_share": sum(and1.values()) / max(tot["fta"] or 1, 1), "team_rebound_share": team_reb_share,
               "steal_share_of_tov": (tot["stl"] or 0) / max(tot["tov"] or 1, 1),
               "nonshooting_fouls_per_poss": max(0.0, ((tot["pf"] or 0) - trips) / poss),
               "window_start": since, "cutoff": cutoff, "fallback_zones": missing}
        self._cache[key] = out
        return out

    # ----------------------------------------------------------- availability model
    def availability_model(self, cutoff):
        """P(plays | consecutive team games missed, recent minutes role), fitted on
        player-team sequences completed before the cutoff (MODELED)."""
        cutoff = M.iso(cutoff)
        key = ("avail", cutoff)
        if key in self._cache:
            return self._cache[key]
        rows = self.wh.query("""SELECT pg.player_id, pg.team_id, pg.game_id, pg.tipoff, pg.minutes, pg.dnp FROM player_games pg
            WHERE pg.tipoff < ? ORDER BY pg.tipoff""", (cutoff,))
        team_games = defaultdict(list)
        for g in self.wh.query("SELECT id, tipoff, home_id, away_id FROM games WHERE tipoff < ? ORDER BY tipoff", (cutoff,)):
            team_games[g["home_id"]].append(g["id"])
            team_games[g["away_id"]].append(g["id"])
        played = defaultdict(dict)
        for r in rows:
            played[(r["player_id"], r["team_id"])][r["game_id"]] = r["minutes"] if not r["dnp"] else 0.0
        counts = defaultdict(lambda: [0, 0])
        for (pid, tid), gm in played.items():
            seq = team_games[tid]
            idx = [i for i, g in enumerate(seq) if g in gm]
            if not idx:
                continue
            first, last = idx[0], idx[-1]
            streak, mins = 0, []
            for i in range(first, last + 1):
                gid = seq[i]
                m = gm.get(gid, 0.0)
                if i > first:
                    role = _role(mins)
                    counts[(_bucket(streak), role)][0] += m > 0
                    counts[(_bucket(streak), role)][1] += 1
                if m > 0:
                    streak = 0
                    mins.append(m)
                else:
                    streak += 1
        table = {f"{k[0]}|{k[1]}": round((v[0] + 1) / (v[1] + 2), 4) for k, v in counts.items()}
        out = {"family": "availability_markov_table", "trained_through": cutoff, "table": table,
               "cells": {f"{k[0]}|{k[1]}": v[1] for k, v in counts.items()},
               "note": "Laplace-smoothed P(appears) by missed-game streak and recent-minutes role; excludes post-trade gaps."}
        self._cache[key] = out
        return out

    def zone_defense(self, cutoff, season, prev, prev_weight=0.5):
        """Opponent zone attempts/makes allowed per defense, current + down-weighted prior season."""
        from .catalog import Agg
        out = defaultdict(Agg)
        for s, w in ((season, 1.0), (prev, prev_weight)):
            if s is None:
                continue
            for r in self.wh.query("""SELECT opponent_id, zone, COUNT(*) n, SUM(made) m FROM shots s JOIN games g ON g.id = s.game_id
                    WHERE s.tipoff < ? AND g.season = ? AND team_attempt_heave = 0 GROUP BY opponent_id, zone""", (cutoff, s)):
                z = ZONE_DB.get(r["zone"])
                if z:
                    out[r["opponent_id"]][f"opp_z_{z}_a"] += w * r["n"]
                    out[r["opponent_id"]][f"opp_z_{z}_m"] += w * r["m"]
        return out

    def four_factors(self, cutoff, season, prev, prev_weight=0.5, k=600.0):
        """Per-team turnover and shooting-foul-trip rates per used possession, for
        and against, shrunk toward league (k used possessions)."""
        from .catalog import Agg
        acc = defaultdict(Agg)
        for s, w in ((season, 1.0), (prev, prev_weight)):
            if s is None:
                continue
            for r in self.wh.query("""SELECT pg.team_id, pg.opponent_id, SUM(fga) fga, SUM(fta) fta, SUM(tov) tov
                    FROM player_games pg JOIN games g ON g.id = pg.game_id WHERE pg.tipoff < ? AND g.season = ?
                    GROUP BY pg.team_id, pg.opponent_id""", (cutoff, s)):
                used = r["fga"] + .44 * r["fta"] + r["tov"]
                for tid, pre in ((r["team_id"], ""), (r["opponent_id"], "opp_")):
                    acc[tid][pre + "used"] += w * used
                    acc[tid][pre + "tov"] += w * r["tov"]
                    acc[tid][pre + "fta"] += w * r["fta"]
        lg_used = sum(a["used"] for a in acc.values()) or 1
        lg_tov = sum(a["tov"] for a in acc.values()) / lg_used
        lg_fta = sum(a["fta"] for a in acc.values()) / lg_used
        out = {"league": {"tov": lg_tov, "fta": lg_fta}}
        for tid, a in acc.items():
            out[tid] = {"tov": _shrink(a["tov"], a["used"], lg_tov, k), "fta": _shrink(a["fta"], a["used"], lg_fta, k),
                        "opp_tov": _shrink(a["opp_tov"], a["opp_used"], lg_tov, k), "opp_fta": _shrink(a["opp_fta"], a["opp_used"], lg_fta, k)}
        return out

    # ----------------------------------------------------------- inputs
    def roster(self, team_id, cutoff, lookback=15):
        """Players whose most recent appearance before cutoff is for this team,
        within the team's last `lookback` games."""
        tips = self.wh.query("""SELECT id FROM games WHERE (home_id=? OR away_id=?) AND tipoff < ? ORDER BY tipoff DESC LIMIT ?""",
                             (team_id, team_id, cutoff, lookback))
        ids = [t["id"] for t in tips]
        if not ids:
            return []
        cands = {r["player_id"] for r in self.wh.query(
            "SELECT DISTINCT player_id FROM player_games WHERE team_id=? AND game_id IN (%s)" % ",".join("?" * len(ids)), (team_id, *ids))}
        out = []
        for pid in cands:
            last = self.wh.query("SELECT team_id FROM player_games WHERE player_id=? AND tipoff < ? ORDER BY tipoff DESC LIMIT 1", (pid, cutoff))
            if last and last[0]["team_id"] == team_id:
                out.append(pid)
        return out

    def player_history(self, pid, team_id, cutoff):
        team_games = [g["id"] for g in self.wh.query(
            "SELECT id FROM games WHERE (home_id=? OR away_id=?) AND tipoff < ? ORDER BY tipoff", (team_id, team_id, cutoff))]
        rows = {r["game_id"]: r for r in self.wh.query(
            "SELECT * FROM player_games WHERE player_id=? AND team_id=? AND tipoff < ? ORDER BY tipoff", (pid, team_id, cutoff))}
        first = next((i for i, g in enumerate(team_games) if g in rows), None)
        seq = []
        if first is not None:
            for g in team_games[first:]:
                r = rows.get(g)
                seq.append((r["minutes"] if r and not r["dnp"] else 0.0, bool(r and r["starter"] and not r["dnp"])))
        return seq

    def build_inputs(self, home, away, cutoff, scenarios=None, model_cutoff=None):
        """`cutoff` bounds roster/minutes/availability history; `model_cutoff`
        (<= cutoff) bounds fitted models and rate aggregates (weekly refits)."""
        cutoff = M.iso(cutoff)
        mc = M.iso(model_cutoff) if model_cutoff else cutoff
        if mc > cutoff:
            raise ValueError("model_cutoff cannot follow cutoff")
        eng = self.engine
        models = eng.models(mc)
        if not models["team"]:
            raise ValueError("Insufficient history before cutoff to fit team ratings")
        lg = self.league(mc)
        avail = self.availability_model(mc)
        season = eng.season_for(cutoff)
        prev = self.wh.scalar("SELECT MAX(season) FROM games WHERE season < ?", (season,))
        cur = eng.player_aggs(mc, season) if eng.season_for(mc) == season else {}
        old = eng.player_aggs(mc, prev) if prev else {}
        zone_def = self.zone_defense(mc, season, prev)
        ff = self.four_factors(mc, season, prev)
        info = {p["id"]: p for p in self.wh.query("SELECT * FROM players")}
        rapm = (models["rapm"] or {}).get("players", {})
        scen = {s["player_id"]: s for s in (scenarios or [])}
        teams, meta, ff_meta = [], [], {}
        for tid in (home, away):
            roster = self.roster(tid, cutoff)
            if len(roster) < 5:
                raise ValueError(f"Team {tid}: fewer than five rostered players before cutoff")
            t = defaultdict(list)
            t["ids"] = []
            players = []
            for pid in roster:
                hist = self.player_history(pid, tid, cutoff)
                mins = [m for m, _ in hist if m > 0]
                if not mins and pid not in scen:
                    continue
                streak = 0
                for m, _ in reversed(hist):
                    if m > 0:
                        break
                    streak += 1
                role = _role(mins[-10:])
                p_active = avail["table"].get(f"{_bucket(streak)}|{role}", 0.5)
                starts = [s for m, s in hist if m > 0][-10:]
                recent_rows = self.wh.query("""SELECT pts, reb, ast, fg3m, stl, blk, tov, minutes FROM player_games
                    WHERE player_id=? AND tipoff < ? AND minutes > 0 ORDER BY tipoff DESC LIMIT 10""", (pid, cutoff))
                baseline = {k: round(float(np.mean([r[k] for r in recent_rows])), 3) for k in
                            ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "minutes")} if recent_rows else None
                a = _combine(cur.get(pid), old.get(pid))
                pos = (info.get(pid) or {}).get("position", "F")
                rates = player_rates(a, lg)
                min_mean = project_minutes(mins[-20:]) if mins else 10.0
                min_sd = max(3.0, float(np.std(mins[-15:])) if len(mins) > 2 else 6.0)
                s = scen.get(pid, {})
                if s.get("active_probability") is not None:
                    p_active = float(s["active_probability"])
                if s.get("minutes") is not None:
                    min_mean, min_sd = float(s["minutes"]), 1.5
                if s.get("minutes_cap") is not None:
                    min_mean = min(min_mean, float(s["minutes_cap"]))
                players.append({"id": pid, "name": (info.get(pid) or {}).get("name", pid), "position": pos,
                                "active_prob": round(p_active, 4), "start_prob": round(sum(starts) / len(starts), 3) if starts else 0.0,
                                "min_mean": round(min_mean, 2), "min_sd": round(min_sd, 2), "streak_missed": streak,
                                "games_with_team": len(mins), "role": role, "rapm": rapm.get(pid, {}), **rates,
                                "minutes_cap": s.get("minutes_cap"), "baseline_last10": baseline})
            players.sort(key=lambda p: -p["min_mean"] * p["active_prob"])
            players = players[:17]
            # Four-factors matchup anchoring: team rate x opponent allowed rate / league.
            opp = away if tid == home else home
            lgf = ff["league"]
            w = np.array([p["usage"] * p["active_prob"] * p["min_mean"] for p in players])
            w = w / max(w.sum(), 1e-9)
            for key, rate_key, conv in (("tov", "tov", 1.0), ("foul_draw", "fta", (1 - lg.get("and1_fta_share", .1)) / 2.05)):
                target = ff.get(tid, {}).get(rate_key, lgf[rate_key]) * ff.get(opp, {}).get("opp_" + rate_key, lgf[rate_key]) / lgf[rate_key] * conv
                current = float(np.dot(w, [p[key] for p in players]))
                factor = target / current if current > 0 else 1.0
                for p in players:
                    p[key] = round(p[key] * factor, 5)
                ff_meta[f"{tid}:{key}"] = {"target": round(target, 4), "factor": round(factor, 4)}
            for p in players:
                t["ids"].append(p["id"])
                for k in ("active_prob", "min_mean", "min_sd", "usage", "tov", "foul_draw", "ft_pct", "three_ft", "ast_w",
                          "oreb_w", "dreb_w", "stl_w", "blk_w", "pf_w"):
                    t[k].append(p[k])
                t["zone_mix"].append(p["zone_mix"])
                t["zone_fg"].append(p["zone_fg"])
            t["def_zone_adj"] = defense_zone_adjustment(zone_def.get(tid, {}), lg)
            teams.append(dict(t))
            meta.append({"team_id": tid, "players": players, "four_factors": {k.split(":")[1]: v for k, v in ff_meta.items() if k.startswith(tid + ":")}, "def_zone_adj": dict(zip(ZONE_KEYS, np.round(t["def_zone_adj"], 4).tolist()))})
        # Team matchup model -> target efficiency and pace.
        tm = models["team"]
        th, ta_ = tm["teams"].get(home), tm["teams"].get(away)
        if not th or not ta_:
            raise ValueError("Team ratings unavailable for one of the teams")
        neutral = False
        hca = tm["home_court_per100"]
        target = [th["ortg_adj"] + ta_["drtg_adj"] - tm["league_ortg"] + hca,
                  ta_["ortg_adj"] + th["drtg_adj"] - tm["league_ortg"]]
        # Availability adjustment through RAPM: projected rotation vs season rotation.
        adj = []
        for side, m in enumerate(meta):
            proj_o, proj_d = rotation_rapm(m["players"], rapm, projected=True)
            base_o, base_d = rotation_rapm(m["players"], rapm, projected=False)
            adj.append({"off": proj_o - base_o, "def": proj_d - base_d})
            m["rapm_adjustment"] = {"offense": round(proj_o - base_o, 3), "defense": round(proj_d - base_d, 3)}
        target[0] += adj[0]["off"] - adj[1]["def"]
        target[1] += adj[1]["off"] - adj[0]["def"]
        mid = (target[0] + target[1]) / 2
        target = [mid + self.strength_stretch * (t - mid) for t in target]
        pace = th["pace"] * ta_["pace"] / tm["league_pace"]
        lg = dict(lg)
        lg["oreb_balance"] = league_oreb_balance(meta)
        return {"inputs": GameInputs(teams, pace, league=lg, neutral=neutral, score_effect=self.score_effect), "meta": meta, "target_ortg": target,
                "pace": pace, "team_model": {"home": th, "away": ta_, "home_court_per100": hca, "league_ortg": tm["league_ortg"],
                                             "margin_sigma": tm["margin_sigma"], "trained_through": tm["trained_through"]},
                "availability_model": {k: avail[k] for k in ("family", "trained_through", "note")}, "league": lg}

    # ----------------------------------------------------------- calibration + run
    def calibrate_shifts(self, built, seed, pilot=1500):
        inp = built["inputs"]
        target = np.array(built["target_ortg"]) / 100
        res = []
        for shift in (0.0, 0.25):
            inp.shifts = [shift, shift]
            r = simulate(inp, pilot, seed + 7919)
            res.append(r["score"].sum(axis=0) / r["possessions"].sum(axis=0))
        slope = (res[1] - res[0]) / 0.25
        shifts = np.clip((target - res[0]) / np.maximum(slope, 1e-3), -1.0, 1.0)
        inp.shifts = shifts.tolist()
        return {"pilot_ppp_at_zero": np.round(res[0], 4).tolist(), "slope": np.round(slope, 4).tolist(),
                "target_ppp": np.round(target, 4).tolist(), "shifts": np.round(shifts, 4).tolist()}

    def project(self, home, away, cutoff, sims=10000, seed=42, scenarios=None, tipoff=None, model_cutoff=None, pilot=1500):
        cutoff = M.iso(cutoff)
        key = hashlib.sha256(json.dumps([home, away, cutoff, model_cutoff, sims, seed, scenarios or [], SIM_VERSION,
                                         self.strength_stretch, self.score_effect], sort_keys=True).encode()).hexdigest()[:24]
        if key in self._cache:
            return self._cache[key]
        built = self.build_inputs(home, away, cutoff, scenarios, model_cutoff)
        cal = self.calibrate_shifts(built, seed, pilot)
        result = simulate(built["inputs"], sims, seed)
        out = summarize(result, built, cal)
        out.update({"key": key, "home_id": home, "away_id": away, "cutoff": cutoff, "tipoff": tipoff,
                    "model_cutoff": M.iso(model_cutoff) if model_cutoff else cutoff,
                    "simulations": sims, "seed": seed, "scenarios": scenarios or [], "version": SIM_VERSION})
        if len(self._cache) > 40:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = out
        return out


def _bucket(streak):
    return "0" if streak == 0 else "1" if streak == 1 else "2-3" if streak <= 3 else "4-9" if streak <= 9 else "10+"


def _role(mins):
    if not mins:
        return "none"
    m = float(np.mean(mins[-10:]))
    return "starter" if m >= 28 else "rotation" if m >= 16 else "bench" if m >= 6 else "deep"


def _combine(cur, old, old_weight=0.5):
    """Count-weighted blend: current season + down-weighted previous season."""
    from .catalog import Agg
    out = Agg()
    for src, w in ((cur, 1.0), (old, old_weight)):
        if not src:
            continue
        for k, v in src.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in (
                    "rapm", "orapm", "drapm", "rapm_sd", "apm", "proj_min", "min_sd", "pts_sd", "l5_usg", "l10_ts",
                    "net_adj", "ortg_adj", "drtg_adj", "sos"):
                out[k] = out[k] + w * v
    return out


def player_rates(a, lg):
    """Shrunk per-player event rates used by the simulator (MODELED inputs).

    Shooting-foul trips exclude and-one free throws, which the simulator adds
    separately after made field goals: trips = FTA * (1 - and1 share) / 2.05."""
    used = a["fga"] + .44 * a["fta"] + a["tov"]
    trips = a["fta"] * (1 - lg.get("and1_fta_share", 0.1)) / 2.05
    team_used = a["on_tm_fga"] + .44 * a["on_tm_fta"] + a["on_tm_tov"]
    usage = _shrink(used, team_used, 0.19, 150)
    tov = _shrink(a["tov"], used, 0.125, 120)
    foul = _shrink(trips, used, 0.085, 120)
    shots = sum(a[f"z_{z}_a"] for z in ZONE_KEYS)
    mix = [_shrink(a[f"z_{z}_a"], shots, lg["zone_share"][z], 40) for z in ZONE_KEYS]
    fg = [_shrink(a[f"z_{z}_m"], a[f"z_{z}_a"], lg["zone_fg"][z], ZONE_PRIOR_K[z]) for z in ZONE_KEYS]
    poss_played = (a["on_po"] + a["on_pd"]) / 2
    return {"usage": round(usage, 4), "tov": round(tov, 4), "foul_draw": round(foul, 4),
            "ft_pct": round(_shrink(a["ftm"], a["fta"], .78, 40), 4),
            "three_ft": round(min(.2, .08 * _shrink(a["fg3a"], a["fga"], .4, 50)), 4),
            "ast_w": round(_shrink(a["ast"], a["on_po"], .055, 300), 5),
            "oreb_w": round(_shrink(a["oreb"], a["on_oreb"] + a["on_opp_dreb"], .052, 300), 5),
            "dreb_w": round(_shrink(a["dreb"], a["on_dreb"] + a["on_opp_oreb"], .148, 300), 5),
            "stl_w": round(_shrink(a["stl"], a["on_pd"], .016, 300), 5),
            "blk_w": round(_shrink(a["blk"], a["on_pd"], .01, 300), 5),
            "pf_w": round(_shrink(a["pf"], poss_played, .038, 300), 5),
            "zone_mix": [round(x / sum(mix), 4) for x in mix], "zone_fg": [round(x, 4) for x in fg]}


def defense_zone_adjustment(ta, lg, k=400):
    """Logit shift applied to opponent shots by zone, from shrunk zone FG% allowed."""
    out = []
    for z in ZONE_KEYS:
        allowed = _shrink(ta[f"opp_z_{z}_m"], ta[f"opp_z_{z}_a"], lg["zone_fg"][z], k)
        out.append(math.log(allowed / (1 - allowed)) - math.log(lg["zone_fg"][z] / (1 - lg["zone_fg"][z])))
    return out


def rotation_rapm(players, rapm, projected):
    """Five-man-equivalent ORAPM/DRAPM sum: minutes share x 5."""
    if projected:
        w = np.array([p["active_prob"] * p["min_mean"] for p in players])
    else:
        w = np.array([p["min_mean"] * (p["games_with_team"] > 0) for p in players])
    if w.sum() <= 0:
        return 0.0, 0.0
    w = w / w.sum() * 5
    o = sum(wi * rapm.get(p["id"], {}).get("orapm", 0.0) for wi, p in zip(w, players))
    d = sum(wi * rapm.get(p["id"], {}).get("drapm", 0.0) for wi, p in zip(w, players))
    return float(o), float(d)


def league_oreb_balance(meta):
    """Normalizer so an average offense vs average defense reproduces the zone base OREB rate."""
    ow = [p["oreb_w"] for m in meta for p in m["players"]]
    dw = [p["dreb_w"] for m in meta for p in m["players"]]
    return (np.mean(ow) / np.mean(dw)) if ow and dw else 1.0


def _dist(x):
    x = np.asarray(x, float)
    q = np.quantile(x, [.05, .1, .25, .5, .75, .9, .95])
    return {"mean": round(float(x.mean()), 3), "sd": round(float(x.std()), 3), "p05": round(float(q[0]), 2),
            "p10": round(float(q[1]), 2), "p25": round(float(q[2]), 2), "median": round(float(q[3]), 2),
            "p75": round(float(q[4]), 2), "p90": round(float(q[5]), 2), "p95": round(float(q[6]), 2)}


CDF_STATS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")


def _cdf(x, tol=5e-4):
    """Compact empirical CDF over integers 0..k, trimmed once F >= 1 - tol."""
    x = np.asarray(x, int)
    if not len(x):
        return None
    counts = np.bincount(np.clip(x, 0, 200))
    F = np.cumsum(counts) / len(x)
    k = int(np.searchsorted(F, 1 - tol)) + 1
    return [round(float(v), 4) for v in F[:k]]


def summarize(r, built, cal):
    S = r["sims"]
    score = r["score"]
    margin = score[:, 0] - score[:, 1]
    total = score.sum(axis=1)
    p = float(np.mean(margin > 0))
    se = math.sqrt(max(p * (1 - p), 1e-9) / S)
    teams = []
    for s in (0, 1):
        b = r["box"][s]
        poss = r["possessions"][:, s]
        teams.append({"team_id": built["meta"][s]["team_id"], "points": _dist(score[:, s]),
                      "quarters": [round(float(r["quarter"][:, s, q].mean()), 2) for q in range(4)],
                      "overtime_points": round(float(r["quarter"][:, s, 4:].sum(axis=1).mean()), 3),
                      "possessions": _dist(poss), "ortg": round(float(100 * score[:, s].sum() / poss.sum()), 2),
                      "target_ortg": round(built["target_ortg"][s], 2),
                      **{k: _dist(b[k].sum(axis=1)) for k in ("reb", "ast", "tov", "fg3a", "fg3m", "fta", "stl", "blk", "pf")},
                      "rapm_adjustment": built["meta"][s]["rapm_adjustment"], "def_zone_adj": built["meta"][s]["def_zone_adj"]})
    players = []
    for s in (0, 1):
        b = r["box"][s]
        for j, pmeta in enumerate(built["meta"][s]["players"]):
            played = b["minutes"][:, j] > 0
            stats = {k: b[k][:, j] for k in ("pts", "reb", "ast", "fg3m", "fg3a", "fgm", "fga", "ftm", "fta", "stl", "blk", "tov", "pf", "minutes", "oreb", "dreb")}
            for c, keys in COMBOS.items():
                if c != "fg3m_pts":
                    stats[c] = sum(stats[k] for k in keys)
            cats = np.stack([stats["pts"] >= 10, stats["reb"] >= 10, stats["ast"] >= 10, stats["stl"] >= 10, stats["blk"] >= 10])
            n10 = cats.sum(axis=0)
            dists = {k: _dist(v) for k, v in stats.items()}
            cond = {k: _dist(v[played]) for k, v in stats.items() if k in ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "minutes", "pra")} if played.any() else {}
            thresholds = {k: {str(t): round(float(np.mean(stats[k] > t)), 4) for t in ts} for k, ts in THRESHOLDS.items() if k in stats}
            corr = {}
            for a_, b_ in (("pts", "reb"), ("pts", "ast"), ("reb", "ast"), ("pts", "minutes")):
                x, y = stats[a_], stats[b_]
                corr[f"{a_}~{b_}"] = round(float(np.corrcoef(x, y)[0, 1]), 3) if x.std() > 0 and y.std() > 0 else None
            team_pts = score[:, s]
            corr["pts~team_pts"] = round(float(np.corrcoef(stats["pts"], team_pts)[0, 1]), 3) if stats["pts"].std() > 0 else None
            players.append({"id": pmeta["id"], "name": pmeta["name"], "team_id": built["meta"][s]["team_id"], "position": pmeta["position"],
                            "active_prob_input": pmeta["active_prob"], "played_prob": round(float(played.mean()), 4),
                            "start_prob": pmeta["start_prob"], "minutes_input": {"mean": pmeta["min_mean"], "sd": pmeta["min_sd"], "cap": pmeta["minutes_cap"]},
                            "foul_trouble_prob": round(float(np.mean(stats["pf"] >= 4)), 4),
                            "stats": dists, "conditional_on_playing": cond, "thresholds": thresholds,
                            "double_double": round(float(np.mean(n10 >= 2)), 4), "triple_double": round(float(np.mean(n10 >= 3)), 4),
                            "correlations": corr, "drivers": drivers(pmeta, built["meta"][1 - s]),
                            "baseline_last10": pmeta.get("baseline_last10"),
                            "cdf_conditional": {k: _cdf(stats[k][played]) for k in CDF_STATS} if played.sum() >= 20 else None,
                            "inputs": {k: pmeta[k] for k in ("usage", "tov", "foul_draw", "ft_pct", "zone_mix", "zone_fg", "streak_missed", "role", "games_with_team")}})
    hist_edges = np.arange(np.floor(margin.min()) - .5, np.ceil(margin.max()) + 1.5, 1)
    mh, _ = np.histogram(margin, bins=hist_edges)
    th_edges = np.arange(np.floor(total.min() / 2) * 2 - 1, np.ceil(total.max() / 2) * 2 + 3, 2)
    tth, _ = np.histogram(total, bins=th_edges)
    tm = built["team_model"]
    ratings_margin = (tm["home"]["net_adj"] - tm["away"]["net_adj"] + tm["home_court_per100"]) * built["pace"] / 100
    ratings_wp = 0.5 * (1 + math.erf(ratings_margin / (tm["margin_sigma"] * math.sqrt(2))))
    return {"home_win_probability": round(p, 4), "away_win_probability": round(1 - p, 4),
            "win_probability_mc_se": round(se, 5), "ratings_model_win_probability": round(ratings_wp, 4),
            "ratings_model_margin": round(ratings_margin, 2),
            "margin": _dist(margin), "total": _dist(total), "overtime_probability": round(float(np.mean(r["overtime"] > 0)), 4),
            "margin_histogram": {"edges": hist_edges.tolist(), "counts": mh.tolist()},
            "total_histogram": {"edges": th_edges.tolist(), "counts": tth.tolist()},
            "spread_cover": {str(x): round(float(np.mean(margin > x)), 4) for x in (-10.5, -7.5, -5.5, -3.5, -1.5, 1.5, 3.5, 5.5, 7.5, 10.5)},
            "total_over": {str(x + .5): round(float(np.mean(total > x + .5)), 4)
                           for x in range(int(np.percentile(total, 10)) // 5 * 5, int(np.percentile(total, 90)) + 5, 5)},
            "teams": teams, "players": players, "pace": round(built["pace"], 2), "calibration": cal,
            "checks": r["checks"], "seconds": r["seconds"], "unresolved_ties": r["unresolved_ties"],
            "team_model": tm, "availability_model": built["availability_model"],
            "league_params": {k: v for k, v in built["league"].items() if k not in ("cutoff",)}}


def drivers(p, opp_meta):
    """Interpretable drivers: player rates vs league priors and opponent zone defense."""
    out = []
    mix = dict(zip(ZONE_KEYS, p["zone_mix"]))
    adj = opp_meta["def_zone_adj"]
    weighted = sum(mix[z] * adj[z] for z in ZONE_KEYS)
    out.append({"driver": "Opponent shot defense on this player's zone mix", "value": round(weighted, 3),
                "direction": "harder" if weighted < 0 else "easier"})
    out.append({"driver": "Usage share", "value": p["usage"]})
    out.append({"driver": "Projected minutes", "value": p["min_mean"]})
    out.append({"driver": "Active probability", "value": p["active_prob"]})
    if opp_meta.get("rapm_adjustment"):
        out.append({"driver": "Opponent availability (defensive RAPM change)", "value": opp_meta["rapm_adjustment"]["defense"]})
    return out
