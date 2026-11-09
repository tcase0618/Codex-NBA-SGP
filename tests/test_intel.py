"""Intelligence platform tests: parsing, reconstruction, models, metrics,
simulation accounting identities, projection pipeline, backtest and API.

All data here is synthetic (tests/intel_fixtures.py); no network is used."""
import math
from datetime import datetime, timezone

import numpy as np
import pytest

from nba_quant.intel import catalog as C
from nba_quant.intel import models as M
from nba_quant.intel.parse import clock_seconds, elapsed, parse_summary, shot_geometry
from nba_quant.intel.reconstruct import reconstruct
from nba_quant.intel.sim import GameInputs, _fill_minutes, _systematic, simulate
from nba_quant.intel.warehouse import Warehouse
from tests.intel_fixtures import build_league, make_summary


@pytest.fixture(scope="module")
def league(tmp_path_factory):
    wh = Warehouse(tmp_path_factory.mktemp("wh") / "w.sqlite")
    ids = build_league(wh)
    return wh, ids


# ------------------------------------------------------------------ parsing
def test_clock_and_elapsed():
    assert clock_seconds("11:29") == 689
    assert clock_seconds("45.2") == pytest.approx(45.2)
    assert clock_seconds("1:39:00") == 99  # provider artifact keeps m:s
    assert elapsed(1, 720) == 0 and elapsed(4, 0) == 2880 and elapsed(5, 0) == 3180


def test_shot_geometry_zones():
    assert shot_geometry(25, 1, 2)["zone"] == "rim"
    assert shot_geometry(25, 10, 2)["zone"] == "short_midrange"
    assert shot_geometry(25, 18, 2)["zone"] == "long_midrange"
    assert shot_geometry(2, 3, 3)["zone"] == "corner_three"
    assert shot_geometry(29, 27, 3)["distance_ft"] == pytest.approx(27.3, abs=.1)
    assert shot_geometry(None, None, 2)["zone"] == "unknown"


def test_parse_reconciles_box_and_market_line():
    payload, prov = make_summary("1", "1", "2", datetime(2025, 11, 1, tzinfo=timezone.utc), seed=3)
    p = parse_summary(payload, prov)
    g = p["game"]
    for t, score in ((g["home_id"], g["home_score"]), (g["away_id"], g["away_score"])):
        boxes = [b for b in p["boxes"] if b["team_id"] == t]
        assert sum(b["pts"] for b in boxes) == score
        assert all(b["pts"] == 2 * b["fgm"] + b["fg3m"] + b["ftm"] for b in boxes)
    assert p["market_line"]["home_spread"] == -2.5 and p["market_line"]["total"] == 220.5


def test_parse_rejects_incomplete_game():
    payload, prov = make_summary("1", "1", "2", datetime(2025, 11, 1, tzinfo=timezone.utc), seed=3)
    payload["header"]["competitions"][0]["status"]["type"]["completed"] = False
    with pytest.raises(ValueError):
        parse_summary(payload, prov)


# ------------------------------------------------------------------ reconstruction
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_reconstruction_identities(seed):
    payload, prov = make_summary("7", "1", "2", datetime(2025, 11, 1, tzinfo=timezone.utc), seed=seed)
    p = parse_summary(payload, prov)
    r = reconstruct(p)
    q = r["quality"]["checks"]
    assert q["possession_points_reconcile"] and q["stint_points_reconcile"] and q["pbp_final_matches_box"]
    assert q["lineups_always_five"] and q["shots_match_box_fga"] and q["stint_seconds_match_game"]
    assert all(len(s["home_lineup"]) == 5 and len(s["away_lineup"]) == 5 for s in r["stints"])
    # Possessions alternate within ±1 per period and end with a known reason.
    poss = r["possessions"]
    assert all(x["end_reason"] in {"made_fg", "made_ft", "dreb", "steal", "tov", "period_end", "change"} for x in poss)
    counts = r["quality"]["possessions"]
    assert abs(counts["1"] - counts["2"]) <= 4
    # Player on-court seconds sum to 5 x game length per team.
    for t in ("1", "2"):
        ids = [b["player_id"] for b in p["boxes"] if b["team_id"] == t]
        assert sum(r["player_onoff"].get(i, {}).get("seconds", 0) for i in ids) == pytest.approx(5 * 2880, abs=1)


def test_missing_substitution_is_repaired_and_counted():
    payload, prov = make_summary("8", "1", "2", datetime(2025, 11, 1, tzinfo=timezone.utc), seed=9)
    subs = [i for i, x in enumerate(payload["plays"]) if x["type"]["text"] == "Substitution"]
    assert subs, "fixture should contain substitutions"
    del payload["plays"][subs[0]]  # provider omits a substitution row
    r = reconstruct(parse_summary(payload, prov))
    assert r["quality"]["checks"]["lineups_always_five"]
    assert r["quality"]["checks"]["possession_points_reconcile"]
    assert r["quality"]["checks"]["lineup_repairs"] >= 1


def test_heave_rule_is_team_attempt():
    payload, prov = make_summary("9", "1", "2", datetime(2025, 11, 1, tzinfo=timezone.utc), seed=4)
    shooter = payload["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["athlete"]["id"]
    end = next(i for i, x in enumerate(payload["plays"]) if x["type"]["text"] == "End Period")
    seq = payload["plays"][end - 1]["sequenceNumber"]
    payload["plays"].insert(end, {**payload["plays"][end - 1], "id": "h", "sequenceNumber": str(int(seq)),
                                  "type": {"text": "Heave Jump Shot"}, "text": "P misses heave jump shot", "team": {"id": "1"},
                                  "participants": [{"athlete": {"id": shooter}}], "clock": {"displayValue": "0.5"},
                                  "scoringPlay": False, "scoreValue": 0, "shootingPlay": True, "pointsAttempted": 3,
                                  "coordinate": {"x": 25, "y": 60}})
    for i, x in enumerate(payload["plays"]):
        x["sequenceNumber"] = str(i + 1)
    r = reconstruct(parse_summary(payload, prov))
    heaves = [s for s in r["shots"] if s["team_attempt_heave"]]
    assert len(heaves) == 1
    assert r["quality"]["checks"]["shots_match_box_fga"]


# ------------------------------------------------------------------ warehouse + models
def test_warehouse_ingest_and_coverage(league):
    wh, ids = league
    cov = wh.coverage()
    assert cov["rows"]["games"] == len(ids)
    assert cov["rows"]["tracking_events"] == 0  # never synthesized
    assert {s["id"] for s in cov["unavailable_sources"]} >= {"optical-tracking"}
    assert wh.scalar("SELECT COUNT(*) FROM market_lines") == len(ids)
    assert wh.scalar("SELECT SUM(pbp_valid) FROM games") == len(ids)


def test_logistic_recovers_coefficients():
    rng = np.random.default_rng(0)
    X = np.column_stack([np.ones(20000), rng.normal(size=20000)])
    y = (rng.random(20000) < 1 / (1 + np.exp(-(-.3 + 1.2 * X[:, 1])))).astype(float)
    w = M.fit_logistic(X, y, l2=1e-6)
    assert w[0] == pytest.approx(-.3, abs=.06) and w[1] == pytest.approx(1.2, abs=.06)


def test_models_respect_cutoff(league):
    wh, ids = league
    first = wh.scalar("SELECT MIN(tipoff) FROM games")
    assert M.fit_team_ratings(wh, first) is None  # nothing before the first tipoff
    late = "2026-01-01T00:00Z"
    tm = M.fit_team_ratings(wh, late)
    assert tm["games"] == wh.scalar("SELECT COUNT(*) FROM games WHERE tipoff < ?", (late,))
    assert abs(sum(t["net_adj"] for t in tm["teams"].values())) < 1.0  # centered
    rp = M.fit_rapm(wh, late)
    assert rp and len(rp["players"]) > 20
    assert all(v["orapm_sd"] > 0 for v in rp["players"].values())


# ------------------------------------------------------------------ catalog
def test_catalog_counts_and_contract():
    counts = C.counts()
    assert counts["player"]["total"] == 100 and counts["team"]["total"] >= 240
    assert all(counts[p]["total"] == 20 for p in ("PG", "SG", "SF", "PF", "C"))
    defs = [m for cat in C.PLAYER.values() for m in cat] + [m for ms in C.POSITION.values() for m in ms] + [m for cat in C.TEAM.values() for m in cat]
    assert len({m["id"] for m in defs}) == len(defs)
    for m in defs:
        if m["status"] == "UNAVAILABLE":
            assert m["fn"] is None and m["reason"]
        else:
            assert m["fn"] is not None and m["formula"]
    # Movement metrics require tracking and must not be faked.
    assert all(m["status"] == "UNAVAILABLE" for m in C.PLAYER["Movement and athleticism"])


# ------------------------------------------------------------------ simulation
def synthetic_inputs(P=12):
    def team(off):
        return {"ids": [f"{off}{i}" for i in range(P)], "active_prob": [1.0] * 8 + [.8] * (P - 8),
                "min_mean": [34, 33, 32, 30, 28, 22, 18, 15, 10, 8, 5, 4][:P], "min_sd": [3.0] * P,
                "usage": [.28, .24, .2, .18, .16, .2, .18, .15, .15, .12, .12, .1][:P], "tov": [.12] * P,
                "foul_draw": [.09] * P, "ft_pct": [.78] * P, "three_ft": [.03] * P, "ast_w": [.06] * P,
                "oreb_w": [.05] * P, "dreb_w": [.15] * P, "stl_w": [.016] * P, "blk_w": [.01] * P, "pf_w": [.04] * P,
                "zone_mix": [[.3, .15, .15, .1, .3]] * P, "zone_fg": [[.66, .44, .41, .38, .35]] * P,
                "def_zone_adj": [0.0] * 5}
    return GameInputs([team("h"), team("a")], pace=99.0, league={"oreb_balance": 1 / 3})


def test_systematic_sampler_exact_five():
    rng = np.random.default_rng(1)
    pi = rng.random((5000, 12))
    pi = pi / pi.sum(1, keepdims=True) * 5
    pi = np.minimum(pi, 1)
    for _ in range(10):
        over = pi.sum(1, keepdims=True)
        pi = np.minimum(1, pi * 5 / over)
    sel = _systematic(rng, pi)
    assert np.all(sel.sum(1) == 5)


def test_fill_minutes_trims_bench_first():
    m = np.array([[36.0, 35, 34, 33, 32, 30, 25, 20, 15]])
    out = _fill_minutes(m)
    assert out.sum() == pytest.approx(240)
    assert out[0, 0] == pytest.approx(36) and out[0, -1] == 0  # starters kept, deep bench cut


def test_simulation_accounting_and_determinism():
    inp = synthetic_inputs()
    a = simulate(inp, 400, seed=5)
    b = simulate(inp, 400, seed=5)
    assert np.array_equal(a["score"], b["score"])
    assert a["score"].shape == (400, 2)  # simulation count controls the run
    for s in (0, 1):
        box = a["box"][s]
        assert np.array_equal(box["pts"].sum(1), a["score"][:, s])
        assert np.allclose(box["minutes"].sum(1), 240 + 25 * a["overtime"])
        assert np.all(box["ast"].sum(1) <= box["fgm"].sum(1))
        assert np.array_equal(box["reb"], box["oreb"] + box["dreb"])
        assert np.allclose(a["quarter"][:, s, :].sum(1), a["score"][:, s])
    assert np.all(a["score"][:, 0] != a["score"][:, 1])
    assert 80 < a["score"].mean() < 150


def test_simulation_shift_moves_efficiency():
    inp = synthetic_inputs()
    inp.shifts = [0.0, 0.0]
    base = simulate(inp, 600, seed=2)["score"][:, 0].mean()
    inp.shifts = [0.4, 0.0]
    up = simulate(inp, 600, seed=2)["score"][:, 0].mean()
    assert up > base + 3


# ------------------------------------------------------------------ engine, projection, backtest, API
def test_engine_profiles(league):
    from nba_quant.intel.engine import Engine
    wh, _ = league
    eng = Engine(wh)
    cutoff = eng.latest_cutoff()
    pid = wh.query("SELECT player_id FROM player_games WHERE minutes > 20 LIMIT 1")[0]["player_id"]
    prof = eng.player_profile(pid, cutoff)
    vals = {m["id"]: m for m in prof["metrics"]}
    assert vals["p_pts_g"]["value"] is not None and vals["p_pts_g"]["status"] == "OBSERVED"
    assert vals["p_mv_0"]["value"] is None and vals["p_mv_0"]["status"] == "UNAVAILABLE"
    assert 0 <= vals["p_ts"]["league_pct"] <= 100
    team = eng.team_profile("1", cutoff)
    tm = {m["id"]: m for m in team["metrics"]}
    assert tm["t_ortg"]["value"] > 0 and tm["t_ortg"]["rank"] >= 1
    assert sum(m["value"] is not None for m in team["metrics"]) > 120
    home = eng.team_profile("1", cutoff, venue="home")
    assert home["games"] < team["games"]


def test_projection_pipeline(league):
    from nba_quant.intel.engine import Engine
    from nba_quant.intel.projection import Projector
    wh, _ = league
    eng = Engine(wh)
    pj = Projector(eng)
    cutoff = eng.latest_cutoff()
    out = pj.project("1", "2", cutoff, sims=600, seed=3)
    assert 0 < out["home_win_probability"] < 1
    assert all(all(c.values()) for c in out["checks"].values())
    pl = out["players"][0]
    assert {"pra", "pr", "pa", "ra", "stocks"} <= set(pl["stats"])
    # PRA comes from the same simulated games, so its mean equals the sum of means.
    assert pl["stats"]["pra"]["mean"] == pytest.approx(pl["stats"]["pts"]["mean"] + pl["stats"]["reb"]["mean"] + pl["stats"]["ast"]["mean"], abs=0.01)
    # Scenario: rule the top player out -> he never plays.
    top = pl["id"]
    out2 = pj.project("1", "2", cutoff, sims=600, seed=3, scenarios=[{"player_id": top, "active_probability": 0.0}])
    assert next(p for p in out2["players"] if p["id"] == top)["played_prob"] == 0


def test_backtest_walk_forward_scoring(league):
    from nba_quant.intel import backtest as B
    wh, _ = league
    run = B.run(wh, "2025-11-20T00:00Z", "2025-12-10T00:00Z", sims=300, workers=1, progress=lambda *_: None)
    ok = [f for f in run["forecasts"] if "error" not in f]
    assert ok, run["forecasts"][:2]
    for f in ok:  # strict information ordering
        assert f["model_cutoff"] <= f["cutoff"] < f["tipoff"]
    rep = B.score(wh, run["forecasts"])
    assert rep["status"] == "MEASURED_WALK_FORWARD"
    assert 0 < rep["game"]["win_prob"]["simulation"]["log_loss"] < 2
    assert rep["players"]["stats"]["pts"]["n"] > 0
    dist = rep["players"]["distribution"]["pts"]
    assert dist["n"] > 0 and 0 <= dist["pit_central80_coverage"] <= 1 and len(dist["pit_histogram"]) == 10
    path = B.save(wh, run, rep, out_dir=str(wh.path) + "-bt")
    assert path.exists()
    assert rep["game"]["platt_live"]["games"] == len(ok)
    assert B.latest_platt(wh) is None  # too few games to deploy a calibration layer
    assert B.latest_platt(wh, "some-other-version") is None


def test_intel_api(league):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from nba_quant.intel.api import register
    wh, _ = league
    app = FastAPI()
    register(app, wh.path)
    c = TestClient(app)
    s = c.get("/api/intel/status").json()
    assert s["status"] == "READY" and s["llm_required"] is False
    assert c.get("/api/intel/catalog").json()["counts"]["team"]["total"] >= 240
    assert len(c.get("/api/intel/teams").json()["teams"]) == 4
    assert c.get("/api/intel/team/1").status_code == 200
    assert c.get("/api/intel/matchup?home_id=1&away_id=2").status_code == 200
    assert c.get("/api/intel/lineups/1?min_poss=0").json()["lineups"]
    assert c.get("/api/intel/shots").status_code == 422
    r = c.post("/api/intel/project", json={"home_id": "1", "away_id": "1"})
    assert r.status_code == 422
    r = c.post("/api/intel/project", json={"home_id": "1", "away_id": "2", "simulations": 100})
    assert r.status_code == 422  # below the 500 minimum
    job = c.post("/api/intel/project", json={"home_id": "1", "away_id": "2", "simulations": 500}).json()["job_id"]
    import time
    for _ in range(120):
        j = c.get(f"/api/intel/jobs/{job}").json()
        if j["status"] != "running":
            break
        time.sleep(.5)
    assert j["status"] == "done", j
    assert j["result"]["simulations"] == 500


def test_intel_api_empty_warehouse(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from nba_quant.intel.api import register
    app = FastAPI()
    register(app, str(tmp_path / "missing.sqlite"))
    c = TestClient(app)
    assert c.get("/api/intel/status").json()["status"] == "EMPTY"
    assert c.get("/api/intel/teams").status_code == 503


def test_randomized_pit_is_exact_for_a_correct_discrete_forecast():
    """A perfectly calibrated integer forecast gets ~80% randomized-PIT coverage,
    while the inclusive p10<=y<=p90 check over-reports (the v1.0 audit's bias)."""
    from nba_quant.intel import backtest as B
    rng = np.random.default_rng(0)
    lam = 1.3
    cdf = B._poisson_cdf(lam, 30)
    ys = rng.poisson(lam, 20000)
    rows = [{"pl": {"cdf": {k: cdf for k in ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")},
                    "baseline": {k: lam for k in ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")}},
             "act": {k: int(y) for k in ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")}} for y in ys]
    d = B.distribution_scores(rows)["blk"]
    assert d["pit_central80_coverage"] == pytest.approx(0.80, abs=0.015)
    assert d["rps_model"] == pytest.approx(d["rps_poisson_last10"], rel=1e-6)  # same distribution
    p10 = next(k for k, f in enumerate(cdf) if f >= .1)
    p90 = next(k for k, f in enumerate(cdf) if f >= .9)
    inclusive = np.mean((ys >= p10) & (ys <= p90))
    assert inclusive > 0.85  # the biased check


def test_injury_report_token_parser_edge_cases():
    from nba_quant.intel.injuries import parse_tokens, to_first_last, _norm
    text = ("Injury Report: 01/15/26 05:00 PM Page 1 of 2 Game Date Game Time Matchup Team Player Name Current Status Reason "
            "01/15/2026 07:00 (ET) OKC@MIA Oklahoma City Thunder Gilgeous- Alexander, Shai Questionable Injury/Illness - Left Ankle; Sprain "
            "Pippen Jr., Scotty Out Injury/Illness - Left Great Toe; Surgery, Recovery Williams, Jalen Probable Rest "
            "Miami Heat NOT YET SUBMITTED")
    teams = parse_tokens(text.split())["teams"]
    okc = teams["Oklahoma City Thunder"]["players"]
    assert [(p["name"], p["status"]) for p in okc] == [("Gilgeous-Alexander, Shai", "Questionable"),
                                                      ("Pippen Jr., Scotty", "Out"), ("Williams, Jalen", "Probable")]
    assert teams["Oklahoma City Thunder"]["submitted"] and not teams["Miami Heat"]["submitted"]
    assert _norm(to_first_last("Pippen Jr., Scotty")) == _norm("Scotty Pippen Jr.")


def test_injury_report_respects_cutoff(league):
    import json as _json
    from nba_quant.intel.injuries import _ensure_tables, report_for
    wh, _ = league
    with wh.connect() as db:
        _ensure_tables(db)
        db.execute("INSERT OR REPLACE INTO injury_reports VALUES(?,?,?,?,?,?)",
                   ("u-early", "2025-11-01T21:00Z", "2025-11-01", "x", _json.dumps(["1"]), "[]"))
        db.execute("INSERT OR REPLACE INTO injury_reports VALUES(?,?,?,?,?,?)",
                   ("u-late", "2025-11-01T23:30Z", "2025-11-01", "x", _json.dumps(["1"]), "[]"))
        db.execute("INSERT OR REPLACE INTO injuries VALUES(?,?,?,?,?,?)", ("1", "100", "Questionable", "", "2025-11-01T21:00Z", "u-early"))
        db.execute("INSERT OR REPLACE INTO injuries VALUES(?,?,?,?,?,?)", ("1", "100", "Out", "", "2025-11-01T23:30Z", "u-late"))
    # Tipoff 8 PM ET; cutoff 23:00Z precedes the late report, so only the early listing is usable.
    status, url = report_for(wh, "1", "2025-11-02T00:00Z", "2025-11-01T23:00Z")
    assert url == "u-early" and status == {"100": "Questionable"}
    status, url = report_for(wh, "1", "2025-11-02T00:00Z", "2025-11-01T23:45Z")
    assert url == "u-late" and status == {"100": "Out"}
    status, url = report_for(wh, "2", "2025-11-02T00:00Z", "2025-11-01T23:45Z")
    assert status is None  # team 2 had not submitted: no information, not "all available"
