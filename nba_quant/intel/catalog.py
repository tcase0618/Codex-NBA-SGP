"""Metric definition registry.

Status classes
  OBSERVED     provider box-score value (ESPN) aggregated over the window
  DERIVED      deterministic formula over reconstructed play-by-play
  ESTIMATED    documented proxy for a concept the provider does not publish
  MODELED      output of a fitted model (xFG, RAPM, team ridge, minutes)
  UNAVAILABLE  requires data with no authorized source here; value is null

Every function receives an aggregate `a` (missing keys read as 0) and returns
a value or None. `n` names the sample used for uncertainty and `kind` selects
the standard-error formula (pct: binomial, rate: Poisson, mean: sd/sqrt(n)).
"""

TRACKING = "Requires licensed optical tracking (Second Spectrum); not available."
PLAYTYPE = "Requires play-type (Synergy) classification; not available."
SCHEME = "Requires defensive coverage tagging (drop/switch/hedge/blitz/zone); not available."
MATCHUP = "Requires defender matchup tracking; not available."
PASSING = "Requires pass tracking (potential/secondary assists, passes); not available."


def d(n, den, scale=1.0):
    return None if not den else n / den * scale


class Agg(dict):
    def __missing__(self, key):
        return 0


def M(mid, label, unit, status, formula, fn=None, n=None, kind="rate", reason=None, higher=True, opp_adj=False):
    return {"id": mid, "label": label, "unit": unit, "status": status, "formula": formula, "fn": fn,
            "n": n, "kind": kind, "reason": reason, "higher_is_better": higher, "opp_adjustable": opp_adj}


def U(mid, label, reason, unit="metric_specific"):
    return M(mid, label, unit, "UNAVAILABLE", None, reason=reason)


def poss(a):
    return (a["on_po"] + a["on_pd"]) / 2


# --------------------------------------------------------------------------- universal player (100)
PLAYER = {
"Scoring": [
    M("p_pts_g", "Points / game", "pts", "OBSERVED", "PTS / GP", lambda a: d(a["pts"], a["gp"]), "gp", "mean", opp_adj=True),
    M("p_pts_36", "Points / 36 minutes", "pts", "DERIVED", "36 * PTS / MIN", lambda a: d(a["pts"], a["min"], 36), "pts"),
    M("p_pts_100", "Points / 100 possessions", "pts", "DERIVED", "100 * PTS / on-court offensive possessions", lambda a: d(a["pts"], a["on_po"], 100), "pts", opp_adj=True),
    M("p_fga_100", "Field goal attempts / 100", "att", "DERIVED", "100 * FGA / on-court offensive possessions", lambda a: d(a["fga"], a["on_po"], 100), "fga"),
    M("p_fta_100", "Free throw attempts / 100", "att", "DERIVED", "100 * FTA / on-court offensive possessions", lambda a: d(a["fta"], a["on_po"], 100), "fta"),
    M("p_paint_pts", "Paint scoring", "pts/g", "ESTIMATED", "Points on made FGs within 14 ft (rim + short midrange) / GP; paint boundary approximated by distance", lambda a: d(a["paint_pts"], a["gp"]), "gp", "mean"),
    M("p_trans_pts", "Transition scoring", "pts/g", "ESTIMATED", "FG points where first action <= 8 s after a defensive rebound or steal / GP", lambda a: d(a["trans_pts"], a["gp"]), "gp", "mean"),
    M("p_2c_pts", "Second-chance scoring", "pts/g", "DERIVED", "FG points after an offensive rebound in the same possession / GP", lambda a: d(a["sc_pts"], a["gp"]), "gp", "mean"),
    M("p_2pt_pts", "Two-point scoring", "pts/g", "OBSERVED", "2 * (FGM - 3PM) / GP", lambda a: d(2 * (a["fgm"] - a["fg3m"]), a["gp"]), "gp", "mean"),
    M("p_3pt_pts", "Three-point scoring", "pts/g", "OBSERVED", "3 * 3PM / GP", lambda a: d(3 * a["fg3m"], a["gp"]), "gp", "mean"),
],
"Shooting efficiency": [
    M("p_ts", "True shooting %", "%", "DERIVED", "PTS / (2 * (FGA + 0.44 * FTA))", lambda a: d(a["pts"], 2 * (a["fga"] + .44 * a["fta"]), 100), "fga", "pct", opp_adj=True),
    M("p_efg", "Effective FG%", "%", "DERIVED", "(FGM + 0.5 * 3PM) / FGA", lambda a: d(a["fgm"] + .5 * a["fg3m"], a["fga"], 100), "fga", "pct"),
    M("p_2p", "2PT%", "%", "OBSERVED", "(FGM - 3PM) / (FGA - 3PA)", lambda a: d(a["fgm"] - a["fg3m"], a["fga"] - a["fg3a"], 100), "fg2a", "pct"),
    M("p_3p", "3PT%", "%", "OBSERVED", "3PM / 3PA", lambda a: d(a["fg3m"], a["fg3a"], 100), "fg3a", "pct"),
    M("p_ft", "FT%", "%", "OBSERVED", "FTM / FTA", lambda a: d(a["ftm"], a["fta"], 100), "fta", "pct"),
    M("p_rim", "Rim FG%", "%", "DERIVED", "Made / attempted FGs within 4 ft", lambda a: d(a["z_rim_m"], a["z_rim_a"], 100), "z_rim_a", "pct"),
    M("p_mid", "Midrange FG%", "%", "DERIVED", "Made / attempted 2PT FGs beyond 4 ft", lambda a: d(a["z_sm_m"] + a["z_lm_m"], a["z_sm_a"] + a["z_lm_a"], 100), "z_mid_a", "pct"),
    M("p_c3", "Corner 3PT%", "%", "DERIVED", "Corner (<= 9 ft from baseline-hoop line) 3PM / 3PA", lambda a: d(a["z_c3_m"], a["z_c3_a"], 100), "z_c3_a", "pct"),
    M("p_ab3", "Above-break 3PT%", "%", "DERIVED", "Non-corner 3PM / 3PA", lambda a: d(a["z_ab3_m"], a["z_ab3_a"], 100), "z_ab3_a", "pct"),
    M("p_smoe", "Shot making above expected", "pp", "MODELED", "100 * (FGM - sum xFG) / FGA (xFG logistic model, heaves excluded)", lambda a: d(a["s_fgm"] - a["s_xfg"], a["s_n"], 100), "s_n", "pct"),
],
"Shot profile": [
    M("p_rim_rate", "Rim attempt share", "%", "DERIVED", "Rim FGA / FGA", lambda a: d(a["z_rim_a"], a["s_n"], 100), "s_n", "pct"),
    M("p_mid_rate", "Midrange attempt share", "%", "DERIVED", "Non-rim 2PT FGA / FGA", lambda a: d(a["z_sm_a"] + a["z_lm_a"], a["s_n"], 100), "s_n", "pct"),
    M("p_3pa_rate", "Three-point attempt share", "%", "OBSERVED", "3PA / FGA", lambda a: d(a["fg3a"], a["fga"], 100), "fga", "pct"),
    M("p_pull_rate", "Pull-up attempt share", "%", "ESTIMATED", "FGA described as pull-up or step-back / FGA (provider shot description)", lambda a: d(a["pull_a"] + a["step_a"], a["s_n"], 100), "s_n", "pct"),
    M("p_cs_rate", "Catch-and-shoot attempt share", "%", "ESTIMATED", "Jump shots not described as pull-up/step-back/fadeaway/turnaround / FGA (proxy, no touch tracking)", lambda a: d(a["cs_a"], a["s_n"], 100), "s_n", "pct"),
    M("p_dist", "Shot distance", "ft", "DERIVED", "Mean FGA distance from hoop (coordinates)", lambda a: d(a["s_dist"], a["s_n"]), "s_n", "mean"),
    U("p_contested", "Contested shot rate", TRACKING),
    U("p_open", "Open shot rate", TRACKING),
    M("p_xpps", "Expected points per shot", "pts", "MODELED", "sum(xFG * shot value) / FGA", lambda a: d(a["s_xpts"], a["s_n"]), "s_n", "mean"),
    M("p_diff_eff", "Shot difficulty-adjusted efficiency", "pp eFG", "MODELED", "50 * (points per shot - expected points per shot)", lambda a: d(a["s_pts"] - a["s_xpts"], a["s_n"], 50), "s_n", "pct"),
],
"Playmaking": [
    M("p_ast_100", "Assists / 100", "ast", "DERIVED", "100 * AST / on-court offensive possessions", lambda a: d(a["ast"], a["on_po"], 100), "ast"),
    U("p_pot_ast", "Potential assists", PASSING),
    U("p_xast", "Expected assists", PASSING),
    U("p_sec_ast", "Secondary assists", PASSING),
    M("p_ast_pts", "Assist points created", "pts/g", "DERIVED", "Points on teammates' FGs credited to the player's assist / GP", lambda a: d(a["ast_pts"], a["gp"]), "gp", "mean"),
    U("p_passes", "Passes / game", PASSING),
    U("p_drive_kick", "Drive-and-kick passes", TRACKING),
    M("p_ast_tov", "Passing efficiency", "ratio", "ESTIMATED", "AST / bad-pass turnovers (proxy for pass value per pass error)", lambda a: d(a["ast"], max(a["tov_bad"], 1)), "ast"),
    U("p_ast_conv", "Assist conversion rate", PASSING),
    M("p_pass_tov", "Passing turnovers", "/100", "DERIVED", "100 * bad-pass turnovers / on-court offensive possessions", lambda a: d(a["tov_bad"], a["on_po"], 100), "tov_bad", higher=False),
],
"Ball handling and usage": [
    M("p_usg", "Usage rate", "%", "DERIVED", "(FGA + 0.44 FTA + TOV) / on-court team (FGA + 0.44 FTA + TOV)", lambda a: d(a["fga"] + .44 * a["fta"] + a["tov"], a["on_tm_fga"] + .44 * a["on_tm_fta"] + a["on_tm_tov"], 100), "fga", "pct"),
    U("p_touches", "Touches / game", TRACKING), U("p_fc_touches", "Frontcourt touches", TRACKING),
    U("p_top", "Time of possession", TRACKING), U("p_sec_touch", "Seconds per touch", TRACKING),
    U("p_drib_touch", "Dribbles per touch", TRACKING),
    M("p_tov_rate", "Turnover rate", "%", "DERIVED", "TOV / (FGA + 0.44 FTA + TOV)", lambda a: d(a["tov"], a["fga"] + .44 * a["fta"] + a["tov"], 100), "tov", "pct", higher=False),
    M("p_badpass", "Bad-pass turnover rate", "/100", "DERIVED", "100 * bad-pass TOV / on-court offensive possessions", lambda a: d(a["tov_bad"], a["on_po"], 100), "tov_bad", higher=False),
    M("p_lostball", "Lost-ball turnover rate", "/100", "DERIVED", "100 * lost-ball TOV / on-court offensive possessions", lambda a: d(a["tov_lost"], a["on_po"], 100), "tov_lost", higher=False),
    U("p_adv_touch", "Advantage creation per touch", TRACKING),
],
"Rebounding": [
    M("p_orb", "Offensive rebound %", "%", "DERIVED", "OREB / (team OREB + opponent DREB) while on court", lambda a: d(a["oreb"], a["on_oreb"] + a["on_opp_dreb"], 100), "reb_chances_o", "pct"),
    M("p_drb", "Defensive rebound %", "%", "DERIVED", "DREB / (team DREB + opponent OREB) while on court", lambda a: d(a["dreb"], a["on_dreb"] + a["on_opp_oreb"], 100), "reb_chances_d", "pct"),
    M("p_trb", "Total rebound %", "%", "DERIVED", "REB / all available rebounds while on court", lambda a: d(a["oreb"] + a["dreb"], a["on_oreb"] + a["on_opp_dreb"] + a["on_dreb"] + a["on_opp_oreb"], 100), "reb_chances", "pct"),
    U("p_reb_chances", "Rebound chances", TRACKING), U("p_reb_conv", "Rebound chance conversion", TRACKING),
    U("p_creb", "Contested rebound %", TRACKING), U("p_ucreb", "Uncontested rebound %", TRACKING),
    U("p_boxouts", "Boxouts", TRACKING),
    M("p_putback_pts", "Second-chance points created", "pts/g", "ESTIMATED", "Putback/tip FG points / GP (own offensive-rebound conversions)", lambda a: d(a["putback_pts"], a["gp"]), "gp", "mean"),
    U("p_reb_pos", "Rebound positioning above expected", TRACKING),
],
"Defense": [
    M("p_def_poss", "Defensive possessions", "poss", "DERIVED", "Opponent possessions while on court", lambda a: a["on_pd"], "gp", "count"),
    U("p_matchup_pa", "Matchup-adjusted points allowed", MATCHUP),
    U("p_deflections", "Deflections", TRACKING),
    M("p_stl_100", "Steals / 100", "stl", "DERIVED", "100 * STL / opponent possessions on court", lambda a: d(a["stl"], a["on_pd"], 100), "stl"),
    M("p_blk_100", "Blocks / 100", "blk", "DERIVED", "100 * BLK / opponent possessions on court", lambda a: d(a["blk"], a["on_pd"], 100), "blk"),
    U("p_contests", "Contested shots", TRACKING),
    M("p_opp_rim", "Opponent rim FG%", "%", "DERIVED", "Opponent rim FG% while on court (team-level on-court, not matchup)", lambda a: d(a["def_rim_m"], a["def_rim_a"], 100), "def_rim_a", "pct", higher=False),
    M("p_pf_100", "Fouls / 100", "pf", "DERIVED", "100 * PF / possessions played", lambda a: d(a["pf"], poss(a), 100), "pf", higher=False),
    M("p_dreb_share", "Defensive rebound contribution", "%", "DERIVED", "Player DREB / team DREB while on court", lambda a: d(a["dreb"], a["on_dreb"], 100), "on_dreb", "pct"),
    M("p_dimpact", "Estimated defensive impact", "pts/100", "MODELED", "Defensive RAPM (ridge, stint-level, positive = suppresses scoring)", lambda a: a.get("drapm"), kind="model"),
],
"Movement and athleticism": [U(f"p_mv_{i}", label, TRACKING) for i, label in enumerate([
    "Distance traveled", "Average movement speed", "Offensive speed", "Defensive speed", "Sprint frequency",
    "Transition speed", "Acceleration events", "Deceleration events", "High-speed distance", "Movement workload estimate"])],
"Player impact": [
    M("p_on_ortg", "On-court offensive rating", "pts/100", "DERIVED", "100 * team points / team possessions while on court", lambda a: d(a["on_pf"], a["on_po"], 100), "on_po"),
    M("p_on_drtg", "On-court defensive rating", "pts/100", "DERIVED", "100 * opponent points / opponent possessions while on court", lambda a: d(a["on_pa"], a["on_pd"], 100), "on_pd", higher=False),
    M("p_on_net", "On-court net rating", "pts/100", "DERIVED", "On ORtg - On DRtg", lambda a: None if not (a["on_po"] and a["on_pd"]) else 100 * (a["on_pf"] / a["on_po"] - a["on_pa"] / a["on_pd"]), "on_po", "mean"),
    M("p_onoff", "On/off net differential", "pts/100", "DERIVED", "On-court net - off-court net (same team, same games)", lambda a: None if not (a["on_po"] and a["on_pd"] and a["off_po"] > 50 and a["off_pd"] > 50) else 100 * ((a["on_pf"] / a["on_po"] - a["on_pa"] / a["on_pd"]) - (a["off_pf"] / a["off_po"] - a["off_pa"] / a["off_pd"])), "on_po", "mean"),
    M("p_apm", "Adjusted plus-minus", "pts/100", "MODELED", "Lightly regularized stint regression (lambda=300)", lambda a: a.get("apm"), kind="model"),
    M("p_rapm", "Regularized adjusted plus-minus", "pts/100", "MODELED", "Ridge stint regression, ORAPM + DRAPM (lambda=3000)", lambda a: a.get("rapm"), kind="model"),
    M("p_oimpact", "Offensive impact estimate", "pts/100", "MODELED", "Offensive RAPM", lambda a: a.get("orapm"), kind="model"),
    M("p_dimpact2", "Defensive impact estimate", "pts/100", "MODELED", "Defensive RAPM", lambda a: a.get("drapm"), kind="model"),
    M("p_war", "Wins above replacement estimate", "wins", "MODELED", "(RAPM + 2.0) * possessions / 100 / 30 points per win (replacement = -2.0/100)", lambda a: None if a.get("rapm") is None else (a["rapm"] + 2.0) * poss(a) / 100 / 30, kind="model"),
    M("p_impact_sd", "Impact estimate uncertainty", "pts/100", "MODELED", "Approximate ridge posterior SD of RAPM", lambda a: a.get("rapm_sd"), kind="model", higher=False),
],
"Consistency and availability": [
    M("p_min_g", "Minutes / game", "min", "OBSERVED", "MIN / GP", lambda a: d(a["min"], a["gp"]), "gp", "mean"),
    M("p_min_sd", "Minutes variance", "min SD", "DERIVED", "SD of minutes in games played", lambda a: a.get("min_sd"), "gp", "count", higher=False),
    M("p_gp", "Games played", "games", "OBSERVED", "Games with minutes > 0", lambda a: a["gp"], kind="count"),
    M("p_missed", "Games missed", "games", "OBSERVED", "Team games in window where player was listed DNP/inactive", lambda a: a["dnp"], kind="count", higher=False),
    M("p_l5_usg", "Last 5 game usage", "%", "DERIVED", "Usage proxy (FGA + .44 FTA + TOV)/team over last 5 games played", lambda a: a.get("l5_usg"), kind="count"),
    M("p_l10_ts", "Last 10 game TS%", "%", "DERIVED", "TS% over last 10 games played", lambda a: a.get("l10_ts"), kind="count"),
    M("p_pts_sd", "Game scoring variance", "pts SD", "DERIVED", "SD of points in games played", lambda a: a.get("pts_sd"), kind="count", higher=False),
    M("p_foul_trouble", "Foul trouble frequency", "%", "DERIVED", "Share of games with >= 3 personal fouls before halftime", lambda a: d(a["foul_trouble_g"], a["gp"], 100), "gp", "pct", higher=False),
    M("p_proj_min", "Projected minutes", "min", "MODELED", "Exponentially weighted recent minutes shrunk to window mean (half-life 5 games)", lambda a: a.get("proj_min"), kind="model"),
    M("p_avail_impact", "Availability-adjusted impact", "pts/100*share", "MODELED", "RAPM * participation rate * projected minutes / 48", lambda a: None if a.get("rapm") is None or not a.get("proj_min") else a["rapm"] * d(a["gp"], a["gp"] + a["dnp"]) * a["proj_min"] / 48, kind="model"),
],
}

# --------------------------------------------------------------------------- positional (20 each)
def _rate(key_m, key_a):
    return lambda a: d(a[key_m], a[key_a], 100)


POSITION = {
"PG": [
    U("pg_pot_ast", "Potential assists / 100", PASSING), U("pg_xast", "Expected assist value", PASSING),
    M("pg_hv_pass", "High-value passes created", "/100", "ESTIMATED", "100 * assists on rim or corner-three makes / on-court possessions", lambda a: d(a["ast_rim"] + a["ast_c3"], a["on_po"], 100), "ast"),
    U("pg_paint_touch", "Paint-touch creation rate", TRACKING), U("pg_drive", "Drive penetration rate", TRACKING),
    U("pg_drive_ast", "Drive assist %", TRACKING), U("pg_drive_tov", "Drive turnover %", TRACKING),
    U("pg_pnr", "Pick-and-roll handler PPP", PLAYTYPE), U("pg_iso", "Isolation PPP", PLAYTYPE),
    M("pg_late", "Late-clock creation PPP", "pts/shot", "ESTIMATED", "Points per FGA taken >= 18 s into the possession (no shot clock feed; resets ignored)", lambda a: d(a["late_pts"], a["late_a"]), "late_a", "mean"),
    U("pg_pressure", "Ball pressure turnover rate", TRACKING), U("pg_trap", "Trap escape rate", SCHEME),
    U("pg_switch", "Switch efficiency", SCHEME), U("pg_drop", "Drop coverage efficiency", SCHEME),
    U("pg_hedge", "Hedge coverage efficiency", SCHEME), U("pg_traps_pass", "Passing versus traps", SCHEME),
    M("pg_control", "Possession control index", "%", "ESTIMATED", "AST / (AST + TOV)", lambda a: d(a["ast"], a["ast"] + a["tov"], 100), "ast", "pct"),
    M("pg_init_ortg", "Offensive efficiency while initiating", "pts/100", "ESTIMATED", "On-court team ORtg (initiation not tracked)", lambda a: d(a["on_pf"], a["on_po"], 100), "on_po"),
    M("pg_clutch", "Clutch creation", "pts/g", "DERIVED", "(Own clutch FG points + clutch assist points) / GP; clutch = last 5:00 of 4th/OT, margin <= 5", lambda a: d(a["clutch_pts"] + a["clutch_ast_pts"], a["gp"]), "gp", "mean"),
    U("pg_adv", "Advantage creation per touch", TRACKING),
],
"SG": [
    M("sg_pu3", "Pull-up 3PT%", "%", "ESTIMATED", "3PM / 3PA on attempts described as pull-up or step-back", _rate("pull3_m", "pull3_a"), "pull3_a", "pct"),
    M("sg_pu3a", "Pull-up 3PA / 100", "att", "ESTIMATED", "100 * pull-up/step-back 3PA / on-court possessions", lambda a: d(a["pull3_a"], a["on_po"], 100), "pull3_a"),
    M("sg_cs3", "Catch-and-shoot 3PT%", "%", "ESTIMATED", "3PM / 3PA on plain jump shots (proxy)", _rate("cs3_m", "cs3_a"), "cs3_a", "pct"),
    U("sg_offscreen", "Off-screen PPP", PLAYTYPE), U("sg_iso", "Isolation PPP", PLAYTYPE),
    M("sg_step", "Step-back efficiency", "eFG%", "ESTIMATED", "eFG% on step-back attempts", lambda a: d(a["step_m"] + .5 * a["step3_m"], a["step_a"], 100), "step_a", "pct"),
    U("sg_tight", "Tightly contested shot %", TRACKING),
    M("sg_xdiff", "Expected shot difficulty", "%", "MODELED", "100 * (1 - mean xFG)", lambda a: None if not a["s_n"] else 100 * (1 - a["s_xfg"] / a["s_n"]), "s_n", "pct", higher=False),
    M("sg_smoe", "Shot making above expected", "pp", "MODELED", "100 * (FGM - xFGM) / FGA", lambda a: d(a["s_fgm"] - a["s_xfg"], a["s_n"], 100), "s_n", "pct"),
    M("sg_self", "Self-created scoring share", "%", "DERIVED", "Unassisted FGM / FGM", lambda a: d(a["s_fgm"] - a["asd_m"], a["s_fgm"], 100), "s_fgm", "pct"),
    U("sg_move", "Movement shooting efficiency", TRACKING), U("sg_reloc", "Relocation three-point efficiency", TRACKING),
    U("sg_spot", "Spot-up PPP", PLAYTYPE), U("sg_gravity", "Off-ball gravity", TRACKING),
    U("sg_closeout", "Closeout attack PPP", PLAYTYPE), U("sg_sec_ast", "Secondary assists", PASSING),
    U("sg_matchdiff", "Perimeter matchup difficulty", MATCHUP), U("sg_screen", "Screen navigation success", TRACKING),
    U("sg_oppguard", "Opponent guard scoring efficiency", MATCHUP),
    M("sg_trans", "Transition PPP", "pts/shot", "ESTIMATED", "FG points per transition FGA", lambda a: d(a["trans_pts"], a["trans_a"]), "trans_a", "mean"),
],
"SF": [
    U("sf_iso", "Wing isolation PPP", PLAYTYPE), U("sf_spot", "Spot-up PPP", PLAYTYPE),
    M("sf_cut", "Cutting PPP", "pts/shot", "ESTIMATED", "FG points per FGA described as cutting", lambda a: d(a["cut_pts"], a["cut_a"]), "cut_a", "mean"),
    M("sf_trans", "Transition PPP", "pts/shot", "ESTIMATED", "FG points per transition FGA", lambda a: d(a["trans_pts"], a["trans_a"]), "trans_a", "mean"),
    U("sf_post", "Post-up mismatch efficiency", PLAYTYPE),
    M("sf_cs3", "Catch-and-shoot 3PT%", "%", "ESTIMATED", "3PM / 3PA on plain jump shots (proxy)", _rate("cs3_m", "cs3_a"), "cs3_a", "pct"),
    U("sf_closeout", "Closeout attack efficiency", PLAYTYPE),
    M("sf_rim_xp", "Rim finishing through contact", "pp", "MODELED", "Rim FG% minus expected rim FG% (contact not tagged)", lambda a: d(a["z_rim_m"] - a["z_rim_x"], a["z_rim_a"], 100), "z_rim_a", "pct"),
    M("sf_ast", "Secondary playmaking", "/100", "DERIVED", "Assists / 100 on-court possessions", lambda a: d(a["ast"], a["on_po"], 100), "ast"),
    U("sf_gravity", "Off-ball gravity", TRACKING), U("sf_matchdiff", "Defensive matchup difficulty", MATCHUP),
    U("sf_isodef", "Isolation containment", PLAYTYPE),
    M("sf_tovcreate", "On-ball turnover creation", "/100", "ESTIMATED", "Steals / 100 opponent possessions (on-ball not separable)", lambda a: d(a["stl"], a["on_pd"], 100), "stl"),
    U("sf_screen", "Screen navigation", TRACKING), U("sf_closeout_d", "Closeout effectiveness", TRACKING),
    U("sf_switch", "Defensive switch success", SCHEME), U("sf_recovery", "Perimeter recovery", TRACKING),
    U("sf_help", "Help defense", TRACKING), U("sf_versatility", "Wing defensive versatility", MATCHUP),
    U("sf_supp", "Opponent wing scoring suppression", MATCHUP),
],
"PF": [
    M("pf_stretch", "Stretch-four shooting rate", "%", "OBSERVED", "3PA / FGA", lambda a: d(a["fg3a"], a["fga"], 100), "fga", "pct"),
    U("pf_pop", "Pick-and-pop PPP", PLAYTYPE), U("pf_roll", "Roll-man PPP", PLAYTYPE),
    U("pf_screen_ast", "Screen assists", TRACKING), U("pf_post", "Post-up PPP", PLAYTYPE),
    M("pf_cut", "Cutting PPP", "pts/shot", "ESTIMATED", "FG points per FGA described as cutting", lambda a: d(a["cut_pts"], a["cut_a"]), "cut_a", "mean"),
    M("pf_putback", "Putback PPP", "pts/shot", "DERIVED", "FG points per putback/tip attempt", lambda a: d(a["putback_pts"], a["putback_a"]), "putback_a", "mean"),
    M("pf_rim_xp", "Rim finishing above expected", "pp", "MODELED", "Rim FG% minus expected rim FG%", lambda a: d(a["z_rim_m"] - a["z_rim_x"], a["z_rim_a"], 100), "z_rim_a", "pct"),
    M("pf_orb", "Offensive rebound chances", "%", "ESTIMATED", "On-court OREB% (chance counts need tracking)", lambda a: d(a["oreb"], a["on_oreb"] + a["on_opp_dreb"], 100), "reb_chances_o", "pct"),
    U("pf_mismatch", "Mismatch scoring", MATCHUP), U("pf_switch", "Switch defense", SCHEME),
    U("pf_rim_contests", "Rim contests", TRACKING),
    M("pf_def_rim", "Defensive rim FG%", "%", "DERIVED", "Opponent rim FG% while on court", lambda a: d(a["def_rim_m"], a["def_rim_a"], 100), "def_rim_a", "pct", higher=False),
    U("pf_weakside", "Weak-side blocks", TRACKING), U("pf_help", "Help rotation", TRACKING),
    M("pf_drb", "Defensive rebound conversion", "%", "DERIVED", "On-court DREB%", lambda a: d(a["dreb"], a["on_dreb"] + a["on_opp_oreb"], 100), "reb_chances_d", "pct"),
    U("pf_boxout", "Boxout success", TRACKING), U("pf_pnr_d", "Pick-and-roll defense", SCHEME),
    M("pf_interior", "Interior containment", "pts/100", "DERIVED", "Opponent points within 14 ft per 100 opponent possessions on court", lambda a: d(a["def_paint_pts"], a["on_pd"], 100), "on_pd", higher=False),
    U("pf_smallball", "Small-ball center efficiency", SCHEME),
],
"C": [
    M("c_rim", "Rim FG%", "%", "DERIVED", "Rim FGM / FGA", _rate("z_rim_m", "z_rim_a"), "z_rim_a", "pct"),
    M("c_rim_xp", "Rim FG% above expected", "pp", "MODELED", "Rim FG% minus expected rim FG%", lambda a: d(a["z_rim_m"] - a["z_rim_x"], a["z_rim_a"], 100), "z_rim_a", "pct"),
    M("c_dunk", "Dunk frequency", "%", "DERIVED", "Dunk attempts / FGA", lambda a: d(a["dunk_a"], a["s_n"], 100), "s_n", "pct"),
    U("c_post", "Post-up PPP", PLAYTYPE), U("c_roll", "Roll-man PPP", PLAYTYPE),
    U("c_paint_touch", "Paint touches", TRACKING), U("c_screen_ast", "Screen assists", TRACKING),
    M("c_orb", "Offensive rebound conversion", "%", "DERIVED", "On-court OREB%", lambda a: d(a["oreb"], a["on_oreb"] + a["on_opp_dreb"], 100), "reb_chances_o", "pct"),
    M("c_putback", "Putback PPP", "pts/shot", "DERIVED", "FG points per putback/tip attempt", lambda a: d(a["putback_pts"], a["putback_a"]), "putback_a", "mean"),
    U("c_post_pass", "Post-up passing value", PLAYTYPE),
    M("c_def_rim", "Defensive FG% at rim", "%", "DERIVED", "Opponent rim FG% while on court", lambda a: d(a["def_rim_m"], a["def_rim_a"], 100), "def_rim_a", "pct", higher=False),
    M("c_rim_supp", "Rim FG% suppression", "pp", "MODELED", "Expected minus actual opponent rim FG% while on court", lambda a: d(a["def_rim_x"] - a["def_rim_m"], a["def_rim_a"], 100), "def_rim_a", "pct"),
    M("c_deter", "Rim deterrence", "%", "DERIVED", "Opponent rim attempt share while on court (lower = deterrence)", lambda a: d(a["def_rim_a"], a["def_n"], 100), "def_n", "pct", higher=False),
    M("c_blk_contest", "Blocks per contest", "%", "ESTIMATED", "Blocks on rim attempts / opponent rim attempts on court (contests not tracked)", lambda a: d(a["blk_rim"], a["def_rim_a"], 100), "def_rim_a", "pct"),
    U("c_drop", "Drop coverage PPP allowed", SCHEME), U("c_switch", "Switch coverage PPP allowed", SCHEME),
    U("c_roll_d", "Roll-man defense", PLAYTYPE),
    M("c_drb", "Defensive rebound conversion", "%", "DERIVED", "On-court DREB%", lambda a: d(a["dreb"], a["on_dreb"] + a["on_opp_oreb"], 100), "reb_chances_d", "pct"),
    U("c_contest_foul", "Rim contest foul rate", TRACKING),
    M("c_interior", "Interior defensive impact", "pts/100", "MODELED", "Defensive RAPM", lambda a: a.get("drapm"), kind="model"),
],
}

# --------------------------------------------------------------------------- team (20 x 12 = 240)
def T(mid, label, unit, status, formula, fn, n=None, kind="rate", higher=True):
    return M(mid, label, unit, status, formula, fn, n, kind, higher=higher)


def _net(a):
    return None if not (a["poss"] and a["opp_poss"]) else 100 * (a["pts"] / a["poss"] - a["opp_pts"] / a["opp_poss"])


TEAM = {
"Overall team strength": [
    T("t_ortg", "Offensive rating", "pts/100", "DERIVED", "100 * PTS / reconstructed possessions", lambda a: d(a["pts"], a["poss"], 100), "poss"),
    T("t_drtg", "Defensive rating", "pts/100", "DERIVED", "100 * opponent PTS / opponent possessions", lambda a: d(a["opp_pts"], a["opp_poss"], 100), "opp_poss", higher=False),
    T("t_net", "Net rating", "pts/100", "DERIVED", "ORtg - DRtg", _net, "poss", "mean"),
    T("t_net_adj", "Opponent-adjusted net rating", "pts/100", "MODELED", "Team ridge model (time-decayed), offense + defense coefficients", lambda a: a.get("net_adj"), kind="model"),
    T("t_pace", "Pace", "poss/48", "DERIVED", "Possessions per 48 minutes", lambda a: d(a["poss"] + a["opp_poss"], 2 * a["game_minutes"], 48), "games", "mean"),
    T("t_xwins", "Expected wins", "wins/82", "DERIVED", "82 * Pythagorean win% (exponent 14)", lambda a: None if not a["pts"] else 82 * a["pts"] ** 14 / (a["pts"] ** 14 + a["opp_pts"] ** 14), "games", "mean"),
    T("t_diff", "Point differential", "pts/g", "OBSERVED", "(PTS - opponent PTS) / games", lambda a: d(a["pts"] - a["opp_pts"], a["games"]), "games", "mean"),
    T("t_hc", "Halfcourt efficiency", "pts/100", "ESTIMATED", "Points per 100 non-transition possessions", lambda a: d(a["pts"] - a["trans_pts_poss"], a["poss"] - a["trans_poss"], 100), "poss"),
    T("t_trans_eff", "Transition efficiency", "pts/100", "ESTIMATED", "Points per 100 transition possessions", lambda a: d(a["trans_pts_poss"], a["trans_poss"], 100), "trans_poss"),
    T("t_sos", "Strength of schedule", "pts/100", "MODELED", "Mean opponent-adjusted net rating of opponents faced", lambda a: a.get("sos"), kind="model"),
    T("t_win_pct", "Win %", "%", "OBSERVED", "Wins / games", lambda a: d(a["wins"], a["games"], 100), "games", "pct"),
    T("t_ortg_adj", "Opponent-adjusted offensive rating", "pts/100", "MODELED", "Team ridge offense coefficient + league mean", lambda a: a.get("ortg_adj"), kind="model"),
],
"Shooting efficiency": [
    T("t_ts", "TS%", "%", "DERIVED", "PTS / (2 * (FGA + .44 FTA))", lambda a: d(a["pts"], 2 * (a["fga"] + .44 * a["fta"]), 100), "fga", "pct"),
    T("t_efg", "eFG%", "%", "DERIVED", "(FGM + .5 3PM) / FGA", lambda a: d(a["fgm"] + .5 * a["fg3m"], a["fga"], 100), "fga", "pct"),
    T("t_2p", "2PT%", "%", "OBSERVED", "2PM / 2PA", lambda a: d(a["fgm"] - a["fg3m"], a["fga"] - a["fg3a"], 100), "fg2a", "pct"),
    T("t_3p", "3PT%", "%", "OBSERVED", "3PM / 3PA", lambda a: d(a["fg3m"], a["fg3a"], 100), "fg3a", "pct"),
    T("t_ft", "FT%", "%", "OBSERVED", "FTM / FTA", lambda a: d(a["ftm"], a["fta"], 100), "fta", "pct"),
    T("t_rim", "Rim FG%", "%", "DERIVED", "Rim FGM / FGA", lambda a: d(a["z_rim_m"], a["z_rim_a"], 100), "z_rim_a", "pct"),
    T("t_mid", "Midrange FG%", "%", "DERIVED", "Non-rim 2PM / 2PA", lambda a: d(a["z_sm_m"] + a["z_lm_m"], a["z_sm_a"] + a["z_lm_a"], 100), "z_mid_a", "pct"),
    T("t_c3", "Corner 3PT%", "%", "DERIVED", "Corner 3PM / 3PA", lambda a: d(a["z_c3_m"], a["z_c3_a"], 100), "z_c3_a", "pct"),
    T("t_ab3", "Above-break 3PT%", "%", "DERIVED", "Above-break 3PM / 3PA", lambda a: d(a["z_ab3_m"], a["z_ab3_a"], 100), "z_ab3_a", "pct"),
    T("t_smoe", "Shot making vs expected", "pp", "MODELED", "100 * (FGM - xFGM) / FGA", lambda a: d(a["s_fgm"] - a["s_xfg"], a["s_n"], 100), "s_n", "pct"),
    T("t_sm_fg", "Short midrange FG%", "%", "DERIVED", "4-14 ft FGM / FGA", lambda a: d(a["z_sm_m"], a["z_sm_a"], 100), "z_sm_a", "pct"),
    T("t_lm_fg", "Long midrange FG%", "%", "DERIVED", "14 ft+ 2PM / 2PA", lambda a: d(a["z_lm_m"], a["z_lm_a"], 100), "z_lm_a", "pct"),
],
"Shot selection": [
    T("t_rim_rate", "Rim attempt rate", "%", "DERIVED", "Rim FGA / FGA", lambda a: d(a["z_rim_a"], a["s_n"], 100), "s_n", "pct"),
    T("t_mid_rate", "Midrange attempt rate", "%", "DERIVED", "Non-rim 2PA / FGA", lambda a: d(a["z_sm_a"] + a["z_lm_a"], a["s_n"], 100), "s_n", "pct", higher=False),
    T("t_3pa_rate", "3PA rate", "%", "OBSERVED", "3PA / FGA", lambda a: d(a["fg3a"], a["fga"], 100), "fga", "pct"),
    T("t_xefg", "Expected eFG%", "%", "MODELED", "50 * expected points per shot", lambda a: d(a["s_xpts"], a["s_n"], 50), "s_n", "pct"),
    T("t_xpps", "Expected points / shot", "pts", "MODELED", "sum(xFG * value) / FGA", lambda a: d(a["s_xpts"], a["s_n"]), "s_n", "mean"),
    U("t_open", "Open shot frequency", TRACKING), U("t_wopen", "Wide-open frequency", TRACKING),
    U("t_contested", "Contested frequency", TRACKING),
    T("t_dist", "Shot distance", "ft", "DERIVED", "Mean FGA distance", lambda a: d(a["s_dist"], a["s_n"]), "s_n", "mean"),
    T("t_profile", "Shot profile quality", "pts", "MODELED", "Expected points per shot at league-average shooting (location/type only)", lambda a: d(a["s_xpts"], a["s_n"]), "s_n", "mean"),
    T("t_c3_rate", "Corner three rate", "%", "DERIVED", "Corner 3PA / FGA", lambda a: d(a["z_c3_a"], a["s_n"], 100), "s_n", "pct"),
    T("t_ast_rate", "Assisted FGM rate", "%", "DERIVED", "Assisted FGM / FGM", lambda a: d(a["asd_m"], a["s_fgm"], 100), "s_fgm", "pct"),
],
"Halfcourt offense": [
    T("t_hc_ppp", "Halfcourt PPP", "pts/poss", "ESTIMATED", "Points per non-transition possession", lambda a: d(a["pts"] - a["trans_pts_poss"], a["poss"] - a["trans_poss"]), "poss", "mean"),
    U("t_iso", "Isolation PPP", PLAYTYPE), U("t_pnr_h", "PnR handler PPP", PLAYTYPE), U("t_pnr_r", "PnR roll-man PPP", PLAYTYPE),
    U("t_spot", "Spot-up PPP", PLAYTYPE), U("t_post", "Post-up PPP", PLAYTYPE),
    T("t_cut", "Cutting PPP", "pts/shot", "ESTIMATED", "FG points per FGA described as cutting", lambda a: d(a["cut_pts"], a["cut_a"]), "cut_a", "mean"),
    U("t_offscreen", "Off-screen PPP", PLAYTYPE), U("t_handoff", "Handoff PPP", PLAYTYPE),
    T("t_putback", "Putback PPP", "pts/shot", "DERIVED", "FG points per putback/tip attempt", lambda a: d(a["putback_pts"], a["putback_a"]), "putback_a", "mean"),
    T("t_late", "Late-clock PPP", "pts/shot", "ESTIMATED", "FG points per FGA >= 18 s into possession", lambda a: d(a["late_pts"], a["late_a"]), "late_a", "mean"),
    T("t_pull_eff", "Pull-up eFG%", "%", "ESTIMATED", "eFG% on pull-up/step-back attempts", lambda a: d(a["pull_m"] + a["step_m"] + .5 * (a["pull3_m"]), a["pull_a"] + a["step_a"], 100), "pull_a", "pct"),
],
"Ball movement": [
    T("t_ast_100", "Assists / 100", "ast", "DERIVED", "100 * AST / possessions", lambda a: d(a["ast"], a["poss"], 100), "ast"),
    U("t_pot_ast", "Potential assists", PASSING), U("t_sec_ast", "Secondary assists", PASSING),
    U("t_passes", "Passes / possession", PASSING), U("t_drive_kick", "Drive-and-kick rate", TRACKING),
    U("t_paint_touch", "Paint touches", TRACKING), U("t_xast", "Expected assists", PASSING),
    T("t_ast_conv", "Assist conversion", "%", "DERIVED", "AST / FGM", lambda a: d(a["ast"], a["fgm"], 100), "fgm", "pct"),
    T("t_usg_conc", "Time of possession concentration", "HHI", "ESTIMATED", "Herfindahl index of player usage shares (TOP needs tracking)", lambda a: a.get("usage_hhi"), kind="count", higher=False),
    T("t_ball_eff", "Ball movement efficiency", "ratio", "DERIVED", "AST / TOV", lambda a: d(a["ast"], a["tov"]), "ast"),
    T("t_ast_3", "Assisted 3PM rate", "%", "DERIVED", "Assisted 3PM / 3PM", lambda a: d(a["asd_3m"], a["s_fg3m"], 100), "s_fg3m", "pct"),
    T("t_ast_rim", "Assisted rim FGM rate", "%", "DERIVED", "Assisted rim FGM / rim FGM", lambda a: d(a["asd_rim"], a["z_rim_m"], 100), "z_rim_m", "pct"),
],
"Transition offense": [
    T("t_trans_freq", "Transition frequency", "%", "ESTIMATED", "Transition possessions / possessions (first action <= 8 s after DREB/steal)", lambda a: d(a["trans_poss"], a["poss"], 100), "poss", "pct"),
    T("t_trans_ppp", "Transition PPP", "pts/poss", "ESTIMATED", "Points per transition possession", lambda a: d(a["trans_pts_poss"], a["trans_poss"]), "trans_poss", "mean"),
    T("t_fb_pts", "Fast-break points", "pts/g", "ESTIMATED", "Transition-possession points / games", lambda a: d(a["trans_pts_poss"], a["games"]), "games", "mean"),
    T("t_early_freq", "Early offense frequency", "%", "ESTIMATED", "Possessions with first action <= 12 s / possessions", lambda a: d(a["early_poss"], a["poss"], 100), "poss", "pct"),
    T("t_early_ppp", "Early offense PPP", "pts/poss", "ESTIMATED", "Points per early-offense possession", lambda a: d(a["early_pts"], a["early_poss"]), "early_poss", "mean"),
    T("t_trans_efg", "Transition eFG%", "%", "ESTIMATED", "eFG% on transition FGA", lambda a: d(a["trans_m"] + .5 * a["trans_3m"], a["trans_a"], 100), "trans_a", "pct"),
    T("t_trans_rim", "Transition rim attempts", "/g", "ESTIMATED", "Transition rim FGA / games", lambda a: d(a["trans_rim_a"], a["games"]), "games", "mean"),
    T("t_trans_3", "Transition threes", "/g", "ESTIMATED", "Transition 3PA / games", lambda a: d(a["trans_3a"], a["games"]), "games", "mean"),
    T("t_trans_tov", "Transition turnovers", "%", "ESTIMATED", "Transition possessions ending in turnover / transition possessions", lambda a: d(a["trans_tov"], a["trans_poss"], 100), "trans_poss", "pct", higher=False),
    T("t_trans_ftr", "Transition foul drawing", "FTA/poss", "ESTIMATED", "FTA per transition possession", lambda a: d(a["trans_fta"], a["trans_poss"]), "trans_poss", "mean"),
    T("t_after_stl", "PPP after steal", "pts/poss", "DERIVED", "Points per possession starting from a steal", lambda a: d(a["st_steal_pts"], a["st_steal"]), "st_steal", "mean"),
    T("t_after_dreb", "PPP after defensive rebound", "pts/poss", "DERIVED", "Points per possession starting from a DREB", lambda a: d(a["st_dreb_pts"], a["st_dreb"]), "st_dreb", "mean"),
],
"Offensive rebounding": [
    T("t_orb", "Offensive rebound %", "%", "OBSERVED", "OREB / (OREB + opponent DREB)", lambda a: d(a["oreb"], a["oreb"] + a["opp_dreb"], 100), "reb_o", "pct"),
    U("t_reb_ch", "Rebound chances", TRACKING), U("t_corb", "Contested OREB%", TRACKING),
    T("t_putback_ppp", "Putback PPP", "pts/shot", "DERIVED", "FG points per putback attempt", lambda a: d(a["putback_pts"], a["putback_a"]), "putback_a", "mean"),
    T("t_2c_pts", "Second-chance points", "pts/g", "DERIVED", "Points after an offensive rebound in the same possession / games", lambda a: d(a["sc_pts_poss"], a["games"]), "games", "mean"),
    U("t_crash", "Crash frequency", TRACKING),
    T("t_orb_rim", "Rebounding by shot location: rim misses", "%", "DERIVED", "Share of missed rim FGA followed by own OREB", lambda a: d(a["orb_after_rim"], a["miss_rim"], 100), "miss_rim", "pct"),
    T("t_orb_3", "Rebounding by shot location: 3PT misses", "%", "DERIVED", "Share of missed 3PA followed by own OREB", lambda a: d(a["orb_after_3"], a["miss_3"], 100), "miss_3", "pct"),
    T("t_kickout3", "Kickout threes after OREB", "/g", "DERIVED", "Second-chance 3PA / games", lambda a: d(a["sc_3a"], a["games"]), "games", "mean"),
    U("t_boxout_dis", "Opponent boxout disruption", TRACKING),
    T("t_orb_sd", "Offensive rebounding consistency", "pp SD", "DERIVED", "SD of game OREB%", lambda a: a.get("orb_sd"), kind="count", higher=False),
    T("t_oreb_g", "Offensive rebounds / game", "reb", "OBSERVED", "OREB / games", lambda a: d(a["oreb"], a["games"]), "games", "mean"),
],
"Turnovers and free throws": [
    T("t_tov", "TOV%", "%", "DERIVED", "TOV / possessions", lambda a: d(a["tov"], a["poss"], 100), "poss", "pct", higher=False),
    T("t_live_tov", "Live-ball turnovers", "%", "DERIVED", "Possessions ending in an opponent steal / possessions", lambda a: d(a["end_steal"], a["poss"], 100), "poss", "pct", higher=False),
    T("t_dead_tov", "Dead-ball turnovers", "%", "DERIVED", "Possessions ending in a non-steal turnover / possessions", lambda a: d(a["end_tov"], a["poss"], 100), "poss", "pct", higher=False),
    T("t_pass_tov", "Passing turnovers", "/100", "DERIVED", "Bad-pass turnovers per 100 possessions", lambda a: d(a["tov_bad"], a["poss"], 100), "tov_bad", higher=False),
    T("t_handle_tov", "Ball-handling turnovers", "/100", "DERIVED", "Lost-ball turnovers per 100 possessions", lambda a: d(a["tov_lost"], a["poss"], 100), "tov_lost", higher=False),
    T("t_pts_off_tov_allowed", "Points conceded off turnovers", "pts/g", "DERIVED", "Opponent points on possessions starting after this team's turnover / games", lambda a: d(a["opp_pts_after_tov"], a["games"]), "games", "mean", higher=False),
    T("t_ftr", "Free throw rate", "FTA/FGA", "OBSERVED", "FTA / FGA", lambda a: d(a["fta"], a["fga"]), "fga", "mean"),
    T("t_sfd", "Shooting fouls drawn", "/100", "DERIVED", "Opponent shooting fouls per 100 possessions", lambda a: d(a["sf_drawn"], a["poss"], 100), "sf_drawn"),
    T("t_and1", "And-one frequency", "%", "DERIVED", "Made FGs followed by a single free throw at the same clock / FGM", lambda a: d(a["and1"], a["fgm"], 100), "fgm", "pct"),
    T("t_tov_adj", "Turnover-adjusted efficiency", "pts/100", "DERIVED", "Points per 100 shooting possessions (excluding turnovers)", lambda a: d(a["pts"], a["poss"] - a["tov"], 100), "poss"),
    T("t_ftm_g", "Free throws made / game", "ftm", "OBSERVED", "FTM / games", lambda a: d(a["ftm"], a["games"]), "games", "mean"),
    T("t_tov_g", "Turnovers / game", "tov", "OBSERVED", "TOV / games", lambda a: d(a["tov"], a["games"]), "games", "mean", higher=False),
],
"Overall defense": [
    T("t_drtg2", "Defensive rating", "pts/100", "DERIVED", "100 * opponent PTS / opponent possessions", lambda a: d(a["opp_pts"], a["opp_poss"], 100), "opp_poss", higher=False),
    T("t_drtg_adj", "Adjusted defensive rating", "pts/100", "MODELED", "Team ridge defense coefficient + league mean", lambda a: a.get("drtg_adj"), kind="model", higher=False),
    T("t_opp_efg", "Opponent eFG%", "%", "DERIVED", "Opponent (FGM + .5 3PM) / FGA", lambda a: d(a["opp_fgm"] + .5 * a["opp_fg3m"], a["opp_fga"], 100), "opp_fga", "pct", higher=False),
    T("t_opp_ts", "Opponent TS%", "%", "DERIVED", "Opponent PTS / (2 (FGA + .44 FTA))", lambda a: d(a["opp_pts"], 2 * (a["opp_fga"] + .44 * a["opp_fta"]), 100), "opp_fga", "pct", higher=False),
    T("t_forced_tov", "Turnovers forced", "%", "DERIVED", "Opponent TOV / opponent possessions", lambda a: d(a["opp_tov"], a["opp_poss"], 100), "opp_poss", "pct"),
    T("t_opp_orb", "Opponent offensive rebounds", "%", "DERIVED", "Opponent OREB / (opponent OREB + DREB)", lambda a: d(a["opp_oreb"], a["opp_oreb"] + a["dreb"], 100), "reb_d", "pct", higher=False),
    T("t_opp_ftr", "Opponent free throw rate", "FTA/FGA", "OBSERVED", "Opponent FTA / FGA", lambda a: d(a["opp_fta"], a["opp_fga"]), "opp_fga", "mean", higher=False),
    T("t_hc_def", "Halfcourt defense", "pts/100", "ESTIMATED", "Opponent points per 100 non-transition possessions", lambda a: d(a["opp_pts"] - a["opp_trans_pts"], a["opp_poss"] - a["opp_trans_poss"], 100), "opp_poss", higher=False),
    T("t_trans_def", "Transition defense", "pts/100", "ESTIMATED", "Opponent points per 100 transition possessions", lambda a: d(a["opp_trans_pts"], a["opp_trans_poss"], 100), "opp_trans_poss", higher=False),
    T("t_opp_xpps", "Opponent shot quality", "pts", "MODELED", "Opponent expected points per shot", lambda a: d(a["opp_s_xpts"], a["opp_s_n"]), "opp_s_n", "mean", higher=False),
    T("t_opp_smoe", "Opponent shot making vs expected", "pp", "MODELED", "100 * (opp FGM - opp xFGM) / opp FGA", lambda a: d(a["opp_s_fgm"] - a["opp_s_xfg"], a["opp_s_n"], 100), "opp_s_n", "pct", higher=False),
    T("t_opp_pts_g", "Opponent points / game", "pts", "OBSERVED", "Opponent PTS / games", lambda a: d(a["opp_pts"], a["games"]), "games", "mean", higher=False),
],
"Perimeter defense": [
    T("t_opp_3par", "Opponent 3PA rate", "%", "OBSERVED", "Opponent 3PA / FGA", lambda a: d(a["opp_fg3a"], a["opp_fga"], 100), "opp_fga", "pct", higher=False),
    T("t_opp_3p", "Opponent 3PT%", "%", "OBSERVED", "Opponent 3PM / 3PA", lambda a: d(a["opp_fg3m"], a["opp_fg3a"], 100), "opp_fg3a", "pct", higher=False),
    T("t_opp_c3r", "Opponent corner three rate", "%", "DERIVED", "Opponent corner 3PA / FGA", lambda a: d(a["opp_z_c3_a"], a["opp_s_n"], 100), "opp_s_n", "pct", higher=False),
    T("t_opp_cs", "Opponent catch-and-shoot efficiency", "eFG%", "ESTIMATED", "Opponent eFG% on plain jump shots (proxy)", lambda a: d(a["opp_cs_m"] + .5 * a["opp_cs3_m"], a["opp_cs_a"], 100), "opp_cs_a", "pct", higher=False),
    T("t_opp_pu", "Opponent pull-up efficiency", "eFG%", "ESTIMATED", "Opponent eFG% on pull-up/step-back attempts", lambda a: d(a["opp_pull_m"] + .5 * a["opp_pull3_m"], a["opp_pull_a"], 100), "opp_pull_a", "pct", higher=False),
    U("t_open3", "Open threes allowed", TRACKING), U("t_wopen3", "Wide-open threes allowed", TRACKING),
    U("t_contests", "Perimeter contests", TRACKING), U("t_closeout", "Closeout quality", TRACKING),
    U("t_opp_drive", "Opponent drive penetration", TRACKING),
    T("t_opp_c3p", "Opponent corner 3PT%", "%", "DERIVED", "Opponent corner 3PM / 3PA", lambda a: d(a["opp_z_c3_m"], a["opp_z_c3_a"], 100), "opp_z_c3_a", "pct", higher=False),
    T("t_opp_ab3p", "Opponent above-break 3PT%", "%", "DERIVED", "Opponent above-break 3PM / 3PA", lambda a: d(a["opp_z_ab3_m"], a["opp_z_ab3_a"], 100), "opp_z_ab3_a", "pct", higher=False),
],
"Interior defense": [
    T("t_opp_rimr", "Opponent rim attempt rate", "%", "DERIVED", "Opponent rim FGA / FGA", lambda a: d(a["opp_z_rim_a"], a["opp_s_n"], 100), "opp_s_n", "pct", higher=False),
    T("t_opp_rim", "Opponent rim FG%", "%", "DERIVED", "Opponent rim FGM / FGA", lambda a: d(a["opp_z_rim_m"], a["opp_z_rim_a"], 100), "opp_z_rim_a", "pct", higher=False),
    T("t_rim_supp", "Rim suppression above expected", "pp", "MODELED", "Expected - actual opponent rim FG%", lambda a: d(a["opp_z_rim_x"] - a["opp_z_rim_m"], a["opp_z_rim_a"], 100), "opp_z_rim_a", "pct"),
    T("t_opp_paint", "Paint points allowed", "pts/g", "ESTIMATED", "Opponent FG points within 14 ft / games", lambda a: d(a["opp_paint_pts"], a["games"]), "games", "mean", higher=False),
    T("t_blk_100", "Blocks / 100", "blk", "DERIVED", "100 * BLK / opponent possessions", lambda a: d(a["blk"], a["opp_poss"], 100), "blk"),
    U("t_rim_contests", "Rim contests", TRACKING),
    T("t_rim_deter", "Rim deterrence", "pp", "DERIVED", "League opponent rim share minus this team's opponent rim share", lambda a: a.get("rim_deter"), kind="count"),
    U("t_rollman_d", "Roll-man defense", PLAYTYPE), U("t_post_d", "Post-up defense", PLAYTYPE),
    U("t_help", "Help rotation", TRACKING),
    T("t_blk_rate", "Block rate on opponent 2PA", "%", "DERIVED", "Blocks / opponent 2PA", lambda a: d(a["blk"], a["opp_fga"] - a["opp_fg3a"], 100), "opp_fga", "pct"),
    T("t_opp_mid", "Opponent midrange FG%", "%", "DERIVED", "Opponent non-rim 2PM / 2PA", lambda a: d(a["opp_z_sm_m"] + a["opp_z_lm_m"], a["opp_z_sm_a"] + a["opp_z_lm_a"], 100), "opp_s_n", "pct", higher=False),
],
"Defensive scheme": [U(f"t_scheme_{i}", label, SCHEME if i < 6 or i == 9 else PLAYTYPE) for i, label in enumerate([
    "Drop coverage frequency", "Switch frequency", "Hedge frequency", "Blitz frequency", "Zone frequency",
    "Man-to-man frequency", "PnR defense", "Isolation defense", "Post-up defense", "Scheme adaptability"])] + [
    T("t_def_foul", "Defensive foul rate", "/100", "DERIVED", "Personal fouls per 100 opponent possessions", lambda a: d(a["pf"], a["opp_poss"], 100), "pf", higher=False),
    T("t_stl_100", "Steals / 100", "stl", "DERIVED", "100 * STL / opponent possessions", lambda a: d(a["stl"], a["opp_poss"], 100), "stl"),
],
"Defensive rebounding": [
    T("t_drb", "Defensive rebound %", "%", "OBSERVED", "DREB / (DREB + opponent OREB)", lambda a: d(a["dreb"], a["dreb"] + a["opp_oreb"], 100), "reb_d", "pct"),
    U("t_dreb_ch", "Rebound chances", TRACKING), U("t_cdreb", "Contested defensive rebounds", TRACKING),
    U("t_boxouts", "Boxouts", TRACKING),
    T("t_opp_2c", "Opponent second-chance points", "pts/g", "DERIVED", "Opponent points after their OREB / games", lambda a: d(a["opp_sc_pts_poss"], a["games"]), "games", "mean", higher=False),
    T("t_opp_putback", "Opponent putback PPP", "pts/shot", "DERIVED", "Opponent FG points per putback attempt", lambda a: d(a["opp_putback_pts"], a["opp_putback_a"]), "opp_putback_a", "mean", higher=False),
    U("t_long_reb", "Long rebounds secured", TRACKING),
    T("t_drb_3", "DREB after threes", "%", "DERIVED", "Share of opponent missed 3PA ending in this team's DREB", lambda a: d(a["drb_after_opp3"], a["opp_miss_3"], 100), "opp_miss_3", "pct"),
    T("t_drb_2", "DREB after twos", "%", "DERIVED", "Share of opponent missed 2PA ending in this team's DREB", lambda a: d(a["drb_after_opp2"], a["opp_miss_2"], 100), "opp_miss_2", "pct"),
    T("t_poss_complete", "Possession completion", "%", "DERIVED", "Opponent possessions with zero offensive rebounds / opponent possessions", lambda a: d(a["opp_poss_no_oreb"], a["opp_poss"], 100), "opp_poss", "pct"),
    T("t_dreb_g", "Defensive rebounds / game", "reb", "OBSERVED", "DREB / games", lambda a: d(a["dreb"], a["games"]), "games", "mean"),
    T("t_opp_orb_g", "Opponent offensive rebounds / game", "reb", "OBSERVED", "Opponent OREB / games", lambda a: d(a["opp_oreb"], a["games"]), "games", "mean", higher=False),
],
"Five-man lineups": [
    T("t_lu_start", "Starting lineup net rating", "pts/100", "DERIVED", "Net rating of the most-used starting five", lambda a: a.get("lu_start_net"), kind="count"),
    T("t_lu_best", "Best lineup adjusted net rating", "pts/100", "MODELED", "Max shrunk lineup net (>= 100 possessions)", lambda a: a.get("lu_best"), kind="model"),
    T("t_lu_worst", "Worst lineup adjusted net rating", "pts/100", "MODELED", "Min shrunk lineup net (>= 100 possessions)", lambda a: a.get("lu_worst"), kind="model"),
    T("t_lu_bench", "Bench lineup net rating", "pts/100", "DERIVED", "Net rating in stints with <= 1 regular starter", lambda a: a.get("lu_bench_net"), kind="count"),
    T("t_lu_small", "Small-ball lineup net rating", "pts/100", "ESTIMATED", "Net rating in stints with no listed center", lambda a: a.get("lu_small_net"), kind="count"),
    T("t_lu_big", "Double-big lineup net rating", "pts/100", "ESTIMATED", "Net rating in stints with >= 2 listed centers or C + PF", lambda a: a.get("lu_big_net"), kind="count"),
    T("t_lu_ortg", "Lineup offensive rating", "pts/100", "DERIVED", "Possession-weighted ORtg of top-5 lineups by minutes", lambda a: a.get("lu_top_ortg"), kind="count"),
    T("t_lu_drtg", "Lineup defensive rating", "pts/100", "DERIVED", "Possession-weighted DRtg of top-5 lineups", lambda a: a.get("lu_top_drtg"), kind="count", higher=False),
    T("t_lu_reb", "Lineup rebound rate", "%", "DERIVED", "Rebound % of top-5 lineups", lambda a: a.get("lu_top_reb"), kind="count"),
    T("t_lu_chem", "Lineup chemistry estimate", "pts/100", "MODELED", "Minutes-weighted shrunk lineup interaction (lineup net beyond player RAPM sum)", lambda a: a.get("lu_chem"), kind="model"),
    T("t_lu_count", "Distinct lineups used", "lineups", "DERIVED", "Five-man units with >= 1 minute", lambda a: a.get("lu_count"), kind="count", higher=False),
    T("t_lu_top_share", "Top lineup minute share", "%", "DERIVED", "Most-used lineup minutes / total minutes", lambda a: a.get("lu_top_share"), kind="count"),
],
"Player dependency": [
    T("t_dep_star", "Star on/off net rating", "pts/100", "DERIVED", "On-off net differential of the team's top-RAPM player", lambda a: a.get("dep_star"), kind="count"),
    T("t_dep_creator", "Creator on/off offense", "pts/100", "DERIVED", "On-off ORtg of the top assist-rate player", lambda a: a.get("dep_creator"), kind="count"),
    T("t_dep_rim", "Rim protector on/off defense", "pts/100", "DERIVED", "Off-on DRtg of the top blocks/100 player (positive = better with him)", lambda a: a.get("dep_rim"), kind="count"),
    T("t_dep_shooter", "Shooter on/off spacing", "pp", "DERIVED", "On-off team 3PA rate for the top-volume shooter", lambda a: a.get("dep_shooter"), kind="count"),
    T("t_dep_stopper", "Defensive stopper on/off", "pts/100", "DERIVED", "Off-on DRtg of the top DRAPM player", lambda a: a.get("dep_stopper"), kind="count"),
    T("t_dep_usage", "Usage redistribution", "pp", "MODELED", "Mean usage-rate increase (pp) for the next four usage players if the top-usage player sits; proportional model u_i/(1-u_top)", lambda a: a.get("dep_usage"), kind="model"),
    T("t_dep_ast", "Assist redistribution", "pp", "DERIVED", "Top assist share (player AST / team AST)", lambda a: a.get("dep_ast"), kind="count"),
    T("t_dep_repl", "Replacement-level lineup quality", "pts/100", "MODELED", "Sum of RAPM of players 6-10 by minutes", lambda a: a.get("dep_repl"), kind="model"),
    T("t_dep_inj", "Injury-adjusted team rating", "pts/100", "MODELED", "Minutes-weighted RAPM of currently available rotation (prior-game participation)", lambda a: a.get("dep_inj"), kind="model"),
    T("t_dep_cont", "Rotation continuity", "%", "DERIVED", "Share of minutes by players who played in the first 10 games of the window", lambda a: a.get("dep_cont"), kind="count"),
    T("t_dep_top3", "Top-3 minutes concentration", "%", "DERIVED", "Minutes share of top 3 players", lambda a: a.get("dep_top3"), kind="count"),
    T("t_dep_top_usg", "Top usage share", "%", "DERIVED", "Highest player usage rate", lambda a: a.get("dep_top_usg"), kind="count"),
],
"Coaching and rotations": [
    T("t_rot_len", "Rotation length", "players", "DERIVED", "Mean players with >= 10 minutes per game", lambda a: a.get("rot_len"), kind="count"),
    T("t_start_share", "Starter minutes share", "%", "OBSERVED", "Starter minutes / total minutes", lambda a: d(a["starter_min"], a["team_min"], 100), "games", "pct"),
    T("t_bench_share", "Bench minutes share", "%", "OBSERVED", "Bench minutes / total minutes", lambda a: d(a["team_min"] - a["starter_min"], a["team_min"], 100), "games", "pct"),
    T("t_sub_timing", "Substitution timing", "min", "DERIVED", "Mean game minute of the first substitution", lambda a: a.get("first_sub_min"), kind="count"),
    T("t_timeouts", "Timeout frequency", "/g", "DERIVED", "Team timeouts / games", lambda a: d(a["timeouts"], a["games"]), "games", "mean"),
    U("t_ato_off", "After-timeout offense", PLAYTYPE), U("t_ato_def", "After-timeout defense", PLAYTYPE),
    T("t_late_lu", "Late-game lineup performance", "pts/100", "DERIVED", "Net rating in clutch possessions", lambda a: None if not (a["cl_poss"] and a["cl_opp_poss"]) else 100 * (a["cl_pts"] / a["cl_poss"] - a["cl_opp_pts"] / a["cl_opp_poss"]), "cl_poss", "mean"),
    U("t_scheme_adj", "Scheme adjustments", SCHEME),
    T("t_rot_net", "Rotation-adjusted net rating", "pts/100", "MODELED", "Minutes-weighted RAPM sum of the rotation", lambda a: a.get("rot_rapm"), kind="model"),
    T("t_subs_g", "Substitutions / game", "subs", "DERIVED", "Team substitutions / games", lambda a: d(a["subs"], a["games"]), "games", "mean"),
    T("t_players_used", "Players used", "players", "OBSERVED", "Distinct players with minutes", lambda a: a.get("players_used"), kind="count"),
],
"Situational performance": [
    *[T(f"t_q{q}", f"Q{q} net rating", "pts/100", "DERIVED", f"Net rating in period {q}", (lambda q: lambda a: None if not (a[f"q{q}_poss"] and a[f"q{q}_opp_poss"]) else 100 * (a[f"q{q}_pts"] / a[f"q{q}_poss"] - a[f"q{q}_opp_pts"] / a[f"q{q}_opp_poss"]))(q), f"q{q}_poss", "mean") for q in (1, 2, 3, 4)],
    T("t_cl_ortg", "Clutch offensive rating", "pts/100", "DERIVED", "Points per 100 clutch possessions (last 5:00 of Q4/OT, margin <= 5)", lambda a: d(a["cl_pts"], a["cl_poss"], 100), "cl_poss"),
    T("t_cl_drtg", "Clutch defensive rating", "pts/100", "DERIVED", "Opponent points per 100 clutch possessions", lambda a: d(a["cl_opp_pts"], a["cl_opp_poss"], 100), "cl_opp_poss", higher=False),
    T("t_cl_tov", "Clutch turnover rate", "%", "DERIVED", "Clutch turnovers / clutch possessions", lambda a: d(a["cl_tov"], a["cl_poss"], 100), "cl_poss", "pct", higher=False),
    T("t_cl_xpps", "Clutch shot quality", "pts", "MODELED", "Expected points per clutch shot", lambda a: d(a["cl_xpts"], a["cl_shots"]), "cl_shots", "mean"),
    T("t_cl_ft", "Clutch FT%", "%", "DERIVED", "Clutch FTM / FTA", lambda a: d(a["cl_ftm"], a["cl_fta"], 100), "cl_fta", "pct"),
    T("t_lead_eff", "Leading-state efficiency", "pts/100", "DERIVED", "ORtg on possessions starting with a lead", lambda a: d(a["lead_pts"], a["lead_poss"], 100), "lead_poss"),
    T("t_trail_eff", "Trailing-state efficiency", "pts/100", "DERIVED", "ORtg on possessions starting behind", lambda a: d(a["trail_pts"], a["trail_poss"], 100), "trail_poss"),
    T("t_close", "Close-game performance", "%", "OBSERVED", "Win % in games decided by <= 5", lambda a: d(a["close_w"], a["close_g"], 100), "close_g", "pct"),
],
"Schedule and fatigue": [
    T("t_home_net", "Home net rating", "pts/100", "DERIVED", "Net rating at home", lambda a: None if not (a["h_poss"] and a["h_opp_poss"]) else 100 * (a["h_pts"] / a["h_poss"] - a["h_opp_pts"] / a["h_opp_poss"]), "h_poss", "mean"),
    T("t_away_net", "Away net rating", "pts/100", "DERIVED", "Net rating on the road", lambda a: None if not (a["a_poss"] and a["a_opp_poss"]) else 100 * (a["a_pts"] / a["a_poss"] - a["a_opp_pts"] / a["a_opp_poss"]), "a_poss", "mean"),
    T("t_b2b_net", "Back-to-back net rating", "pts/100", "DERIVED", "Net rating on the second night of back-to-backs", lambda a: None if not (a["b2b_poss"] and a["b2b_opp_poss"]) else 100 * (a["b2b_pts"] / a["b2b_poss"] - a["b2b_opp_pts"] / a["b2b_opp_poss"]), "b2b_poss", "mean"),
    T("t_rest_net", "Rest-adjusted net rating", "pts/100", "DERIVED", "Net rating with >= 2 days of rest", lambda a: None if not (a["rest_poss"] and a["rest_opp_poss"]) else 100 * (a["rest_pts"] / a["rest_poss"] - a["rest_opp_pts"] / a["rest_opp_poss"]), "rest_poss", "mean"),
    T("t_3in4", "Three-in-four-days performance", "pts/g", "DERIVED", "Point differential in the third game within four days", lambda a: d(a["t34_diff"], a["t34_g"]), "t34_g", "mean"),
    T("t_travel", "Travel adjustment", "miles/g", "DERIVED", "Mean great-circle miles traveled into each game (arena reference coordinates)", lambda a: d(a["travel_miles"], a["games"]), "games", "mean", higher=False),
    T("t_altitude", "Altitude adjustment", "pts/g", "DERIVED", "Point differential in games at >= 4,000 ft venues (DEN, UTA)", lambda a: d(a["alt_diff"], a["alt_g"]), "alt_g", "mean"),
    T("t_tz", "Time-zone adjustment", "pts/g", "DERIVED", "Point differential in games after crossing >= 2 time zones", lambda a: d(a["tz_diff"], a["tz_g"]), "tz_g", "mean"),
    T("t_workload", "Accumulated player workload", "min", "DERIVED", "Top-5 players' minutes over the last 7 days of the window", lambda a: a.get("workload"), kind="count", higher=False),
    T("t_fatigue_net", "Fatigue-adjusted team rating", "pts/100", "MODELED", "Adjusted net minus league back-to-back penalty share", lambda a: a.get("fatigue_net"), kind="model"),
    T("t_b2b_g", "Back-to-back games", "games", "DERIVED", "Second nights of back-to-backs", lambda a: a["b2b_g"], kind="count"),
    T("t_rest_days", "Average rest days", "days", "DERIVED", "Mean days since the previous game", lambda a: d(a["rest_days_sum"], a["rest_days_n"]), "rest_days_n", "mean"),
],
"Trends and regression": [
    T("t_l5", "Last 5 games net rating", "pts/100", "DERIVED", "Net rating over the last 5 games", lambda a: a.get("l5_net"), kind="count"),
    T("t_l10", "Last 10 games net rating", "pts/100", "DERIVED", "Net rating over the last 10 games", lambda a: a.get("l10_net"), kind="count"),
    T("t_l20", "Last 20 games net rating", "pts/100", "DERIVED", "Net rating over the last 20 games", lambda a: a.get("l20_net"), kind="count"),
    T("t_roll_o", "Rolling offensive rating", "pts/100", "DERIVED", "10-game rolling ORtg", lambda a: a.get("l10_ortg"), kind="count"),
    T("t_roll_d", "Rolling defensive rating", "pts/100", "DERIVED", "10-game rolling DRtg", lambda a: a.get("l10_drtg"), kind="count", higher=False),
    T("t_roll_pace", "Rolling pace", "poss/48", "DERIVED", "10-game rolling pace", lambda a: a.get("l10_pace"), kind="count"),
    T("t_sq_trend", "Shot quality trend", "pts", "MODELED", "Last-10 minus window expected points per shot", lambda a: a.get("sq_trend"), kind="model"),
    T("t_tov_var", "Turnover variance", "pp SD", "DERIVED", "SD of game TOV%", lambda a: a.get("tov_sd"), kind="count", higher=False),
    T("t_3p_vs_x", "3PT shooting vs expected", "pp", "MODELED", "3PT% minus expected 3PT% (xFG)", lambda a: d(a["s_fg3m"] - a["s_x3"], a["s_fg3a"], 100), "s_fg3a", "pct"),
    T("t_regress", "Projected regression", "pts/100", "MODELED", "Adjusted net minus raw net (shrinkage toward schedule-adjusted strength)", lambda a: None if a.get("net_adj") is None or _net(a) is None else a["net_adj"] - _net(a), kind="model"),
    T("t_opp_3p_vs_x", "Opponent 3PT vs expected", "pp", "MODELED", "Opponent 3PT% minus expected", lambda a: d(a["opp_s_fg3m"] - a["opp_s_x3"], a["opp_s_fg3a"], 100), "opp_s_fg3a", "pct", higher=False),
    T("t_l10_w", "Last 10 record", "wins", "OBSERVED", "Wins in last 10 games", lambda a: a.get("l10_w"), kind="count"),
],
"Matchup intelligence": [
    U("t_vs_drop", "Offense vs drop", SCHEME), U("t_vs_switch", "Offense vs switch", SCHEME),
    U("t_vs_zone", "Offense vs zone", SCHEME), U("t_vs_blitz", "Offense vs blitz", SCHEME),
    U("t_d_iso", "Defense vs isolation", PLAYTYPE), U("t_d_pnr", "Defense vs pick-and-roll", PLAYTYPE),
    U("t_d_post", "Defense vs post-ups", PLAYTYPE),
    T("t_d_trans", "Defense vs transition", "pts/100", "ESTIMATED", "Opponent transition points per 100 opponent possessions", lambda a: d(a["opp_trans_pts"], a["opp_poss"], 100), "opp_poss", higher=False),
    T("t_reb_match", "Rebounding matchup", "pp", "DERIVED", "OREB% + DREB% - 100 (combined rebounding margin)", lambda a: None if not (a["oreb"] + a["opp_dreb"] and a["dreb"] + a["opp_oreb"]) else 100 * (a["oreb"] / (a["oreb"] + a["opp_dreb"]) + a["dreb"] / (a["dreb"] + a["opp_oreb"])) - 100, "reb_o", "mean"),
    T("t_lu_mismatch", "Lineup mismatch advantage", "pts/100", "MODELED", "Minutes-weighted RAPM sum of rotation vs league average rotation", lambda a: a.get("rot_rapm"), kind="model"),
    T("t_vs_good", "Net vs top-10 opponents", "pts/g", "DERIVED", "Point differential vs teams in the top 10 by adjusted net", lambda a: d(a["vs_top_diff"], a["vs_top_g"]), "vs_top_g", "mean"),
    T("t_vs_bad", "Net vs bottom-10 opponents", "pts/g", "DERIVED", "Point differential vs bottom-10 teams", lambda a: d(a["vs_bot_diff"], a["vs_bot_g"]), "vs_bot_g", "mean"),
],
}

ARCHETYPES = ["Primary creator", "Secondary creator", "Scoring guard", "Movement shooter", "Catch-and-shoot specialist",
              "Slashing wing", "3-and-D wing", "Defensive stopper", "Stretch big", "Roll man", "Post-up creator",
              "Rim protector", "Rebounding specialist", "Point center"]


def all_definitions():
    out = []
    for scope, cats in (("player", PLAYER), *((p, {f"{p} specialization": POSITION[p]}) for p in POSITION), ("team", TEAM)):
        for cat, metrics in cats.items():
            for m in metrics:
                out.append({"scope": scope, "category": cat, **{k: v for k, v in m.items() if k != "fn"}})
    return out


def counts():
    defs = all_definitions()
    by = {}
    for m in defs:
        s = by.setdefault(m["scope"], {"total": 0})
        s["total"] += 1
        s[m["status"]] = s.get(m["status"], 0) + 1
    return by
