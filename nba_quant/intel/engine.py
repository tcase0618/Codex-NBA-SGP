"""Metric engine: aggregates warehouse rows into player and team profiles.

Windows are defined by an exclusive cutoff (information available before a
tipoff) plus a season and optional filters. Models used by MODELED metrics are
fitted on games strictly before the same cutoff. Results are cached per window.
"""
import math
import statistics
import threading
from collections import defaultdict
from datetime import datetime

import numpy as np

from . import models as M
from .arenas import elevation, miles, tz
from .catalog import ARCHETYPES, PLAYER, POSITION, TEAM, Agg

POS_GROUP = {"PG": "G", "SG": "G", "G": "G", "SF": "F", "PF": "F", "F": "F", "C": "C"}
SPECIALIZATION = {"PG": "PG", "SG": "SG", "G": "SG", "SF": "SF", "F": "SF", "PF": "PF", "C": "C"}
BOX = ("minutes", "pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb", "ast", "tov", "stl", "blk", "pf")


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _sd(values):
    return round(statistics.pstdev(values), 3) if len(values) > 1 else None


class Engine:
    def __init__(self, wh):
        self.wh = wh
        self._cache = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ windows
    def latest_cutoff(self):
        """Six hours after the most recent ingested tipoff (all of its games final)."""
        last = self.wh.scalar("SELECT MAX(tipoff) FROM games")
        dt = _dt(last)
        return M.iso(datetime.fromtimestamp(dt.timestamp() + 6 * 3600, dt.tzinfo))

    def season_for(self, cutoff):
        return self.wh.scalar("SELECT season FROM games WHERE tipoff < ? ORDER BY tipoff DESC LIMIT 1", (M.iso(cutoff),))

    def season_start(self, season):
        return self.wh.scalar("SELECT MIN(tipoff) FROM games WHERE season = ?", (season,))

    def seasons(self):
        return self.wh.query("SELECT season, COUNT(*) games, MIN(tipoff) first, MAX(tipoff) last FROM games GROUP BY season ORDER BY season")

    def _cached(self, key, fn):
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        value = fn()
        with self._lock:
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = value
        return value

    def models(self, cutoff):
        cutoff = M.iso(cutoff)

        def build():
            return {"shot": M.train_shot_model(self.wh, cutoff), "rapm": M.fit_rapm(self.wh, cutoff),
                    "apm": M.fit_rapm(self.wh, cutoff, lam=300.0), "team": M.fit_team_ratings(self.wh, cutoff)}
        return self._cached(("models", cutoff), build)

    def _games(self, cutoff, season):
        return self.wh.query("SELECT * FROM games WHERE tipoff < ? AND season = ? ORDER BY tipoff", (cutoff, season))

    # ------------------------------------------------------------ players
    def player_aggs(self, cutoff, season=None, player_id=None, game_ids=None):
        cutoff = M.iso(cutoff)
        season = season or self.season_for(cutoff)
        key = ("pagg", cutoff, season, player_id, tuple(sorted(game_ids)) if game_ids else None)
        return self._cached(key, lambda: self._player_aggs(cutoff, season, player_id, game_ids))

    def _player_aggs(self, cutoff, season, player_id, game_ids):
        mdl = self.models(cutoff)
        gid_filter = set(game_ids) if game_ids else None
        games = {g["id"]: g for g in self._games(cutoff, season) if not gid_filter or g["id"] in gid_filter}
        if not games:
            return {}
        pfilter = " AND pg.player_id = ?" if player_id else ""
        params = (cutoff, season) + ((player_id,) if player_id else ())
        rows = self.wh.query(f"""SELECT pg.* FROM player_games pg JOIN games g ON g.id = pg.game_id
            WHERE pg.tipoff < ? AND g.season = ?{pfilter} ORDER BY pg.tipoff""", params)
        team_tot = defaultdict(lambda: defaultdict(float))
        for r in self.wh.query("""SELECT pg.game_id, pg.team_id, SUM(fga) fga, SUM(fta) fta, SUM(tov) tov, SUM(fgm) fgm,
                SUM(minutes) min, SUM(pts) pts FROM player_games pg JOIN games g ON g.id = pg.game_id
                WHERE pg.tipoff < ? AND g.season = ? GROUP BY pg.game_id, pg.team_id""", (cutoff, season)):
            team_tot[(r["game_id"], r["team_id"])].update(r)
        A = defaultdict(Agg)
        per_game = defaultdict(list)
        for r in rows:
            if r["game_id"] not in games:
                continue
            a = A[r["player_id"]]
            a["team_id"] = r["team_id"]
            a["last_tip"] = r["tipoff"]
            if r["dnp"] or not r["minutes"]:
                a["dnp"] += 1
                continue
            g = games[r["game_id"]]
            a["gp"] += 1
            a["gp_team"] = a["gp_team"] + 1 if a.get("gp_team_id") == r["team_id"] else 1
            a["gp_team_id"] = r["team_id"]
            a["starts"] += r["starter"]
            for k in BOX:
                a["min" if k == "minutes" else k] += r[k] or 0
            a["on_sec"] += r["on_seconds"] or 0
            a["on_pf"] += r["on_pts_for"] or 0
            a["on_pa"] += r["on_pts_against"] or 0
            a["on_po"] += r["on_poss_off"] or 0
            a["on_pd"] += r["on_poss_def"] or 0
            a["on_oreb"] += r["on_oreb"] or 0
            a["on_dreb"] += r["on_dreb"] or 0
            a["on_opp_oreb"] += r["on_opp_oreb"] or 0
            a["on_opp_dreb"] += r["on_opp_dreb"] or 0
            home = r["team_id"] == g["home_id"]
            tp, op = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
            tposs, oposs = (g["home_poss"], g["away_poss"]) if home else (g["away_poss"], g["home_poss"])
            a["off_pf"] += tp - (r["on_pts_for"] or 0)
            a["off_pa"] += op - (r["on_pts_against"] or 0)
            a["off_po"] += (tposs or 0) - (r["on_poss_off"] or 0)
            a["off_pd"] += (oposs or 0) - (r["on_poss_def"] or 0)
            tt = team_tot[(r["game_id"], r["team_id"])]
            usg_den = tt["fga"] + .44 * tt["fta"] + tt["tov"]
            per_game[r["player_id"]].append({
                "tip": r["tipoff"], "min": r["minutes"], "pts": r["pts"], "reb": r["reb"], "ast": r["ast"],
                "usg": 100 * (r["fga"] + .44 * r["fta"] + r["tov"]) * (tt["min"] / 5) / (r["minutes"] * usg_den) if usg_den and r["minutes"] else None,
                "ts_n": r["pts"], "ts_d": 2 * (r["fga"] + .44 * r["fta"]), "game_id": r["game_id"]})
        # On-court team possessions-used for usage rate (stints).
        sql = """SELECT s.home_lineup, s.away_lineup, s.home_fga, s.away_fga, s.home_fta, s.away_fta, s.home_tov,
                 s.away_tov, s.game_id FROM lineup_stints s JOIN games g ON g.id = s.game_id WHERE s.tipoff < ? AND g.season = ?"""
        for s in self.wh.query(sql, (cutoff, season)):
            if s["game_id"] not in games:
                continue
            for side in ("home", "away"):
                for pid in s[f"{side}_lineup"].split(","):
                    if pid in A:
                        a = A[pid]
                        a["on_tm_fga"] += s[f"{side}_fga"]
                        a["on_tm_fta"] += s[f"{side}_fta"]
                        a["on_tm_tov"] += s[f"{side}_tov"]
        # Shots: offense, playmaking, rim protection while on court.
        shots = self.wh.query("""SELECT s.* FROM shots s JOIN games g ON g.id = s.game_id
            WHERE s.tipoff < ? AND g.season = ?""", (cutoff, season))
        shots = [s for s in shots if s["game_id"] in games]
        xs = M.predict_shots(mdl["shot"], shots) if mdl["shot"] and shots else np.full(len(shots), np.nan)
        for s, x in zip(shots, xs):
            heave = s["team_attempt_heave"]
            value = s["points_attempted"]
            made = s["made"]
            zone = {"rim": "rim", "short_midrange": "sm", "long_midrange": "lm", "corner_three": "c3",
                    "above_break_three": "ab3"}.get(s["zone"])
            fam = s["family"]
            text = (s["type"] or "").lower()
            clutch = s["period"] >= 4 and s["clock"] <= 300 and abs(s["score_margin"] or 0) <= 5
            sh = s["shooter_id"]
            if sh in A and not heave:
                a = A[sh]
                a["s_n"] += 1
                a["s_fgm"] += made
                a["s_pts"] += s["points"]
                a["s_dist"] += s["distance_ft"] or 0
                if not math.isnan(x):
                    a["s_xfg"] += x
                    a["s_xpts"] += x * value
                if zone:
                    a[f"z_{zone}_a"] += 1
                    a[f"z_{zone}_m"] += made
                    if zone == "rim" and not math.isnan(x):
                        a["z_rim_x"] += x
                if zone in ("rim", "sm") and made:
                    a["paint_pts"] += s["points"]
                if s["transition"]:
                    a["trans_a"] += 1
                    a["trans_pts"] += s["points"]
                if s["second_chance"]:
                    a["sc_pts"] += s["points"]
                if fam == "tip" or "putback" in text:
                    a["putback_a"] += 1
                    a["putback_m"] += made
                    a["putback_pts"] += s["points"]
                if fam == "pullup":
                    a["pull_a"] += 1
                    a["pull_m"] += made
                if fam == "stepback":
                    a["step_a"] += 1
                    a["step_m"] += made
                    a["step3_m"] += made and value == 3
                if fam in ("pullup", "stepback") and value == 3:
                    a["pull3_a"] += 1
                    a["pull3_m"] += made
                if fam == "jumper" and "turnaround" not in text and "fade" not in text:
                    a["cs_a"] += 1
                    a["cs_m"] += made
                    if value == 3:
                        a["cs3_a"] += 1
                        a["cs3_m"] += made
                if fam == "dunk":
                    a["dunk_a"] += 1
                    a["dunk_m"] += made
                if "cutting" in text:
                    a["cut_a"] += 1
                    a["cut_pts"] += s["points"]
                if made and s["assist_id"]:
                    a["asd_m"] += 1
                    a["asd_3m"] += value == 3
                if (s["seconds_into_possession"] or 0) >= 18:
                    a["late_a"] += 1
                    a["late_pts"] += s["points"]
                if clutch:
                    a["clutch_a"] += 1
                    a["clutch_pts"] += s["points"]
            ast = s["assist_id"]
            if ast in A and made:
                a = A[ast]
                a["ast_pts"] += s["points"]
                a["ast_rim"] += zone == "rim"
                a["ast_c3"] += zone == "c3"
                a["ast_3"] += value == 3
                if clutch:
                    a["clutch_ast_pts"] += s["points"]
            blk = s["block_id"]
            if blk in A and zone == "rim":
                A[blk]["blk_rim"] += 1
            if heave:
                continue
            for pid in (s["defense_lineup"] or "").split(","):
                if pid in A:
                    a = A[pid]
                    a["def_n"] += 1
                    a["def_fgm"] += made
                    if not math.isnan(x):
                        a["def_xfg"] += x
                    if zone == "rim":
                        a["def_rim_a"] += 1
                        a["def_rim_m"] += made
                        if not math.isnan(x):
                            a["def_rim_x"] += x
                    if zone in ("rim", "sm") and made:
                        a["def_paint_pts"] += s["points"]
        # Turnover types and early fouls from play events.
        ev = self.wh.query("""SELECT e.game_id, e.p1, e.type, e.period FROM play_events e JOIN games g ON g.id = e.game_id
            WHERE g.tipoff < ? AND g.season = ? AND (e.type LIKE '%Turnover%' OR (e.type LIKE '%Foul%' AND e.type NOT LIKE '%Technical%'))""",
                           (cutoff, season))
        early = defaultdict(int)
        for e in ev:
            if e["game_id"] not in games or e["p1"] not in A:
                continue
            t = e["type"].lower()
            if "turnover" in t:
                if "bad pass" in t:
                    A[e["p1"]]["tov_bad"] += 1
                elif "lost ball" in t:
                    A[e["p1"]]["tov_lost"] += 1
            elif e["period"] <= 2 and "offensive foul turnover" not in t:
                early[(e["game_id"], e["p1"])] += 1
        for (gid, pid), n in early.items():
            if n >= 3:
                A[pid]["foul_trouble_g"] += 1
        # Games missed: team games after the player's first appearance for his latest team
        # that have no positive-minute row (ESPN omits inactive players from box scores).
        team_tips = defaultdict(list)
        for g in games.values():
            team_tips[g["home_id"]].append(g["tipoff"])
            team_tips[g["away_id"]].append(g["tipoff"])
        first_with_team = {}
        for r in rows:
            if r["game_id"] in games and A.get(r["player_id"], {}).get("team_id") == r["team_id"]:
                first_with_team.setdefault(r["player_id"], r["tipoff"])
        for pid, a in A.items():
            start = first_with_team.get(pid)
            if start:
                a["dnp"] = max(a["dnp"], sum(1 for t in team_tips[a["team_id"]] if t >= start) - a["gp_team"])
        # Per-game derived consistency, projections, models.
        rapm = (mdl["rapm"] or {}).get("players", {})
        apm = (mdl["apm"] or {}).get("players", {})
        for pid, a in A.items():
            pg = per_game.get(pid, [])
            a["fg2a"] = a["fga"] - a["fg3a"]
            a["z_mid_a"] = a["z_sm_a"] + a["z_lm_a"]
            a["reb_chances_o"] = a["on_oreb"] + a["on_opp_dreb"]
            a["reb_chances_d"] = a["on_dreb"] + a["on_opp_oreb"]
            a["reb_chances"] = a["reb_chances_o"] + a["reb_chances_d"]
            if pg:
                a["min_sd"] = _sd([x["min"] for x in pg])
                a["pts_sd"] = _sd([x["pts"] for x in pg])
                l5 = [x["usg"] for x in pg[-5:] if x["usg"] is not None]
                a["l5_usg"] = round(sum(l5) / len(l5), 2) if l5 else None
                l10n, l10d = sum(x["ts_n"] for x in pg[-10:]), sum(x["ts_d"] for x in pg[-10:])
                a["l10_ts"] = round(100 * l10n / l10d, 2) if l10d else None
                a["proj_min"] = round(project_minutes([x["min"] for x in pg]), 2)
                a["recent_games"] = [x["game_id"] for x in pg[-10:]]
            if pid in rapm:
                a.update({k: rapm[pid][k] for k in ("rapm", "orapm", "drapm")})
                a["rapm_sd"] = round(math.hypot(rapm[pid]["orapm_sd"], rapm[pid]["drapm_sd"]), 3)
            if pid in apm:
                a["apm"] = apm[pid]["rapm"]
        return dict(A)

    # ------------------------------------------------------------ evaluation
    @staticmethod
    def evaluate(defs, a):
        out = []
        for m in defs:
            value = None
            if m["fn"] is not None:
                try:
                    value = m["fn"](a)
                except (ZeroDivisionError, TypeError):
                    value = None
            n = a[m["n"]] if m["n"] else None
            out.append({"id": m["id"], "label": m["label"], "unit": m["unit"], "status": m["status"],
                        "formula": m["formula"], "reason": m["reason"], "higher_is_better": m["higher_is_better"],
                        "value": None if value is None else round(float(value), 3), "sample": n,
                        "se": standard_error(m, value, n, a)})
        return out

    def archetypes(self, a, pos):
        """Multi-label functional roles from box/PBP rates (DERIVED heuristic thresholds)."""
        if not a["gp"] or a["min"] < 100:
            return []
        usg = 100 * (a["fga"] + .44 * a["fta"] + a["tov"]) / max(a["on_tm_fga"] + .44 * a["on_tm_fta"] + a["on_tm_tov"], 1)
        ast100 = 100 * a["ast"] / max(a["on_po"], 1)
        p3 = 100 * a["fg3a"] / max(a["fga"], 1)
        self_created = 100 * (a["s_fgm"] - a["asd_m"]) / max(a["s_fgm"], 1)
        cs = 100 * a["cs_a"] / max(a["s_n"], 1)
        rim = 100 * a["z_rim_a"] / max(a["s_n"], 1)
        blk100 = 100 * a["blk"] / max(a["on_pd"], 1)
        stl100 = 100 * a["stl"] / max(a["on_pd"], 1)
        trb = 100 * (a["oreb"] + a["dreb"]) / max(a["reb_chances"], 1)
        drapm = a.get("drapm") or 0
        dunk = 100 * a["dunk_a"] / max(a["s_n"], 1)
        big = pos in ("C", "PF") or trb >= 14
        roles = []
        if ast100 >= 9 and usg >= 24:
            roles.append("Primary creator")
        elif ast100 >= 6:
            roles.append("Secondary creator")
        if usg >= 24 and self_created >= 55 and pos in ("PG", "SG", "G"):
            roles.append("Scoring guard")
        if p3 >= 45 and 100 * a["pull3_a"] / max(a["fg3a"], 1) < 25 and not big:
            roles.append("Catch-and-shoot specialist")
        if p3 >= 40 and a["fg3a"] >= 100 and cs >= 30 and usg >= 18 and not big:
            roles.append("Movement shooter")
        if rim >= 35 and not big and usg >= 16:
            roles.append("Slashing wing")
        if p3 >= 38 and (stl100 >= 1.6 or drapm >= 1.0) and not big:
            roles.append("3-and-D wing")
        if drapm >= 2.0 or (stl100 >= 2.2 and not big):
            roles.append("Defensive stopper")
        if big and p3 >= 30:
            roles.append("Stretch big")
        if big and rim >= 55 and dunk >= 15 and usg < 22:
            roles.append("Roll man")
        if big and usg >= 24 and rim < 55:
            roles.append("Post-up creator")
        if blk100 >= 2.5:
            roles.append("Rim protector")
        if trb >= 17:
            roles.append("Rebounding specialist")
        if big and ast100 >= 7:
            roles.append("Point center")
        return [r for r in ARCHETYPES if r in roles]

    def player_board(self, cutoff, season=None):
        """League-wide evaluated metrics for percentile ranking."""
        cutoff = M.iso(cutoff)
        season = season or self.season_for(cutoff)

        def build():
            aggs = self.player_aggs(cutoff, season)
            info = {p["id"]: p for p in self.wh.query("SELECT * FROM players")}
            team_games = defaultdict(int)
            for g in self._games(cutoff, season):
                team_games[g["home_id"]] += 1
                team_games[g["away_id"]] += 1
            median_games = statistics.median(team_games.values()) if team_games else 0
            qualify = max(60.0, 6.0 * median_games)
            defs = [m for cat in PLAYER.values() for m in cat]
            board = {}
            for pid, a in aggs.items():
                pos = (info.get(pid) or {}).get("position", "UNKNOWN")
                spec = SPECIALIZATION.get(pos)
                metrics = self.evaluate(defs, a)
                if spec:
                    metrics += [{**x, "specialization": spec} for x in self.evaluate(POSITION[spec], a)]
                board[pid] = {"metrics": metrics, "position": pos, "group": POS_GROUP.get(pos, "F"),
                              "archetypes": self.archetypes(a, pos), "qualified": a["min"] >= qualify, "agg": a}
            return {"board": board, "qualify_minutes": qualify, "season": season, "cutoff": cutoff}
        return self._cached(("pboard", cutoff, season), build)

    def player_profile(self, player_id, cutoff, season=None):
        cutoff = M.iso(cutoff)
        pb = self.player_board(cutoff, season)
        season = pb["season"]
        entry = pb["board"].get(player_id)
        info = self.wh.query("SELECT * FROM players WHERE id = ?", (player_id,))
        if not info:
            raise KeyError("Unknown player")
        info = info[0]
        if entry is None:
            return {"player": info, "season": season, "cutoff": cutoff, "metrics": [], "note": "No games in window"}
        board = pb["board"]
        qualified = [e for e in board.values() if e["qualified"]]
        a = entry["agg"]
        # Trend: last 10 games played; baseline: previous season.
        recent = self.player_aggs(cutoff, season, player_id, a.get("recent_games")).get(player_id) if a.get("recent_games") else None
        prev_season = self.wh.scalar("SELECT MAX(season) FROM games WHERE season < ?", (season,))
        base = self.player_aggs(cutoff, prev_season, player_id).get(player_id) if prev_season else None
        recent_m = {m["id"]: m["value"] for m in self.evaluate(_defs_for(entry), recent)} if recent else {}
        base_m = {m["id"]: m["value"] for m in self.evaluate(_defs_for(entry), base)} if base else {}
        team_model = self.models(cutoff)["team"]
        opp_factor = self._opponent_factor(player_id, cutoff, season, team_model)
        out = []
        for m in entry["metrics"]:
            row = dict(m)
            if m["value"] is not None:
                row["league_pct"] = percentile(m, [e for e in qualified], m["value"])
                row["position_pct"] = percentile(m, [e for e in qualified if e["group"] == entry["group"]], m["value"])
                role = entry["archetypes"][0] if entry["archetypes"] else None
                row["role_pct"] = percentile(m, [e for e in qualified if role in e["archetypes"]], m["value"]) if role else None
                if any(d["id"] == m["id"] and d["opp_adjustable"] for d in _all_player_defs()) and opp_factor:
                    row["opponent_adjusted"] = round(m["value"] * opp_factor, 3)
            row["recent"] = recent_m.get(m["id"])
            row["baseline"] = base_m.get(m["id"])
            row["trend"] = None if row["recent"] is None or m["value"] is None else round(row["recent"] - m["value"], 3)
            out.append(row)
        games = self.wh.query("""SELECT pg.*, g.home_id, g.away_id, g.home_score, g.away_score FROM player_games pg
            JOIN games g ON g.id = pg.game_id WHERE pg.player_id = ? AND pg.tipoff < ? AND g.season = ?
            ORDER BY pg.tipoff DESC LIMIT 25""", (player_id, cutoff, season))
        return {"player": info, "season": season, "cutoff": cutoff, "position": entry["position"],
                "specialization": SPECIALIZATION.get(entry["position"]), "archetypes": entry["archetypes"],
                "qualified": entry["qualified"], "qualify_minutes": pb["qualify_minutes"], "games_played": a["gp"],
                "minutes": round(a["min"], 1), "team_id": a.get("team_id"), "opponent_factor": opp_factor,
                "metrics": out, "game_log": games, "baseline_season": prev_season,
                "percentile_note": "Percentiles are direction-adjusted (100 = best) among players with at least "
                                   f"{pb['qualify_minutes']:.0f} minutes in the window."}

    def _opponent_factor(self, player_id, cutoff, season, team_model):
        """League ORtg / mean adjusted DRtg of opponents faced (minutes-weighted)."""
        if not team_model:
            return None
        rows = self.wh.query("""SELECT opponent_id, minutes FROM player_games pg JOIN games g ON g.id = pg.game_id
            WHERE player_id = ? AND pg.tipoff < ? AND g.season = ? AND minutes > 0""", (player_id, cutoff, season))
        w = sum(r["minutes"] for r in rows)
        if not w:
            return None
        opp = sum(team_model["teams"].get(r["opponent_id"], {}).get("drtg_adj", team_model["league_ortg"]) * r["minutes"] for r in rows) / w
        return round(team_model["league_ortg"] / opp, 4) if opp else None

    def shot_chart(self, cutoff, season=None, shooter_id=None, team_id=None, opponent_id=None, limit=3000):
        cutoff = M.iso(cutoff)
        season = season or self.season_for(cutoff)
        where, params = ["s.tipoff < ?", "g.season = ?", "s.team_attempt_heave = 0", "s.x IS NOT NULL"], [cutoff, season]
        for col, val in (("s.shooter_id", shooter_id), ("s.team_id", team_id), ("s.opponent_id", opponent_id)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        shots = self.wh.query(f"""SELECT s.* FROM shots s JOIN games g ON g.id = s.game_id WHERE {' AND '.join(where)}
            ORDER BY s.tipoff DESC""", params)
        mdl = self.models(cutoff)["shot"]
        xs = M.predict_shots(mdl, shots) if mdl and shots else np.zeros(len(shots))
        zones = defaultdict(lambda: {"fga": 0, "fgm": 0, "x": 0.0, "pts": 0, "xpts": 0.0})
        for s, x in zip(shots, xs):
            z = zones[s["zone"]]
            z["fga"] += 1
            z["fgm"] += s["made"]
            z["x"] += float(x)
            z["pts"] += s["points"]
            z["xpts"] += float(x) * s["points_attempted"]
        league = self._league_zones(cutoff, season)
        total = sum(z["fga"] for z in zones.values())
        zone_rows = [{"zone": k, "fga": v["fga"], "share": round(100 * v["fga"] / total, 1) if total else None,
                      "fg_pct": round(100 * v["fgm"] / v["fga"], 1), "xfg_pct": round(100 * v["x"] / v["fga"], 1),
                      "smoe": round(100 * (v["fgm"] - v["x"]) / v["fga"], 1), "pps": round(v["pts"] / v["fga"], 3),
                      "league_fg_pct": league.get(k)} for k, v in zones.items() if v["fga"]]
        return {"shots": [{"x": s["x"], "y": s["y"], "made": s["made"], "zone": s["zone"], "family": s["family"],
                           "distance": s["distance_ft"], "xfg": round(float(x), 3), "value": s["points_attempted"]}
                          for s, x in list(zip(shots, xs))[:limit]],
                "zones": sorted(zone_rows, key=lambda r: -r["fga"]), "total": total, "season": season, "cutoff": cutoff,
                "model": {"family": mdl["family"], "trained_through": mdl["trained_through"], "evaluation": mdl["evaluation"]} if mdl else None}

    def _league_zones(self, cutoff, season):
        def build():
            rows = self.wh.query("""SELECT zone, AVG(made) fg FROM shots s JOIN games g ON g.id = s.game_id
                WHERE s.tipoff < ? AND g.season = ? AND team_attempt_heave = 0 GROUP BY zone""", (cutoff, season))
            return {r["zone"]: round(100 * r["fg"], 1) for r in rows}
        return self._cached(("lzones", cutoff, season), build)

    # ------------------------------------------------------------ teams
    def team_aggs(self, cutoff, season=None, venue=None, last_n=None, opponent_id=None):
        cutoff = M.iso(cutoff)
        season = season or self.season_for(cutoff)
        key = ("tagg", cutoff, season, venue, last_n, opponent_id)
        return self._cached(key, lambda: self._team_aggs(cutoff, season, venue, last_n, opponent_id))

    def _team_window(self, cutoff, season, venue, last_n, opponent_id):
        games = self._games(cutoff, season)
        member = defaultdict(set)
        for tid in {g["home_id"] for g in games} | {g["away_id"] for g in games}:
            own = [g for g in games if tid in (g["home_id"], g["away_id"])]
            if venue == "home":
                own = [g for g in own if g["home_id"] == tid]
            elif venue == "away":
                own = [g for g in own if g["away_id"] == tid]
            if opponent_id:
                own = [g for g in own if opponent_id in (g["home_id"], g["away_id"])]
            if last_n:
                own = own[-int(last_n):]
            member[tid] = {g["id"] for g in own}
        return games, member

    def _team_aggs(self, cutoff, season, venue, last_n, opponent_id):
        mdl = self.models(cutoff)
        games, member = self._team_window(cutoff, season, venue, last_n, opponent_id)
        gmap = {g["id"]: g for g in games}
        abbr = {t["id"]: t["abbreviation"] for t in self.wh.query("SELECT id, abbreviation FROM teams")}
        A = defaultdict(Agg)
        per_game = defaultdict(list)
        box = defaultdict(dict)
        for r in self.wh.query("""SELECT pg.game_id, pg.team_id, SUM(fgm) fgm, SUM(fga) fga, SUM(fg3m) fg3m, SUM(fg3a) fg3a,
                SUM(ftm) ftm, SUM(fta) fta, SUM(oreb) oreb, SUM(dreb) dreb, SUM(ast) ast, SUM(tov) tov, SUM(stl) stl,
                SUM(blk) blk, SUM(pf) pf, SUM(minutes) min, SUM(CASE WHEN starter=1 THEN minutes ELSE 0 END) starter_min,
                SUM(CASE WHEN minutes >= 10 THEN 1 ELSE 0 END) rot, SUM(CASE WHEN minutes > 0 THEN 1 ELSE 0 END) used
                FROM player_games pg JOIN games g ON g.id = pg.game_id WHERE pg.tipoff < ? AND g.season = ?
                GROUP BY pg.game_id, pg.team_id""", (cutoff, season)):
            box[r["game_id"]][r["team_id"]] = r
        team_ratings = (mdl["team"] or {}).get("teams", {})
        ranked = sorted(team_ratings, key=lambda t: -team_ratings[t]["net_adj"])
        top10, bot10 = set(ranked[:10]), set(ranked[-10:])
        for g in games:
            for tid, oid, home in ((g["home_id"], g["away_id"], True), (g["away_id"], g["home_id"], False)):
                if g["id"] not in member[tid] or tid not in box[g["id"]] or oid not in box[g["id"]]:
                    continue
                a = A[tid]
                own, opp = box[g["id"]][tid], box[g["id"]][oid]
                pts, opts = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
                poss, oposs = (g["home_poss"], g["away_poss"]) if home else (g["away_poss"], g["home_poss"])
                poss, oposs = poss or 0, oposs or 0
                minutes = 48 + 5 * max(0, g["periods"] - 4)
                a["games"] += 1
                a["wins"] += pts > opts
                a["pts"] += pts
                a["opp_pts"] += opts
                a["poss"] += poss
                a["opp_poss"] += oposs
                a["game_minutes"] += minutes
                for k in ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "tov", "stl", "blk", "pf"):
                    a[k] += own[k]
                    a[f"opp_{k}"] += opp[k]
                a["team_min"] += own["min"]
                a["starter_min"] += own["starter_min"]
                a["rot_sum"] += own["rot"]
                if abs(pts - opts) <= 5:
                    a["close_g"] += 1
                    a["close_w"] += pts > opts
                pre = "h" if home else "a"
                a[f"{pre}_pts"] += pts
                a[f"{pre}_opp_pts"] += opts
                a[f"{pre}_poss"] += poss
                a[f"{pre}_opp_poss"] += oposs
                if oid in top10:
                    a["vs_top_g"] += 1
                    a["vs_top_diff"] += pts - opts
                if oid in bot10:
                    a["vs_bot_g"] += 1
                    a["vs_bot_diff"] += pts - opts
                orb_g = own["oreb"] / max(own["oreb"] + opp["dreb"], 1)
                per_game[tid].append({"id": g["id"], "tip": g["tipoff"], "pts": pts, "opp": opts, "poss": poss,
                                      "oposs": oposs, "min": minutes, "tov": own["tov"], "orb": 100 * orb_g,
                                      "win": pts > opts})
        # Schedule features over all prior games (rest needs history before the window).
        sched = self.wh.query("SELECT id, tipoff, home_id, away_id, home_score, away_score FROM games WHERE tipoff < ? ORDER BY tipoff", (cutoff,))
        last = {}
        recent = defaultdict(list)
        for g in sched:
            for tid, oid, home in ((g["home_id"], g["away_id"], True), (g["away_id"], g["home_id"], False)):
                t = _dt(g["tipoff"])
                diff = (g["home_score"] - g["away_score"]) * (1 if home else -1)
                loc = abbr.get(g["home_id"])
                prev = last.get(tid)
                recent[tid] = [x for x in recent[tid] if (t - x).days < 4] + [t]
                if g["id"] in member[tid]:
                    a = A[tid]
                    if prev:
                        rest = (t.date() - prev[0].date()).days
                        a["rest_days_sum"] += min(rest, 7)
                        a["rest_days_n"] += 1
                        a["travel_miles"] += miles(prev[1], loc)
                        tagger = None
                        if rest == 1:
                            tagger = "b2b"
                            a["b2b_g"] += 1
                        elif rest >= 2:
                            tagger = "rest"
                        gm = next((x for x in per_game[tid] if x["id"] == g["id"]), None)
                        if gm and tagger:
                            a[f"{tagger}_pts"] += gm["pts"]
                            a[f"{tagger}_opp_pts"] += gm["opp"]
                            a[f"{tagger}_poss"] += gm["poss"]
                            a[f"{tagger}_opp_poss"] += gm["oposs"]
                        if abs(tz(loc) - tz(prev[1])) >= 2:
                            a["tz_g"] += 1
                            a["tz_diff"] += diff
                    if len(recent[tid]) >= 3:
                        a["t34_g"] += 1
                        a["t34_diff"] += diff
                    if elevation(loc) >= 4000:
                        a["alt_g"] += 1
                        a["alt_diff"] += diff
                last[tid] = (t, loc)
        # Possessions.
        for p in self.wh.query("""SELECT p.* FROM possessions p JOIN games g ON g.id = p.game_id
                WHERE p.tipoff < ? AND g.season = ?""", (cutoff, season)):
            gid = p["game_id"]
            off, de = p["offense_id"], p["defense_id"]
            length = 720 if p["period"] <= 4 else 300
            period_end = p["period"] * 720 if p["period"] <= 4 else 2880 + (p["period"] - 4) * 300
            clutch = p["period"] >= 4 and period_end - p["start"] <= 300 and abs(p["start_margin"] or 0) <= 5
            q = p["period"] if p["period"] <= 4 else None
            for tid, pre in ((off, ""), (de, "opp_")):
                if gid not in member[tid]:
                    continue
                a = A[tid]
                if p["transition"]:
                    a[f"{pre}trans_poss"] += 1
                    a[f"{pre}trans_pts" if pre else "trans_pts_poss"] += p["points"]
                a[f"{pre}sc_pts_poss"] += p["second_chance_points"]
                if q:
                    a[f"q{q}_{'opp_' if pre else ''}pts"] += p["points"]
                    a[f"q{q}_{'opp_' if pre else ''}poss"] += 1
                if clutch:
                    a["cl_opp_pts" if pre else "cl_pts"] += p["points"]
                    a["cl_opp_poss" if pre else "cl_poss"] += 1
                    if not pre:
                        a["cl_tov"] += p["tov"]
                if pre:
                    if p["start_type"] in ("after_steal", "after_dead_ball_tov"):
                        a["opp_pts_after_tov"] += p["points"]
                    if p["oreb"] == 0:
                        a["opp_poss_no_oreb"] += 1
                else:
                    if p["transition"]:
                        a["trans_tov"] += p["tov"]
                        a["trans_fta"] += p["fta"]
                    if p["first_action"] is not None and p["first_action"] <= 12 and p["start_type"] != "period_start":
                        a["early_poss"] += 1
                        a["early_pts"] += p["points"]
                    if p["start_type"] == "after_steal":
                        a["st_steal"] += 1
                        a["st_steal_pts"] += p["points"]
                    if p["start_type"] == "after_dreb":
                        a["st_dreb"] += 1
                        a["st_dreb_pts"] += p["points"]
                    if p["end_reason"] == "steal":
                        a["end_steal"] += 1
                    elif p["end_reason"] == "tov":
                        a["end_tov"] += 1
                    m = p["start_margin"] or 0
                    if m > 0:
                        a["lead_poss"] += 1
                        a["lead_pts"] += p["points"]
                    elif m < 0:
                        a["trail_poss"] += 1
                        a["trail_pts"] += p["points"]
        # Shots for and against.
        shots = [s for s in self.wh.query("""SELECT s.* FROM shots s JOIN games g ON g.id = s.game_id
                WHERE s.tipoff < ? AND g.season = ?""", (cutoff, season))]
        xs = M.predict_shots(mdl["shot"], shots) if mdl["shot"] and shots else np.zeros(len(shots))
        recent_sq = defaultdict(lambda: [0.0, 0])
        recent_ids = {tid: {x["id"] for x in pg[-10:]} for tid, pg in per_game.items()}
        for s, x in zip(shots, xs):
            if s["team_attempt_heave"]:
                continue
            zone = {"rim": "rim", "short_midrange": "sm", "long_midrange": "lm", "corner_three": "c3",
                    "above_break_three": "ab3"}.get(s["zone"])
            fam, text, made, value = s["family"], (s["type"] or "").lower(), s["made"], s["points_attempted"]
            clutch = s["period"] >= 4 and s["clock"] <= 300 and abs(s["score_margin"] or 0) <= 5
            for tid, pre in ((s["team_id"], ""), (s["opponent_id"], "opp_")):
                if s["game_id"] not in member[tid]:
                    continue
                a = A[tid]
                a[f"{pre}s_n"] += 1
                a[f"{pre}s_fgm"] += made
                a[f"{pre}s_xfg"] += float(x)
                a[f"{pre}s_xpts"] += float(x) * value
                if value == 3:
                    a[f"{pre}s_fg3a"] += 1
                    a[f"{pre}s_fg3m"] += made
                    a[f"{pre}s_x3"] += float(x)
                if zone:
                    a[f"{pre}z_{zone}_a"] += 1
                    a[f"{pre}z_{zone}_m"] += made
                    if zone == "rim":
                        a[f"{pre}z_rim_x"] += float(x)
                if zone in ("rim", "sm") and made:
                    a[f"{pre}paint_pts"] += s["points"]
                if fam == "tip" or "putback" in text:
                    a[f"{pre}putback_a"] += 1
                    a[f"{pre}putback_pts"] += s["points"]
                if fam in ("pullup", "stepback"):
                    a[f"{pre}pull_a"] += 1
                    a[f"{pre}pull_m"] += made
                    a[f"{pre}pull3_m"] += made and value == 3
                if fam == "jumper" and "turnaround" not in text and "fade" not in text:
                    a[f"{pre}cs_a"] += 1
                    a[f"{pre}cs_m"] += made
                    a[f"{pre}cs3_m"] += made and value == 3
                if pre:
                    continue
                a["s_dist"] += s["distance_ft"] or 0
                if made and s["assist_id"]:
                    a["asd_m"] += 1
                    a["asd_3m"] += value == 3
                    a["asd_rim"] += zone == "rim"
                if s["transition"]:
                    a["trans_a"] += 1
                    a["trans_m"] += made
                    a["trans_3m"] += made and value == 3
                    a["trans_3a"] += value == 3
                    a["trans_rim_a"] += zone == "rim"
                if "cutting" in text:
                    a["cut_a"] += 1
                    a["cut_pts"] += s["points"]
                if (s["seconds_into_possession"] or 0) >= 18:
                    a["late_a"] += 1
                    a["late_pts"] += s["points"]
                if s["second_chance"]:
                    a["sc_3a"] += value == 3
                if clutch:
                    a["cl_shots"] += 1
                    a["cl_xpts"] += float(x) * value
                if s["game_id"] in recent_ids.get(tid, ()):
                    recent_sq[tid][0] += float(x) * value
                    recent_sq[tid][1] += 1
        # Event-level: rebound after miss, and-ones, fouls drawn, timeouts, subs, turnover types, clutch FT.
        ev = self.wh.query("""SELECT e.game_id, e.seq, e.period, e.clock, e.elapsed, e.type, e.text, e.team_id, e.p1,
                e.scoring, e.shooting, e.points_attempted, e.x, e.y FROM play_events e JOIN games g ON g.id = e.game_id
                WHERE g.tipoff < ? AND g.season = ? ORDER BY e.game_id, e.seq""", (cutoff, season))
        pending, prev_fg = None, None
        first_sub = {}
        current = None
        for e in ev:
            gid = e["game_id"]
            if gid != current:
                current, pending, prev_fg = gid, None, None
            if gid not in gmap:
                continue
            g = gmap[gid]
            t = e["type"].lower()
            team = e["team_id"]
            other = g["away_id"] if team == g["home_id"] else g["home_id"] if team == g["away_id"] else None
            in_t = team and gid in member.get(team, ())
            if t == "substitution":
                if in_t:
                    A[team]["subs"] += 1
                    first_sub.setdefault((gid, team), e["elapsed"])
                continue
            if "timeout" in t and in_t:
                A[team]["timeouts"] += 1
            if "turnover" in t and in_t:
                if "bad pass" in t:
                    A[team]["tov_bad"] += 1
                elif "lost ball" in t:
                    A[team]["tov_lost"] += 1
            if t == "shooting foul" and other and gid in member.get(other, ()):
                A[other]["sf_drawn"] += 1
            if t.startswith("free throw"):
                if prev_fg and prev_fg[0] == team and abs(prev_fg[1] - e["elapsed"]) < .05 and "1 of 1" in t and in_t:
                    A[team]["and1"] += 1
                if in_t and e["period"] >= 4 and e["clock"] <= 300:
                    A[team]["cl_fta"] += 1
                    A[team]["cl_ftm"] += bool(e["scoring"])
                prev_fg = None
                continue
            if e["shooting"]:
                if e["scoring"]:
                    prev_fg = (team, e["elapsed"])
                    pending = None
                else:
                    dist = math.hypot((e["x"] or 25) - 25, e["y"] or 0) if e["x"] is not None else None
                    kind = "3" if e["points_attempted"] == 3 else ("rim" if dist is not None and dist <= 4 else "2")
                    pending = (team, kind)
                    prev_fg = None
                continue
            if pending and t in ("offensive rebound", "defensive rebound"):
                shooter_team, kind = pending
                defense = g["away_id"] if shooter_team == g["home_id"] else g["home_id"]
                if gid in member.get(shooter_team, ()):
                    a = A[shooter_team]
                    a["miss_3" if kind == "3" else "miss_rim" if kind == "rim" else "miss_2x"] += 1
                    if t == "offensive rebound":
                        a["orb_after_3" if kind == "3" else "orb_after_rim" if kind == "rim" else "orb_after_2x"] += 1
                if gid in member.get(defense, ()):
                    a = A[defense]
                    a["opp_miss_3" if kind == "3" else "opp_miss_2"] += 1
                    if t == "defensive rebound":
                        a["drb_after_opp3" if kind == "3" else "drb_after_opp2"] += 1
                pending = None
        for (gid, tid), at in first_sub.items():
            A[tid]["first_sub_sum"] += at / 60
            A[tid]["first_sub_n"] += 1
        # Player-derived team metrics.
        pboard = self.player_board(cutoff, season)["board"]
        info = {p["id"]: p for p in self.wh.query("SELECT id, position FROM players")}
        lineups_cache = {}
        rapm = (mdl["rapm"] or {}).get("players", {})
        for tid, a in A.items():
            pg = per_game[tid]
            a["fg2a"] = a["fga"] - a["fg3a"]
            a["z_mid_a"] = a["z_sm_a"] + a["z_lm_a"]
            a["reb_o"] = a["oreb"] + a["opp_dreb"]
            a["reb_d"] = a["dreb"] + a["opp_oreb"]
            a["rot_len"] = round(a["rot_sum"] / a["games"], 2) if a["games"] else None
            a["first_sub_min"] = round(a["first_sub_sum"] / a["first_sub_n"], 2) if a["first_sub_n"] else None
            if tid in team_ratings:
                tr = team_ratings[tid]
                a.update(net_adj=tr["net_adj"], ortg_adj=tr["ortg_adj"], drtg_adj=tr["drtg_adj"])
                opps = [x for g in games if g["id"] in member[tid] for x in (g["home_id"], g["away_id"]) if x != tid]
                a["sos"] = round(sum(team_ratings[o]["net_adj"] for o in opps if o in team_ratings) / max(len(opps), 1), 2)
            for n in (5, 10, 20):
                sub = pg[-n:]
                po, opo = sum(x["poss"] for x in sub), sum(x["oposs"] for x in sub)
                if po and opo:
                    a[f"l{n}_net"] = round(100 * (sum(x["pts"] for x in sub) / po - sum(x["opp"] for x in sub) / opo), 2)
                    if n == 10:
                        a["l10_ortg"] = round(100 * sum(x["pts"] for x in sub) / po, 2)
                        a["l10_drtg"] = round(100 * sum(x["opp"] for x in sub) / opo, 2)
                        a["l10_pace"] = round(48 * (po + opo) / (2 * sum(x["min"] for x in sub)), 2)
                        a["l10_w"] = sum(x["win"] for x in sub)
            if len(pg) > 1:
                a["tov_sd"] = _sd([100 * x["tov"] / max(x["poss"], 1) for x in pg])
                a["orb_sd"] = _sd([x["orb"] for x in pg])
            if recent_sq[tid][1] and a["s_n"]:
                a["sq_trend"] = round(recent_sq[tid][0] / recent_sq[tid][1] - a["s_xpts"] / a["s_n"], 4)
            if a.get("net_adj") is not None and a["games"]:
                b2b_share = a["b2b_g"] / a["games"]
                a["fatigue_net"] = round(a["net_adj"] - 1.5 * b2b_share, 2)  # league B2B penalty ~1.5/100 (assumption)
            # Player dependency and rotation features.
            members = [(pid, e) for pid, e in pboard.items() if e["agg"].get("team_id") == tid and e["agg"]["min"] > 0]
            members.sort(key=lambda x: -x[1]["agg"]["min"])
            tot_min = sum(e["agg"]["min"] for _, e in members) or 1
            a["players_used"] = len(members)
            a["dep_top3"] = round(100 * sum(e["agg"]["min"] for _, e in members[:3]) / tot_min, 1)
            usage = []
            for pid, e in members:
                ag = e["agg"]
                den = ag["on_tm_fga"] + .44 * ag["on_tm_fta"] + ag["on_tm_tov"]
                if den and ag["min"] >= 100:
                    usage.append((pid, (ag["fga"] + .44 * ag["fta"] + ag["tov"]) / den, ag["min"]))
            if usage:
                shares = [u[1] * u[2] for u in usage]
                tot = sum(shares)
                a["usage_hhi"] = round(sum((s / tot) ** 2 for s in shares), 4)
                top = max(usage, key=lambda u: u[1])
                a["dep_top_usg"] = round(100 * top[1], 1)
                others = [u for u in usage if u[0] != top[0]]
                others = sorted(others, key=lambda u: -u[1])[:4]
                if others:
                    # Proportional model: remaining players absorb the top player's share in
                    # proportion to their own usage, u_i' = u_i / (1 - u_top) (MODELED assumption).
                    a["dep_usage"] = round(sum(100 * (u[1] / (1 - top[1]) - u[1]) for u in others) / len(others), 2)
            if a["ast"] and members:
                a["dep_ast"] = round(100 * max(e["agg"]["ast"] for _, e in members) / a["ast"], 1)

            def onoff(pid, kind):
                ag = pboard[pid]["agg"]
                if not (ag["on_po"] and ag["on_pd"] and ag["off_po"] > 100 and ag["off_pd"] > 100):
                    return None
                on_o, on_d = 100 * ag["on_pf"] / ag["on_po"], 100 * ag["on_pa"] / ag["on_pd"]
                off_o, off_d = 100 * ag["off_pf"] / ag["off_po"], 100 * ag["off_pa"] / ag["off_pd"]
                return {"net": (on_o - on_d) - (off_o - off_d), "off": on_o - off_o, "def": off_d - on_d}[kind]
            rot = [pid for pid, e in members if e["agg"]["min"] >= 200]
            if rot:
                star = max(rot, key=lambda p: rapm.get(p, {}).get("rapm", -99))
                a["dep_star"] = _r(onoff(star, "net"))
                creator = max(rot, key=lambda p: pboard[p]["agg"]["ast"] / max(pboard[p]["agg"]["on_po"], 1))
                a["dep_creator"] = _r(onoff(creator, "off"))
                rimp = max(rot, key=lambda p: pboard[p]["agg"]["blk"] / max(pboard[p]["agg"]["on_pd"], 1))
                a["dep_rim"] = _r(onoff(rimp, "def"))
                stopper = max(rot, key=lambda p: rapm.get(p, {}).get("drapm", -99))
                a["dep_stopper"] = _r(onoff(stopper, "def"))
                a["rot_rapm"] = round(sum(rapm.get(p, {}).get("rapm", 0) * pboard[p]["agg"]["min"] for p in rot) /
                                      sum(pboard[p]["agg"]["min"] for p in rot) * 5, 2)
                a["dep_repl"] = round(sum(rapm.get(p, {}).get("rapm", 0) for p, _ in members[5:10]), 2)
                a["dep_inj"] = a["rot_rapm"]
                first_ids = {x["id"] for x in pg[:10]}
                early_players = {r["player_id"] for r in self.wh.query(
                    "SELECT player_id FROM player_games WHERE team_id=? AND minutes>0 AND game_id IN (%s)" % ",".join("?" * len(first_ids)),
                    (tid, *first_ids))} if first_ids else set()
                a["dep_cont"] = round(100 * sum(e["agg"]["min"] for p, e in members if p in early_players) / tot_min, 1)
                last7 = [x for x in pg if pg and (_dt(pg[-1]["tip"]) - _dt(x["tip"])).days < 7]
                if last7:
                    ids = {x["id"] for x in last7}
                    rows = self.wh.query("SELECT player_id, minutes FROM player_games WHERE team_id=? AND game_id IN (%s)" % ",".join("?" * len(ids)), (tid, *ids))
                    per = defaultdict(float)
                    for r in rows:
                        per[r["player_id"]] += r["minutes"] or 0
                    a["workload"] = round(sum(sorted(per.values(), reverse=True)[:5]), 1)
                shooter = max(rot, key=lambda p: pboard[p]["agg"]["fg3a"])
                a["dep_shooter"] = self._onoff_3pa(tid, shooter, cutoff, season, member[tid])
            # Lineups.
            lus = M.lineup_table(self.wh, cutoff, tid, mdl["rapm"], since=self.season_start(season)) if mdl["rapm"] else []
            lineups_cache[tid] = lus
            self._lineup_features(a, lus, tid, members, info, cutoff, season, member[tid])
        league_rim = sum(a["opp_z_rim_a"] for a in A.values()) / max(sum(a["opp_s_n"] for a in A.values()), 1)
        for a in A.values():
            if a["opp_s_n"]:
                a["rim_deter"] = round(100 * (league_rim - a["opp_z_rim_a"] / a["opp_s_n"]), 2)
        return dict(A)

    def _onoff_3pa(self, tid, pid, cutoff, season, game_ids):
        rows = self.wh.query("""SELECT s.* FROM lineup_stints s JOIN games g ON g.id = s.game_id
            WHERE s.tipoff < ? AND g.season = ? AND (s.home_id = ? OR s.away_id = ?)""", (cutoff, season, tid, tid))
        on = [0, 0]
        off = [0, 0]
        for s in rows:
            if s["game_id"] not in game_ids:
                continue
            side = "home" if s["home_id"] == tid else "away"
            bucket = on if pid in s[f"{side}_lineup"].split(",") else off
            bucket[0] += s[f"{side}_fg3a"]
            bucket[1] += s[f"{side}_fga"]
        if not on[1] or not off[1]:
            return None
        return round(100 * (on[0] / on[1] - off[0] / off[1]), 2)

    def _lineup_features(self, a, lus, tid, members, info, cutoff, season, game_ids):
        if not lus:
            return
        a["lu_count"] = sum(1 for l in lus if l["seconds"] >= 60)
        total = sum(l["seconds"] for l in lus) or 1
        a["lu_top_share"] = round(100 * lus[0]["seconds"] / total, 1)
        big = [l for l in lus if l["possessions"] >= 100]
        if big:
            a["lu_best"] = max(l["adjusted_net"] for l in big)
            a["lu_worst"] = min(l["adjusted_net"] for l in big)
        top5 = lus[:5]
        poss = sum(l["possessions"] for l in top5) or 1
        a["lu_top_ortg"] = round(sum(l["ortg"] * l["possessions"] for l in top5) / poss, 1)
        a["lu_top_drtg"] = round(sum(l["drtg"] * l["possessions"] for l in top5) / poss, 1)
        a["lu_top_reb"] = round(sum((l["oreb_pct"] + l["dreb_pct"]) / 2 * l["possessions"] for l in top5) / poss, 1)
        w = sum(l["seconds"] for l in lus)
        a["lu_chem"] = round(sum(l["interaction"] * l["seconds"] for l in lus) / w, 2) if w else None
        starters = {pid for pid, e in members if e["agg"]["starts"] >= max(3, e["agg"]["gp"] * .5)}
        start_lu = next((l for l in lus if set(l["lineup"]) <= starters or len(set(l["lineup"]) & starters) == 5), None)
        a["lu_start_net"] = start_lu["net"] if start_lu else None

        def group_net(pred):
            pf = pa = po = pd = 0
            for l in lus:
                if pred(l["lineup"]):
                    po += l["possessions"]
                    pf += l["ortg"] * l["possessions"] / 100
                    pa += l["drtg"] * l["possessions"] / 100
            return round(100 * (pf - pa) / po, 2) if po >= 50 else None
        centers = lambda ids: sum(1 for p in ids if (info.get(p) or {}).get("position") == "C")
        pfs = lambda ids: sum(1 for p in ids if (info.get(p) or {}).get("position") == "PF")
        a["lu_bench_net"] = group_net(lambda ids: len(set(ids) & starters) <= 1)
        a["lu_small_net"] = group_net(lambda ids: centers(ids) == 0)
        a["lu_big_net"] = group_net(lambda ids: centers(ids) >= 2 or (centers(ids) == 1 and pfs(ids) >= 1))

    def team_board(self, cutoff, season=None, **filters):
        cutoff = M.iso(cutoff)
        season = season or self.season_for(cutoff)
        aggs = self.team_aggs(cutoff, season, **filters)
        defs = [m for cat in TEAM.values() for m in cat]
        return {tid: self.evaluate(defs, a) for tid, a in aggs.items()}, aggs, season

    def team_profile(self, team_id, cutoff, season=None, **filters):
        cutoff = M.iso(cutoff)
        board, aggs, season = self.team_board(cutoff, season, **filters)
        if team_id not in board:
            raise KeyError("Team has no games in the window")
        metrics = []
        cats = [(cat, m["id"]) for cat, ms in TEAM.items() for m in ms]
        cat_of = dict((mid, cat) for cat, mid in cats)
        for m in board[team_id]:
            row = {**m, "category": cat_of[m["id"]]}
            if m["value"] is not None:
                vals = [x for t in board.values() for x in t if x["id"] == m["id"] and x["value"] is not None]
                ordered = sorted(vals, key=lambda x: x["value"], reverse=m["higher_is_better"])
                row["rank"] = 1 + sum(1 for x in ordered if (x["value"] > m["value"] if m["higher_is_better"] else x["value"] < m["value"]))
                row["of"] = len(ordered)
                row["league_avg"] = round(sum(x["value"] for x in vals) / len(vals), 3)
                row["league_pct"] = round(100 * (len(ordered) - row["rank"]) / max(len(ordered) - 1, 1), 1)
            metrics.append(row)
        mdl = self.models(cutoff)
        lus = M.lineup_table(self.wh, cutoff, team_id, mdl["rapm"], since=self.season_start(season)) if mdl["rapm"] else []
        names = {p["id"]: p["name"] for p in self.wh.query("SELECT id, name FROM players")}
        quarters = self.wh.query("""SELECT period, SUM(CASE WHEN team_id=? THEN points ELSE 0 END) pts,
            SUM(CASE WHEN team_id!=? THEN points ELSE 0 END) opp, COUNT(DISTINCT pr.game_id) games FROM periods pr
            JOIN games g ON g.id = pr.game_id WHERE (g.home_id=? OR g.away_id=?) AND g.tipoff < ? AND g.season = ?
            GROUP BY period ORDER BY period""", (team_id, team_id, team_id, team_id, cutoff, season))
        _, member = self._team_window(cutoff, season, filters.get("venue"), filters.get("last_n"), filters.get("opponent_id"))
        log = self.wh.query("""SELECT id, tipoff, home_id, away_id, home_score, away_score, home_poss, away_poss
            FROM games WHERE id IN (%s) ORDER BY tipoff""" % ",".join("?" * len(member[team_id])), tuple(member[team_id])) if member[team_id] else []
        rolling = []
        for i, g in enumerate(log):
            home = g["home_id"] == team_id
            pts, opp = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
            po, opo = (g["home_poss"], g["away_poss"]) if home else (g["away_poss"], g["home_poss"])
            rolling.append({"game_id": g["id"], "tipoff": g["tipoff"], "opponent_id": g["away_id"] if home else g["home_id"],
                            "home": home, "pts": pts, "opp_pts": opp, "ortg": round(100 * pts / max(po or 1, 1), 1),
                            "drtg": round(100 * opp / max(opo or 1, 1), 1)})
        for i in range(len(rolling)):
            win = rolling[max(0, i - 9):i + 1]
            rolling[i]["roll10_net"] = round(sum(x["ortg"] - x["drtg"] for x in win) / len(win), 2)
        team = self.wh.query("SELECT * FROM teams WHERE id = ?", (team_id,))[0]
        tr = (mdl["team"] or {}).get("teams", {}).get(team_id)
        return {"team": team, "season": season, "cutoff": cutoff, "filters": filters, "metrics": metrics,
                "games": aggs[team_id]["games"], "rating_model": tr,
                "lineups": [{**l, "names": [names.get(p, p) for p in l["lineup"]]} for l in lus[:40]],
                "quarters": quarters, "game_log": rolling}


def _r(v, n=2):
    return None if v is None else round(v, n)


def _all_player_defs():
    return [m for cat in PLAYER.values() for m in cat] + [m for ms in POSITION.values() for m in ms]


def _defs_for(entry):
    spec = SPECIALIZATION.get(entry["position"])
    return [m for cat in PLAYER.values() for m in cat] + (POSITION[spec] if spec else [])


def project_minutes(minutes, half_life=5.0, prior_weight=3.0):
    """EWMA of recent minutes shrunk toward the window mean (MODELED)."""
    if not minutes:
        return 0.0
    w = np.array([0.5 ** ((len(minutes) - 1 - i) / half_life) for i in range(len(minutes))])
    ewma = float(np.dot(w, minutes) / w.sum())
    mean = float(np.mean(minutes))
    n_eff = float(w.sum())
    return (n_eff * ewma + prior_weight * mean) / (n_eff + prior_weight)


def standard_error(m, value, n, a):
    if value is None:
        return None
    if m["kind"] == "model":
        return a.get("rapm_sd") if m["id"] in ("p_rapm", "p_war", "p_avail_impact") else None
    if not n:
        return None
    if m["kind"] == "pct":
        p = value / 100 if 0 <= value <= 100 else 0.25
        return round(100 * math.sqrt(max(p * (1 - p), 0.01) / n), 3)
    if m["kind"] == "rate":
        return round(abs(value) / math.sqrt(n), 3) if n > 0 else None
    if m["kind"] == "mean":
        if m["unit"] == "pts/100":
            return round(170 / math.sqrt(n), 3)
        return round(math.sqrt(abs(value) / n), 3)
    return None


def percentile(m, entries, value):
    vals = [x["value"] for e in entries for x in e["metrics"] if x["id"] == m["id"] and x["value"] is not None]
    if len(vals) < 5:
        return None
    below = sum(v < value for v in vals) + 0.5 * sum(v == value for v in vals)
    pct = 100 * below / len(vals)
    return round(pct if m["higher_is_better"] else 100 - pct, 1)
