"""FastAPI routes for the intelligence terminal (/api/intel/*).

Expensive work (model fits, league boards, 50k simulations) runs in background
threads; projections are submitted as jobs and polled. No LLM is involved.
"""
import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import catalog as C
from . import models as M
from .warehouse import DEFAULT_DB, UNAVAILABLE_SOURCES, Warehouse


class ScenarioIn(BaseModel):
    player_id: str
    active_probability: float | None = Field(default=None, ge=0, le=1)
    minutes: float | None = Field(default=None, ge=0, le=48)
    minutes_cap: float | None = Field(default=None, ge=0, le=48)


class ProjectIn(BaseModel):
    home_id: str
    away_id: str
    cutoff: str | None = None
    tipoff: str | None = None
    simulations: int = Field(default=10000, ge=500, le=50000)
    seed: int = Field(default=42, ge=0, le=2 ** 31 - 1)
    scenarios: list[ScenarioIn] = Field(default_factory=list, max_length=30)


class State:
    def __init__(self, db_path):
        self.db_path = db_path
        self.wh = Warehouse(db_path) if Path(db_path).exists() else None
        self.engine = self.projector = None
        if self.wh and self.wh.scalar("SELECT COUNT(*) FROM games"):
            from .engine import Engine
            from .projection import Projector
            self.engine = Engine(self.wh)
            self.projector = Projector(self.engine)
        self.jobs = {}
        self.warm = {"status": "idle"}

    def require(self):
        if not self.engine:
            raise HTTPException(503, "Warehouse is empty. Run: python -m nba_quant.intel acquire START END && python -m nba_quant.intel build")
        return self.engine


def register(app, db_path=None):
    st = State(db_path or os.environ.get("NBA_INTEL_DB", str(DEFAULT_DB)))
    app.state.intel = st

    def warm():
        if not st.engine:
            return
        st.warm = {"status": "warming", "started": time.time()}
        try:
            c = st.engine.latest_cutoff()
            st.engine.models(c)
            st.engine.player_board(c)
            st.engine.team_aggs(c)
            st.warm = {"status": "ready", "cutoff": c, "seconds": round(time.time() - st.warm["started"], 1)}
        except Exception as error:  # surfaced through /status, never hidden
            st.warm = {"status": "error", "error": repr(error)[:300]}

    if os.environ.get("NBA_INTEL_WARM", "0") == "1":
        threading.Thread(target=warm, daemon=True).start()

    def cutoff_or_latest(cutoff):
        eng = st.require()
        return M.iso(cutoff) if cutoff else eng.latest_cutoff()

    @app.get("/terminal", include_in_schema=False)
    def terminal():
        return RedirectResponse("/terminal.html")

    @app.get("/api/intel/status")
    def status():
        base = {"llm_required": False, "catalog": C.counts(), "warm": st.warm,
                "unavailable_sources": [{"id": k, "provider": n, "note": t} for k, n, t in UNAVAILABLE_SOURCES]}
        if not st.engine:
            return {**base, "status": "EMPTY", "message": "No warehouse data. Run the acquire and build commands."}
        cov = st.wh.coverage()
        return {**base, "status": "READY", "coverage": cov, "latest_cutoff": st.engine.latest_cutoff(),
                "seasons": st.engine.seasons(), "jobs_running": sum(j["status"] == "running" for j in st.jobs.values()),
                "backtests": st.wh.scalar("SELECT COUNT(*) FROM validation_results")}

    @app.get("/api/intel/catalog")
    def catalog(scope: str | None = None):
        defs = C.all_definitions()
        return {"definitions": [d for d in defs if not scope or d["scope"] == scope], "counts": C.counts(),
                "status_classes": {"OBSERVED": "Provider box-score value", "DERIVED": "Deterministic formula over reconstructed play-by-play",
                                   "ESTIMATED": "Documented proxy for a concept the provider does not publish",
                                   "MODELED": "Output of a fitted model", "UNAVAILABLE": "No authorized data source; value is null"}}

    @app.get("/api/intel/teams")
    def teams(cutoff: str | None = None):
        eng = st.require()
        c = cutoff_or_latest(cutoff)
        tm = eng.models(c)["team"] or {}
        rows = st.wh.query("SELECT * FROM teams ORDER BY abbreviation")
        for r in rows:
            r["rating"] = (tm.get("teams") or {}).get(r["id"])
        return {"teams": rows, "cutoff": c, "model": {k: tm.get(k) for k in ("trained_through", "games", "home_court_per100", "league_ortg", "league_pace", "margin_sigma")}}

    @app.get("/api/intel/players")
    def players(q: str = "", team_id: str | None = None, limit: int = 40):
        st.require()
        sql, params = "SELECT p.*, t.abbreviation team FROM players p LEFT JOIN teams t ON t.id = p.last_team_id WHERE 1=1", []
        if q:
            sql += " AND p.name LIKE ?"
            params.append(f"%{q}%")
        if team_id:
            sql += " AND p.last_team_id = ?"
            params.append(team_id)
        sql += " ORDER BY p.last_game_at DESC LIMIT ?"
        params.append(min(limit, 500))
        return {"players": st.wh.query(sql, params)}

    @app.get("/api/intel/player/{player_id}")
    def player(player_id: str, season: int | None = None, cutoff: str | None = None):
        eng = st.require()
        try:
            prof = eng.player_profile(player_id, cutoff_or_latest(cutoff), season)
        except KeyError as error:
            raise HTTPException(404, str(error))
        team = st.wh.query("SELECT * FROM teams WHERE id = ?", (prof.get("team_id"),))
        prof["team"] = team[0] if team else None
        return prof

    @app.get("/api/intel/team/{team_id}")
    def team(team_id: str, season: int | None = None, cutoff: str | None = None, venue: str | None = None,
             last_n: int | None = None, opponent_id: str | None = None):
        eng = st.require()
        if venue not in (None, "home", "away"):
            raise HTTPException(422, "venue must be home or away")
        try:
            return eng.team_profile(team_id, cutoff_or_latest(cutoff), season, venue=venue, last_n=last_n, opponent_id=opponent_id)
        except KeyError as error:
            raise HTTPException(404, str(error))

    @app.get("/api/intel/shots")
    def shots(player_id: str | None = None, team_id: str | None = None, opponent_id: str | None = None,
              season: int | None = None, cutoff: str | None = None):
        eng = st.require()
        if not (player_id or team_id or opponent_id):
            raise HTTPException(422, "Choose a player, team or opponent")
        return eng.shot_chart(cutoff_or_latest(cutoff), season, player_id, team_id, opponent_id)

    @app.get("/api/intel/leaders")
    def leaders(metric: str, season: int | None = None, cutoff: str | None = None, limit: int = 25):
        eng = st.require()
        board = eng.player_board(cutoff_or_latest(cutoff), season)
        names = {p["id"]: p for p in st.wh.query("SELECT p.id, p.name, t.abbreviation team FROM players p LEFT JOIN teams t ON t.id = p.last_team_id")}
        rows = []
        for pid, e in board["board"].items():
            if not e["qualified"]:
                continue
            m = next((x for x in e["metrics"] if x["id"] == metric), None)
            if m and m["value"] is not None:
                rows.append({"player_id": pid, "name": names.get(pid, {}).get("name"), "team": names.get(pid, {}).get("team"),
                             "position": e["position"], "value": m["value"], "higher_is_better": m["higher_is_better"]})
        if not rows:
            raise HTTPException(404, "No qualified values for this metric")
        rows.sort(key=lambda r: -r["value"] if r["higher_is_better"] else r["value"])
        return {"metric": metric, "season": board["season"], "rows": rows[:limit], "qualify_minutes": board["qualify_minutes"]}

    @app.get("/api/intel/lineups/{team_id}")
    def lineups(team_id: str, season: int | None = None, cutoff: str | None = None, min_poss: int = 25, include: str = "", exclude: str = ""):
        eng = st.require()
        c = cutoff_or_latest(cutoff)
        season = season or eng.season_for(c)
        mdl = eng.models(c)
        rows = M.lineup_table(st.wh, c, team_id, mdl["rapm"], since=eng.season_start(season))
        inc = {x for x in include.split(",") if x}
        exc = {x for x in exclude.split(",") if x}
        rows = [r for r in rows if r["possessions"] >= min_poss and inc <= set(r["lineup"]) and not (exc & set(r["lineup"]))]
        info = {p["id"]: p for p in st.wh.query("SELECT id, name, position FROM players")}
        pboard = eng.player_board(c, season)["board"]
        rapm = (mdl["rapm"] or {}).get("players", {})
        for r in rows:
            r["players"] = [{"id": p, "name": info.get(p, {}).get("name", p), "position": info.get(p, {}).get("position"),
                             "rapm": rapm.get(p, {}).get("rapm"), "archetypes": (pboard.get(p) or {}).get("archetypes", [])} for p in r["lineup"]]
            r["role_balance"] = role_balance(r["players"])
        roster = sorted({p for r in rows for p in r["lineup"]}, key=lambda p: info.get(p, {}).get("name", p))
        return {"team_id": team_id, "season": season, "cutoff": c, "lineups": rows[:200], "roster": [
            {"id": p, "name": info.get(p, {}).get("name", p), "position": info.get(p, {}).get("position")} for p in roster],
            "method": "Observed lineup stints; adjusted net = (poss*net + 400*talent prior)/(poss + 400); talent prior = sum of player RAPM; interaction = adjusted - prior."}

    @app.get("/api/intel/matchup")
    def matchup(home_id: str, away_id: str, season: int | None = None, cutoff: str | None = None):
        eng = st.require()
        c = cutoff_or_latest(cutoff)
        return build_matchup(eng, c, season, home_id, away_id)

    @app.post("/api/intel/project", status_code=202)
    def project(req: ProjectIn):
        eng = st.require()
        if req.home_id == req.away_id:
            raise HTTPException(422, "Choose two different teams")
        now = datetime.now(timezone.utc)
        cutoff = M.iso(req.cutoff) if req.cutoff else min(M.iso(now), M.iso(req.tipoff) if req.tipoff else M.iso(now))
        job = {"id": str(uuid.uuid4()), "status": "running", "created_at": now.isoformat(), "request": req.model_dump(),
               "cutoff": cutoff}
        st.jobs[job["id"]] = job

        def work():
            t0 = time.time()
            try:
                out = st.projector.project(req.home_id, req.away_id, cutoff, sims=req.simulations, seed=req.seed,
                                           scenarios=[s.model_dump() for s in req.scenarios], tipoff=req.tipoff)
                from .backtest import apply_platt, latest_platt
                live = latest_platt(st.wh)
                out["calibrated_home_win_probability"] = apply_platt(live, out["home_win_probability"])
                out["calibration_source"] = ({"method": "Platt scaling fitted on walk-forward backtest forecasts",
                                              "games": live["games"], "trained_through": live["trained_through"]} if live else None)
                names = {t["id"]: t for t in st.wh.query("SELECT * FROM teams")}
                out["home"], out["away"] = names.get(req.home_id), names.get(req.away_id)
                job.update(status="done", result=out, seconds=round(time.time() - t0, 2))
                with st.wh.connect() as db:
                    db.execute("INSERT OR REPLACE INTO simulation_runs VALUES(?,?,?,?,?,?,?,?)", (
                        job["id"], job["created_at"], f"{req.home_id}-{req.away_id}", req.simulations, req.seed, out["key"],
                        job["seconds"], json.dumps({k: out[k] for k in ("home_win_probability", "margin", "total", "pace", "calibration", "cutoff", "scenarios")})))
            except Exception as error:
                job.update(status="error", error=str(error)[:500], trace=traceback.format_exc()[-1500:])
        threading.Thread(target=work, daemon=True).start()
        if len(st.jobs) > 50:
            for k in list(st.jobs)[:-50]:
                st.jobs.pop(k, None)
        return {"job_id": job["id"], "status": "running"}

    @app.get("/api/intel/jobs/{job_id}")
    def job(job_id: str):
        j = st.jobs.get(job_id)
        if not j:
            raise HTTPException(404, "Unknown job")
        return {k: v for k, v in j.items() if k != "trace"}

    @app.get("/api/intel/roster/{team_id}")
    def roster(team_id: str, cutoff: str | None = None):
        st.require()
        c = cutoff_or_latest(cutoff)
        ids = st.projector.roster(team_id, c)
        info = {p["id"]: p for p in st.wh.query("SELECT id, name, position FROM players")}
        rows = []
        for pid in ids:
            hist = st.projector.player_history(pid, team_id, c)
            mins = [m for m, _ in hist if m > 0]
            rows.append({"id": pid, "name": info.get(pid, {}).get("name"), "position": info.get(pid, {}).get("position"),
                         "recent_minutes": round(sum(mins[-5:]) / max(len(mins[-5:]), 1), 1), "missed_last": next((i for i, (m, _) in enumerate(reversed(hist)) if m > 0), len(hist))})
        rows.sort(key=lambda r: -r["recent_minutes"])
        return {"team_id": team_id, "cutoff": c, "players": rows}

    @app.get("/api/intel/schedule")
    def schedule(day: str | None = None):
        st.require()
        from datetime import date as d_
        from .acquire import scoreboard
        try:
            target = d_.fromisoformat(day) if day else datetime.now(timezone.utc).date()
            events = scoreboard(target)
        except Exception as error:
            raise HTTPException(502, f"ESPN schedule unavailable: {error}")
        out = []
        for e in events:
            comp = e["competitions"][0]
            teams_ = {c["homeAway"]: c for c in comp["competitors"]}
            out.append({"id": e["id"], "tipoff": e["date"], "status": comp["status"]["type"].get("description"),
                        "completed": comp["status"]["type"].get("completed"),
                        "home_id": teams_["home"]["team"]["id"], "away_id": teams_["away"]["team"]["id"],
                        "home": teams_["home"]["team"].get("abbreviation"), "away": teams_["away"]["team"].get("abbreviation"),
                        "home_score": teams_["home"].get("score"), "away_score": teams_["away"].get("score")})
        return {"date": target.isoformat(), "games": out, "source": "ESPN scoreboard (schedule only)"}

    @app.get("/api/intel/audit")
    def audit():
        eng = st.require()
        c = eng.latest_cutoff()
        mdl = eng.models(c)
        runs = st.wh.query("SELECT id, created_at, kind, model_version, payload FROM validation_results ORDER BY created_at DESC LIMIT 10")
        for r in runs:
            r["payload"] = json.loads(r["payload"])
        rapm = mdl["rapm"] or {}
        return {"backtests": runs, "cutoff": c,
                "models": [
                    {"family": "shot_quality_logistic", "trained_through": (mdl["shot"] or {}).get("trained_through"),
                     "n": (mdl["shot"] or {}).get("n_shots"), "evaluation": (mdl["shot"] or {}).get("evaluation"),
                     "feature_importance": shot_importance(mdl["shot"])},
                    {"family": "rapm_ridge", "trained_through": rapm.get("trained_through"), "n": rapm.get("stints"),
                     "lambda": rapm.get("lambda"), "home_court_per100": rapm.get("home_court_per100")},
                    {"family": "team_ridge", **{k: (mdl["team"] or {}).get(k) for k in ("trained_through", "games", "lambda", "half_life_days", "home_court_per100", "league_ortg", "league_pace", "margin_sigma")}},
                    {"family": "availability_markov_table", **st.projector.availability_model(c)},
                    {"family": "possession_simulation", "version": "possession-sim-1.0",
                     "note": "Event-level simulator; team efficiency anchored to team_ridge with RAPM availability adjustment; turnovers/fouls anchored to four-factors matchup."}],
                "promotion_policy": "No model is auto-promoted on training fit. A change ships only if walk-forward log loss/MAE improve on the same games."}

    return st


def role_balance(players):
    roles = [r for p in players for r in p["archetypes"]]
    has = lambda *names: any(r in names for r in roles)
    out = {"creators": sum(r in ("Primary creator", "Secondary creator", "Point center") for r in roles),
           "shooters": sum(r in ("Movement shooter", "Catch-and-shoot specialist", "Stretch big", "3-and-D wing") for r in roles),
           "rim_protection": has("Rim protector"), "rebounding": has("Rebounding specialist", "Rim protector"),
           "defenders": sum(r in ("Defensive stopper", "3-and-D wing", "Rim protector") for r in roles)}
    flags = []
    if out["creators"] == 0:
        flags.append("No primary/secondary creator")
    if out["shooters"] < 2:
        flags.append("Fewer than two spacing roles")
    if not out["rim_protection"]:
        flags.append("No rim protector")
    out["flags"] = flags
    out["method"] = "DERIVED heuristic from multi-label archetypes; not a fitted synergy model"
    return out


def shot_importance(model):
    if not model:
        return []
    w = model["weights"]
    rows = [{"feature": f, "coefficient": round(c, 4)} for f, c in zip(model["features"], w) if f != "intercept"]
    return sorted(rows, key=lambda r: -abs(r["coefficient"]))[:15]


def build_matchup(eng, cutoff, season, home, away):
    season = season or eng.season_for(cutoff)
    aggs = eng.team_aggs(cutoff, season)
    if home not in aggs or away not in aggs:
        raise HTTPException(404, "Both teams need games in the window")
    zones = (("rim", "Rim"), ("sm", "Short midrange"), ("lm", "Long midrange"), ("c3", "Corner three"), ("ab3", "Above-break three"))
    league = {z: sum(a[f"z_{z}_m"] for a in aggs.values()) / max(sum(a[f"z_{z}_a"] for a in aggs.values()), 1) for z, _ in zones}
    league_share = {z: sum(a[f"z_{z}_a"] for a in aggs.values()) / max(sum(a["s_n"] for a in aggs.values()), 1) for z, _ in zones}

    def side(off, de):
        o, d = aggs[off], aggs[de]
        rows = []
        for z, label in zones:
            rows.append({"zone": label, "offense_share": _pct(o[f"z_{z}_a"], o["s_n"]), "offense_fg": _pct(o[f"z_{z}_m"], o[f"z_{z}_a"]),
                         "defense_share_allowed": _pct(d[f"opp_z_{z}_a"], d["opp_s_n"]), "defense_fg_allowed": _pct(d[f"opp_z_{z}_m"], d[f"opp_z_{z}_a"]),
                         "league_fg": round(100 * league[z], 1), "league_share": round(100 * league_share[z], 1)})
        for r in rows:
            if r["offense_fg"] is not None and r["defense_fg_allowed"] is not None:
                r["edge_pp"] = round((r["offense_fg"] - r["league_fg"]) + (r["defense_fg_allowed"] - r["league_fg"]), 1)
        orb = _pct(o["oreb"], o["oreb"] + o["opp_dreb"])
        opp_drb = _pct(d["dreb"], d["dreb"] + d["opp_oreb"])
        return {"zones": rows,
                "four_factors": {"offense_efg": _pct(o["fgm"] + .5 * o["fg3m"], o["fga"]), "defense_efg_allowed": _pct(d["opp_fgm"] + .5 * d["opp_fg3m"], d["opp_fga"]),
                                 "offense_tov": _pct(o["tov"], o["poss"]), "defense_forced_tov": _pct(d["opp_tov"], d["opp_poss"]),
                                 "offense_orb": orb, "defense_drb": opp_drb,
                                 "offense_ftr": _pct(o["fta"], o["fga"]), "defense_ftr_allowed": _pct(d["opp_fta"], d["opp_fga"])},
                "shot_quality": {"offense_xpps": _r(o["s_xpts"] / o["s_n"], 3) if o["s_n"] else None,
                                 "defense_xpps_allowed": _r(d["opp_s_xpts"] / d["opp_s_n"], 3) if d["opp_s_n"] else None,
                                 "offense_smoe": _pct(o["s_fgm"] - o["s_xfg"], o["s_n"]), "defense_smoe_allowed": _pct(d["opp_s_fgm"] - d["opp_s_xfg"], d["opp_s_n"])},
                "transition": {"offense_freq": _pct(o["trans_poss"], o["poss"]), "offense_ppp": _r(o["trans_pts_poss"] / o["trans_poss"], 3) if o["trans_poss"] else None,
                               "defense_ppp_allowed": _r(d["opp_trans_pts"] / d["opp_trans_poss"], 3) if d["opp_trans_poss"] else None}}

    a, b = side(home, away), side(away, home)
    names = {t["id"]: t for t in eng.wh.query("SELECT * FROM teams")}
    pboard = eng.player_board(cutoff, season)["board"]
    info = {p["id"]: p for p in eng.wh.query("SELECT id, name, position FROM players")}

    def key_players(tid, opp):
        rows = []
        for pid, e in pboard.items():
            ag = e["agg"]
            if ag.get("team_id") != tid or ag["min"] < 150:
                continue
            usg = 100 * (ag["fga"] + .44 * ag["fta"] + ag["tov"]) / max(ag["on_tm_fga"] + .44 * ag["on_tm_fta"] + ag["on_tm_tov"], 1)
            rows.append({"id": pid, "name": info.get(pid, {}).get("name"), "position": e["position"], "archetypes": e["archetypes"],
                         "usage": round(usg, 1), "min_g": round(ag["min"] / max(ag["gp"], 1), 1),
                         "rim_rate": _pct(ag["z_rim_a"], ag["s_n"]), "three_rate": _pct(ag["fg3a"], ag["fga"]),
                         "zone_vs_defense": [{"zone": z, "player_fg": _pct(ag[f"z_{k}_m"], ag[f"z_{k}_a"]), "attempts": int(ag[f"z_{k}_a"]),
                                              "opp_allowed_fg": _pct(aggs[opp][f"opp_z_{k}_m"], aggs[opp][f"opp_z_{k}_a"])}
                                             for k, z in (("rim", "Rim"), ("c3", "Corner 3"), ("ab3", "Above-break 3"))]})
        rows.sort(key=lambda r: -r["usage"] * r["min_g"])
        return rows[:6]

    notes = tactical_notes(names, home, away, a, b)
    return {"home": names.get(home), "away": names.get(away), "season": season, "cutoff": cutoff,
            "home_offense_vs_away_defense": a, "away_offense_vs_home_defense": b,
            "key_players": {"home": key_players(home, away), "away": key_players(away, home)},
            "tactical_notes": notes,
            "unavailable": ["Guard vs primary defender (matchup tracking)", "Pick-and-roll, drop and switch coverage outcomes (Synergy/coverage tagging)"],
            "method": "Team-level zone and four-factor comparison from reconstructed play-by-play; edges are additive pp versus league average."}


def tactical_notes(names, home, away, a, b):
    notes = []
    for side, off, de in ((a, home, away), (b, away, home)):
        on, dn = names[off]["abbreviation"], names[de]["abbreviation"]
        zs = [z for z in side["zones"] if z.get("edge_pp") is not None]
        if zs:
            best = max(zs, key=lambda z: z["edge_pp"] * (z["offense_share"] or 0))
            worst = min(zs, key=lambda z: z["edge_pp"])
            notes.append(f"{on} offense: largest combined edge at {best['zone'].lower()} ({best['edge_pp']:+.1f} pp vs league); "
                         f"least favorable at {worst['zone'].lower()} ({worst['edge_pp']:+.1f} pp).")
        ff = side["four_factors"]
        if ff["offense_orb"] and ff["defense_drb"]:
            notes.append(f"{on} OREB% {ff['offense_orb']:.1f} vs {dn} DREB% {ff['defense_drb']:.1f}.")
        if ff["defense_forced_tov"] and ff["offense_tov"]:
            notes.append(f"{dn} forces turnovers on {ff['defense_forced_tov']:.1f}% of possessions; {on} commits {ff['offense_tov']:.1f}%.")
    return notes


def _pct(n, d):
    return round(100 * n / d, 1) if d else None


def _r(v, n=2):
    return None if v is None else round(v, n)
