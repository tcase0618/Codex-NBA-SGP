"""Lineup, stint, possession and shot reconstruction from play-by-play.

Reconstruction rules (documented and tested):
* Period-1 lineups are the box-score starters. Later period lineups are inferred:
  a player whose first event in the period is anything but entering as a
  substitute was on court at the start. Missing slots are filled from the
  previous period's closing lineup. Games whose lineups cannot be kept at five
  are flagged, never silently repaired.
* Offensive ownership: FG attempts, non-technical free throws, turnovers and
  offensive rebounds belong to the event team; a defensive rebound starts a
  possession for the rebounding team. A possession is a maximal run of events
  owned by the same team inside one period.
* Technical free-throw points are counted outside possessions so that
  possession points + technical points reconcile with the final score.
* "Transition" is an ESTIMATE: first offensive action within 8 seconds of a
  live-ball start (defensive rebound or steal). ESPN does not publish a
  transition flag or shot clock.
"""
import re
from collections import defaultdict

from .parse import shot_family, shot_geometry

TURNOVER_EXTRA = {"traveling", "offensive charge", "shot clock turnover", "3-second turnover",
                  "double dribble turnover", "discontinued dribble turnover", "kicked ball turnover",
                  "lane violation turnover", "palming turnover", "inbound turnover", "backcourt turnover",
                  "8-second turnover", "5-second turnover", "basket from below turnover",
                  "offensive goaltending turnover", "illegal screen turnover", "jump ball violation turnover",
                  "excess timeout turnover", "too many players turnover", "illegal assist turnover",
                  "opposite basket turnover", "punched ball turnover", "player out of bounds violation turnover",
                  "swinging elbows turnover", "step out of bounds turnover", "out of bounds lost ball turnover"}
FT_PATTERN = re.compile(r"(\d+) of (\d+)")
TRANSITION_SECONDS = 8


def kind(play):
    t = play["type"].lower()
    if t == "substitution":
        return "sub"
    if t.startswith("free throw"):
        return "ft"
    if play["shooting"]:
        return "fga"
    if t == "offensive rebound":
        return "oreb"
    if t == "defensive rebound":
        return "dreb"
    if "turnover" in t or t in TURNOVER_EXTRA:
        # "Offensive Foul" alone is a foul; the paired "... Turnover" row counts.
        return "tov"
    if "foul" in t or "flagrant" in t or "technical" in t:
        return "foul"
    if t in ("end period", "end game"):
        return "end"
    return "other"


def ft_info(play):
    t = play["type"].lower()
    technical = "technical" in t
    m = FT_PATTERN.search(play["type"]) or FT_PATTERN.search(play["text"])
    n, total = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
    made = play["scoring"] or " makes " in f" {play['text'].lower()} "
    return {"technical": technical, "last": n == total, "made": bool(made), "flagrant": "flagrant" in t}


def _period_starters(period_plays, team_players, previous):
    """Infer the five players on court at a period start for each team.

    Evidence uses elapsed time, not row order, because ESPN often lists a
    substitution after an action recorded at the same clock. A player is on court
    at the start when they are substituted out, or act, strictly before their
    first substitution in. Technical fouls are ignored (bench players can draw
    them). Ties are broken by preferring sub-out evidence, then earliest action.
    """
    first_in, first_out, first_evt = {}, {}, {}
    for p in period_plays:
        k = kind(p)
        at = p["elapsed"]
        if k == "sub":
            if p["p1"]:
                first_in.setdefault(p["p1"], at)
            if p["p2"]:
                first_out.setdefault(p["p2"], at)
        elif k in ("fga", "ft", "oreb", "dreb", "tov", "foul") and "technical" not in p["type"].lower():
            for pid in (p["p1"], p["p2"], p["p3"]):
                if pid:
                    first_evt.setdefault(pid, at)
    starters, flags = {}, []
    for team_id, roster in team_players.items():
        evidence = []
        for pid in roster:
            tin = first_in.get(pid)
            tout, tev = first_out.get(pid), first_evt.get(pid)
            if tout is not None and (tin is None or tout <= tin) and not (tin is not None and tin == tout and tev is None):
                evidence.append((0, tout, pid))
            elif tev is not None and (tin is None or tev < tin):
                evidence.append((1, tev, pid))
        evidence.sort()
        on = [pid for _, _, pid in evidence]
        if len(on) > 5:
            flags.append(f"period_start_overflow:{team_id}")
            on = on[:5]
        if len(on) < 5:
            # Players with no event and no substitution played the full period.
            for pid in previous.get(team_id, []):
                if pid not in on and pid not in first_in:
                    on.append(pid)
                if len(on) == 5:
                    break
        if len(on) < 5:
            flags.append(f"period_start_underflow:{team_id}")
        starters[team_id] = on
    return starters, flags


def reconstruct(parsed):
    g = parsed["game"]
    home, away = g["home_id"], g["away_id"]
    plays = parsed["plays"]
    team_of = {b["player_id"]: b["team_id"] for b in parsed["boxes"]}
    team_players = {home: {p for p, t in team_of.items() if t == home},
                    away: {p for p, t in team_of.items() if t == away}}
    other = {home: away, away: home}
    flags = []
    timeline = defaultdict(list)  # pid -> [(seq, role)] for missing-substitution repair
    for p in plays:
        if kind(p) == "sub":
            if p["p1"]:
                timeline[p["p1"]].append((p["seq"], "in"))
            if p["p2"]:
                timeline[p["p2"]].append((p["seq"], "out"))
        else:
            for pid in (p["p1"], p["p2"], p["p3"]):
                if pid:
                    timeline[pid].append((p["seq"], "event"))
    repairs = []

    def silent_leaver(team_id, seq, exclude):
        """Lineup player most likely to have left without a recorded substitution:
        whose next involvement in this period is entering the game, else the one
        with the longest gap until any involvement."""
        best, best_key = None, None
        for pid in lineup[team_id]:
            if pid == exclude:
                continue
            nxt = next(((s, r) for s, r in timeline[pid] if s > seq), None)
            key = (0 if nxt and nxt[1] == "in" else 1, nxt[0] if nxt else 10**9)
            rank = (key[0] == 0, key[1])
            if best_key is None or rank > best_key:
                best, best_key = pid, rank
        return best

    def repair(team_id, pid, at, seq, reason):
        out = silent_leaver(team_id, seq, pid)
        if out is None:
            return False
        close_stint(at)
        lineup[team_id] = [pid if x == out else x for x in lineup[team_id]]
        open_stint(at)
        repairs.append({"seq": seq, "team_id": team_id, "in": pid, "out": out, "reason": reason})
        return True
    if not plays:
        return {"stints": [], "possessions": [], "shots": [], "player_onoff": {}, "events": [],
                "quality": {"pbp_available": False, "flags": ["no_play_by_play"], "valid": False}}

    periods = sorted({p["period"] for p in plays})
    by_period = defaultdict(list)
    for p in plays:
        by_period[p["period"]].append(p)
    lineup = {home: [b["player_id"] for b in parsed["boxes"] if b["team_id"] == home and b["starter"]],
              away: [b["player_id"] for b in parsed["boxes"] if b["team_id"] == away and b["starter"]]}
    stints, possessions, shots, events = [], [], [], []
    onoff = defaultdict(lambda: defaultdict(float))
    invalid_events = 0
    technical_points = {home: 0, away: 0}
    poss = None
    last_poss_end = {"elapsed": 0.0, "start_type": "period_start"}

    def lineup_key():
        return (tuple(sorted(lineup[home])), tuple(sorted(lineup[away])))

    stint = None

    def open_stint(at):
        nonlocal stint
        stint = {"game_id": g["id"], "stint_index": len(stints), "period": cur_period, "start": at, "end": at,
                 "home_lineup": sorted(lineup[home]), "away_lineup": sorted(lineup[away]),
                 "home_pts": 0, "away_pts": 0, "home_poss": 0, "away_poss": 0,
                 "home_fga": 0, "away_fga": 0, "home_oreb": 0, "away_oreb": 0, "home_dreb": 0, "away_dreb": 0,
                 "home_tov": 0, "away_tov": 0, "home_fta": 0, "away_fta": 0, "home_fg3a": 0, "away_fg3a": 0,
                 "valid": len(lineup[home]) == 5 and len(lineup[away]) == 5}
        stints.append(stint)

    def close_stint(at):
        if stint is not None:
            stint["end"] = at
            dur = stint["end"] - stint["start"]
            for side, team in (("home", home), ("away", away)):
                opp = "away" if side == "home" else "home"
                for pid in stint[f"{side}_lineup"]:
                    o = onoff[pid]
                    o["seconds"] += dur
                    o["pts_for"] += stint[f"{side}_pts"]
                    o["pts_against"] += stint[f"{opp}_pts"]
                    o["poss_off"] += stint[f"{side}_poss"]
                    o["poss_def"] += stint[f"{opp}_poss"]
                    o["oreb"] += stint[f"{side}_oreb"]
                    o["dreb"] += stint[f"{side}_dreb"]
                    o["opp_oreb"] += stint[f"{opp}_oreb"]
                    o["opp_dreb"] += stint[f"{opp}_dreb"]

    def end_possession(at, reason):
        nonlocal poss, last_poss_end
        if poss is None:
            return
        poss["end"] = at
        poss["end_reason"] = reason
        poss["duration"] = round(at - poss["start"], 1)
        possessions.append(poss)
        side = "home" if poss["offense_id"] == home else "away"
        if stint is not None:
            stint[f"{side}_poss"] += 1
        last_poss_end = {"elapsed": at, "start_type": reason}
        poss = None

    def ensure_possession(team_id, at, play):
        nonlocal poss
        if poss is not None and poss["offense_id"] == team_id:
            return poss
        if poss is not None:
            end_possession(poss.get("terminal_at", at), poss.get("terminal", "change"))
        start_type = {"made_fg": "after_made_fg", "made_ft": "after_made_ft", "dreb": "after_dreb",
                      "steal": "after_steal", "tov": "after_dead_ball_tov"}.get(last_poss_end["start_type"], last_poss_end["start_type"])
        hs, as_ = play["home_score"], play["away_score"]
        if play["scoring"]:  # running score is post-event; recover the pre-event state
            hs -= play["score_value"] if team_id == home else 0
            as_ -= play["score_value"] if team_id == away else 0
        poss = {"game_id": g["id"], "possession_index": len(possessions), "period": cur_period,
                "start_margin": (hs - as_) if team_id == home else (as_ - hs),
                "offense_id": team_id, "defense_id": other[team_id], "start": last_poss_end["elapsed"],
                "start_type": start_type, "points": 0, "fga": 0, "fg3a": 0, "fta": 0, "oreb": 0, "tov": 0,
                "first_action": None, "second_chance_points": 0, "stint_index": stint["stint_index"] if stint else None,
                "offense_lineup": sorted(lineup[team_id]), "defense_lineup": sorted(lineup[other[team_id]])}
        return poss

    cur_period = periods[0]
    for period in periods:
        cur_period = period
        pp = by_period[period]
        if period > 1:
            starters, f = _period_starters(pp, team_players, lineup)
            flags += [f"{x}:p{period}" for x in f]
            lineup = {t: list(v) for t, v in starters.items()}
        start_at = (period - 1) * 720 if period <= 4 else 2880 + (period - 5) * 300
        open_stint(start_at)
        last_poss_end = {"elapsed": start_at, "start_type": "period_start"}
        for p in pp:
            k = kind(p)
            at = p["elapsed"]
            team = p["team_id"] if p["team_id"] in (home, away) else None
            if k == "sub":
                t = team_of.get(p["p1"]) or team
                if t is None or not p["p1"] or not p["p2"]:
                    invalid_events += 1
                    flags.append(f"sub_unknown_team:{p['seq']}")
                    continue
                if p["p1"] in lineup[t] and p["p2"] not in lineup[t]:
                    continue  # duplicate/late row: already reflected by an earlier repair
                if p["p2"] not in lineup[t]:
                    repair(t, p["p2"], at, p["seq"], "leaving_player_not_on_court")
                close_stint(at)
                lineup[t] = [p["p1"] if x == p["p2"] else x for x in lineup[t]]
                open_stint(at)
                continue
            pid = p["p1"]
            if pid and k in ("fga", "ft", "oreb", "dreb", "tov") and team_of.get(pid) in lineup \
                    and pid not in lineup[team_of[pid]] and "technical" not in p["type"].lower():
                repair(team_of[pid], pid, at, p["seq"], "actor_not_on_court")
            valid = len(lineup[home]) == 5 and len(lineup[away]) == 5
            if not valid:
                invalid_events += 1
            side = "home" if team == home else "away" if team == away else None
            ev = {"seq": p["seq"], "kind": k, "stint_index": stint["stint_index"], "valid_lineup": valid}
            if k == "fga" and team:
                cur = ensure_possession(team, at, p)
                geo = shot_geometry(p["x"], p["y"], p["points_attempted"])
                made = p["scoring"]
                pts = p["score_value"] if made else 0
                text = p["text"].lower()
                assister = p["p2"] if made and "assist" in text else None
                blocker = p["p2"] if (not made and "block" in text) else None
                second = cur["oreb"] > 0
                into = round(at - cur["start"], 1)
                if cur["first_action"] is None:
                    cur["first_action"] = into
                transition = cur["start_type"] in ("after_dreb", "after_steal") and cur["first_action"] is not None and cur["first_action"] <= TRANSITION_SECONDS
                # NBA 2024-25 rule: a missed shot from >= 36 ft in the final 3 seconds of
                # quarters 1-3 is a team attempt, not an individual FGA.
                heave = (not made and period <= 3 and p["clock"] <= 3 and
                         ((geo["distance_ft"] or 0) >= 36 or "heave" in p["type"].lower()))
                margin = (p["home_score"] - p["away_score"] - (pts if team == home else -pts)) * (1 if team == home else -1)
                shots.append({"game_id": g["id"], "seq": p["seq"], "period": period, "clock": p["clock"],
                              "elapsed": at, "team_id": team, "opponent_id": other[team], "shooter_id": p["p1"],
                              "assist_id": assister, "block_id": blocker, "made": bool(made),
                              "points_attempted": 3 if p["points_attempted"] == 3 else 2, "points": pts,
                              "x": p["x"], "y": p["y"], **geo, "family": shot_family(p["type"], p["text"]),
                              "type": p["type"], "second_chance": second, "seconds_into_possession": into,
                              "transition": bool(transition), "score_margin": margin, "team_attempt_heave": bool(heave),
                              "home": team == home, "possession_index": cur["possession_index"],
                              "offense_lineup": sorted(lineup[team]), "defense_lineup": sorted(lineup[other[team]])})
                cur["fga"] += 1
                cur["fg3a"] += int(p["points_attempted"] == 3)
                stint[f"{side}_fga"] += 1
                stint[f"{side}_fg3a"] += int(p["points_attempted"] == 3)
                if made:
                    cur["points"] += pts
                    if second:
                        cur["second_chance_points"] += pts
                    stint[f"{side}_pts"] += pts
                    # Possession ends at the next opponent action; an and-one FT stays here.
                    cur["terminal"], cur["terminal_at"] = "made_fg", at
                ev["shot_index"] = len(shots) - 1
            elif k == "ft" and team:
                info = ft_info(p)
                pts = p["score_value"] if info["made"] else 0
                if info["technical"]:
                    technical_points[team] += pts
                    stint[f"{side}_pts"] += pts
                else:
                    cur = ensure_possession(team, at, p)
                    cur.pop("terminal", None)
                    if cur["first_action"] is None:
                        cur["first_action"] = round(at - cur["start"], 1)
                    cur["fta"] += 1
                    cur["points"] += pts
                    if cur["oreb"] > 0:
                        cur["second_chance_points"] += pts
                    stint[f"{side}_pts"] += pts
                    stint[f"{side}_fta"] += 1
                    if info["last"] and info["made"] and not info["flagrant"]:
                        cur["terminal"], cur["terminal_at"] = "made_ft", at
            elif k == "oreb" and team:
                cur = ensure_possession(team, at, p)
                cur.pop("terminal", None)
                cur["oreb"] += 1
                stint[f"{side}_oreb"] += 1
            elif k == "dreb" and team:
                if poss is not None and poss["offense_id"] != team:
                    poss.pop("terminal", None)
                    end_possession(at, "dreb")
                stint[f"{side}_dreb"] += 1
                ensure_possession(team, at, p)
            elif k == "tov" and team:
                cur = ensure_possession(team, at, p)
                if cur["first_action"] is None:
                    cur["first_action"] = round(at - cur["start"], 1)
                cur["tov"] += 1
                stint[f"{side}_tov"] += 1
                cur.pop("terminal", None)
                end_possession(at, "steal" if "steal" in p["text"].lower() else "tov")
            elif k == "end":
                pass
            events.append(ev)
        end_at = (period * 720) if period <= 4 else 2880 + (period - 4) * 300
        if poss is not None:
            end_possession(poss.get("terminal_at", end_at), poss.get("terminal", "period_end"))
        close_stint(end_at)
        stint = None
    # Post-hoc: a pending end flagged on the last possession may still be attached.
    for ps in possessions:
        ps.pop("terminal", None)
        ps.pop("terminal_at", None)
        ps["transition"] = ps["start_type"] in ("after_dreb", "after_steal") and ps["first_action"] is not None and ps["first_action"] <= TRANSITION_SECONDS
    # Validation
    final_pts = {home: g["home_score"], away: g["away_score"]}
    poss_pts = {t: sum(p["points"] for p in possessions if p["offense_id"] == t) for t in (home, away)}
    stint_pts = {home: sum(s["home_pts"] for s in stints), away: sum(s["away_pts"] for s in stints)}
    # The running score on the final row is sometimes stale; the max running score is robust.
    pbp_final = {home: max(p["home_score"] for p in plays), away: max(p["away_score"] for p in plays)}
    box_fga = {t: sum(b["fga"] for b in parsed["boxes"] if b["team_id"] == t) for t in (home, away)}
    shot_fga = {t: sum(1 for s in shots if s["team_id"] == t and not s["team_attempt_heave"]) for t in (home, away)}
    box_min = {b["player_id"]: b["minutes"] for b in parsed["boxes"]}
    min_err = [abs(onoff[p]["seconds"] / 60 - m) for p, m in box_min.items() if m > 0]
    game_seconds = 2880 + 300 * max(0, max(periods) - 4)
    stint_seconds = sum(s["end"] - s["start"] for s in stints)
    checks = {
        "pbp_final_matches_box": pbp_final == final_pts,
        "possession_points_reconcile": all(poss_pts[t] + technical_points[t] == final_pts[t] for t in final_pts),
        "stint_points_reconcile": stint_pts == final_pts,
        "shots_match_box_fga": shot_fga == box_fga,
        "stint_seconds_match_game": abs(stint_seconds - game_seconds) < 1,
        "lineups_always_five": invalid_events == 0 and all(s["valid"] for s in stints),
        "lineup_repairs": len(repairs),
        "player_minutes_mae": round(sum(min_err) / len(min_err), 3) if min_err else None,
    }
    valid = checks["possession_points_reconcile"] and checks["stint_points_reconcile"] \
        and checks["lineups_always_five"] and len(repairs) <= 8 and (checks["player_minutes_mae"] or 0) < 1.5
    for s in stints:
        s["seconds"] = round(s["end"] - s["start"], 1)
    return {"stints": [s for s in stints if s["seconds"] > 0 or s["home_pts"] or s["away_pts"]],
            "possessions": possessions, "shots": shots,
            "player_onoff": {k: dict(v) for k, v in onoff.items()}, "events": events,
            "technical_points": technical_points,
            "quality": {"pbp_available": True, "checks": checks, "flags": flags[:50],
                        "invalid_lineup_events": invalid_events, "lineup_repairs": repairs[:20], "valid": bool(valid),
                        "possessions": {t: sum(1 for p in possessions if p["offense_id"] == t) for t in (home, away)}}}
