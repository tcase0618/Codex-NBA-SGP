"""Synthetic ESPN-format game summaries for intelligence-platform tests.

These are FICTIONAL games generated from simple rules, used only to test
parsing, reconstruction, accounting and pipelines. They are never NBA data.
"""
import random
from datetime import datetime, timedelta, timezone

KEYS = ["minutes", "points", "fieldGoalsMade-fieldGoalsAttempted", "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
        "freeThrowsMade-freeThrowsAttempted", "rebounds", "assists", "turnovers", "steals", "blocks", "offensiveRebounds",
        "defensiveRebounds", "fouls", "plusMinus"]


def team_players(team_id, n=10):
    pos = ["PG", "SG", "SF", "PF", "C"] * 2
    return [{"id": f"{team_id}{i:02d}", "name": f"Player {team_id}-{i}", "pos": pos[i]} for i in range(n)]


def make_summary(game_id, home, away, tipoff, seed, season=2026):
    rng = random.Random(seed)
    teams = {home: team_players(home), away: team_players(away)}
    box = {p["id"]: {k: 0 for k in ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "tov", "stl", "blk", "pf", "sec")}
           for t in teams.values() for p in t}
    lineup = {t: [p["id"] for p in teams[t][:5]] for t in teams}
    plays, score, seq = [], {home: 0, away: 0}, [0]
    last_sub = {home: 0.0, away: 0.0}

    def add(period, clock, typ, text, team, parts, scoring=False, value=0, shooting=False, pa=0, x=None, y=None):
        seq[0] += 1
        plays.append({"id": f"{game_id}{seq[0]}", "sequenceNumber": str(seq[0]), "type": {"id": "1", "text": typ}, "text": text,
                      "awayScore": score[away], "homeScore": score[home], "period": {"number": period},
                      "clock": {"displayValue": f"{int(clock // 60)}:{int(clock % 60):02d}" if clock >= 60 else f"{clock:.1f}"},
                      "scoringPlay": scoring, "scoreValue": value, "team": {"id": team},
                      "participants": [{"athlete": {"id": p}} for p in parts],
                      "shootingPlay": shooting, "pointsAttempted": pa,
                      "coordinate": {"x": x if x is not None else -214748340, "y": y if y is not None else -214748365}})

    def name(pid):
        return f"P{pid}"

    off = home
    for period in (1, 2, 3, 4):
        clock = 720.0
        while clock > 6:
            elapsed_before = 720 - clock
            dt = rng.uniform(10, 20)
            for t in teams:
                for pid in lineup[t]:
                    box[pid]["sec"] += min(dt, clock)
            clock = max(0.0, clock - dt)
            de = away if off == home else home
            shooter = rng.choice(lineup[off])
            r = rng.random()
            if r < 0.12:  # turnover, half with steals
                box[shooter]["tov"] += 1
                if rng.random() < .5:
                    thief = rng.choice(lineup[de])
                    box[thief]["stl"] += 1
                    add(period, clock, "Lost Ball Turnover", f"{name(shooter)} lost ball turnover ({name(thief)} steals)", off, [shooter, thief])
                else:
                    add(period, clock, "Bad Pass Turnover", f"{name(shooter)} bad pass turnover", off, [shooter])
            elif r < 0.22:  # shooting foul, two free throws
                fouler = rng.choice(lineup[de])
                box[fouler]["pf"] += 1
                add(period, clock, "Shooting Foul", f"{name(fouler)} shooting foul", de, [fouler])
                for k in (1, 2):
                    made = rng.random() < .78
                    box[shooter]["fta"] += 1
                    if made:
                        box[shooter]["ftm"] += 1
                        box[shooter]["pts"] += 1
                        score[off] += 1
                    add(period, clock, f"Free Throw - {k} of 2", f"{name(shooter)} {'makes' if made else 'misses'} free throw {k} of 2", off, [shooter], made, int(made))
                if not made:
                    reb = rng.choice(lineup[de])
                    box[reb]["dreb"] += 1
                    add(period, clock, "Defensive Rebound", f"{name(reb)} defensive rebound", de, [reb])
            else:
                for attempt in range(3):
                    three = rng.random() < .38
                    x = rng.choice([2, 48]) if three and rng.random() < .25 else rng.uniform(10, 40) if three else rng.uniform(20, 30)
                    y = 3.0 if three and x in (2, 48) else rng.uniform(22, 27) if three else rng.choice([rng.uniform(0, 12), rng.uniform(15, 19)])
                    made = rng.random() < (.36 if three else .54)
                    box[shooter]["fga"] += 1
                    box[shooter]["fg3a"] += three
                    parts = [shooter]
                    text = f"{name(shooter)} {'makes' if made else 'misses'} {'three point jumper' if three else 'driving layup'}"
                    if made:
                        pts = 3 if three else 2
                        box[shooter]["fgm"] += 1
                        box[shooter]["fg3m"] += three
                        box[shooter]["pts"] += pts
                        score[off] += pts
                        if rng.random() < .6:
                            passer = rng.choice([p for p in lineup[off] if p != shooter])
                            box[passer]["ast"] += 1
                            parts.append(passer)
                            text += f" ({name(passer)} assists)"
                        add(period, clock, "Jump Shot" if three else "Driving Layup Shot", text, off, parts, True, pts, True, 3 if three else 2, round(x), round(y))
                        break
                    add(period, clock, "Jump Shot" if three else "Driving Layup Shot", text, off, parts, False, 0, True, 3 if three else 2, round(x), round(y))
                    if rng.random() < .27:
                        reb = rng.choice(lineup[off])
                        box[reb]["oreb"] += 1
                        add(period, clock, "Offensive Rebound", f"{name(reb)} offensive rebound", off, [reb])
                        shooter = reb
                        continue
                    reb = rng.choice(lineup[de])
                    box[reb]["dreb"] += 1
                    add(period, clock, "Defensive Rebound", f"{name(reb)} defensive rebound", de, [reb])
                    break
            # Substitutions at dead-ball-like moments (synthetic).
            for t in teams:
                if elapsed_before - last_sub[t] > 50 and rng.random() < .7:
                    bench = [p["id"] for p in teams[t] if p["id"] not in lineup[t]]
                    out = rng.choice(lineup[t])
                    inn = rng.choice(bench)
                    lineup[t] = [inn if p == out else p for p in lineup[t]]
                    add(period, clock, "Substitution", f"{name(inn)} enters the game for {name(out)}", t, [inn, out])
                    last_sub[t] = elapsed_before
            off = de
        for t in teams:  # remainder of the period
            for pid in lineup[t]:
                box[pid]["sec"] += clock
        add(period, 0.0, "End Period", f"End of period {period}", None, [])
    if score[home] == score[away]:  # force a winner with a final free throw
        shooter = lineup[home][0]
        box[shooter]["fta"] += 1
        box[shooter]["ftm"] += 1
        box[shooter]["pts"] += 1
        score[home] += 1
        add(4, 0.0, "Free Throw - 1 of 1", f"{name(shooter)} makes free throw 1 of 1", home, [shooter], True, 1)
    # Integer-second minutes: adjust so team totals are exactly 240 minutes.
    athletes = {}
    for t in teams:
        rows = []
        for i, p in enumerate(teams[t]):
            b = box[p["id"]]
            dnp = b["sec"] == 0
            mins = int(round(b["sec"] / 60))
            stats = [] if dnp else [str(mins), str(b["pts"]), f"{b['fgm']}-{b['fga']}", f"{b['fg3m']}-{b['fg3a']}", f"{b['ftm']}-{b['fta']}",
                                    str(b["oreb"] + b["dreb"]), str(b["ast"]), str(b["tov"]), str(b["stl"]), str(b["blk"]),
                                    str(b["oreb"]), str(b["dreb"]), str(b["pf"]), "0"]
            rows.append({"athlete": {"id": p["id"], "displayName": p["name"], "position": {"abbreviation": p["pos"]}, "jersey": str(i)},
                         "starter": i < 5, "didNotPlay": dnp, "reason": "COACH'S DECISION" if dnp else None, "ejected": False, "stats": stats})
        athletes[t] = rows
    tip = tipoff.strftime("%Y-%m-%dT%H:%MZ")
    comp = {"date": tip, "neutralSite": False, "status": {"type": {"completed": True}},
            "competitors": [{"homeAway": "home", "score": str(score[home]), "team": {"id": home, "abbreviation": f"T{home}", "displayName": f"Team {home}", "location": f"City {home}"}},
                            {"homeAway": "away", "score": str(score[away]), "team": {"id": away, "abbreviation": f"T{away}", "displayName": f"Team {away}", "location": f"City {away}"}}]}
    payload = {"header": {"id": game_id, "season": {"year": season, "type": 2}, "competitions": [comp]},
               "gameInfo": {"venue": {"fullName": "Synthetic Arena"}, "attendance": 0},
               "boxscore": {"players": [{"team": {"id": t}, "statistics": [{"keys": KEYS, "athletes": athletes[t]}]} for t in (home, away)]},
               "plays": plays,
               "pickcenter": [{"provider": {"name": "Synthetic"}, "spread": -2.5, "overUnder": 220.5,
                               "homeTeamOdds": {"moneyLine": -140}, "awayTeamOdds": {"moneyLine": 120}}]}
    provenance = {"provider": "SYNTHETIC", "provider_class": "test_fixture", "url": f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={game_id}",
                  "retrieved_at": "2026-07-01T00:00:00+00:00", "sha256": f"synthetic-{game_id}-{seed}", "bytes": 0}
    return payload, provenance


def build_league(wh, teams=("1", "2", "3", "4"), rounds=6, start=datetime(2025, 11, 1, 0, 0, tzinfo=timezone.utc)):
    """Round-robin synthetic league ingested into a warehouse; returns game ids."""
    from nba_quant.intel.parse import parse_summary
    gid, day, ids = 900000, start, []
    for r in range(rounds):
        for i, h in enumerate(teams):
            for a in teams[i + 1:]:
                home, away = (h, a) if (r + i) % 2 == 0 else (a, h)
                payload, prov = make_summary(str(gid), home, away, day, seed=gid)
                wh.ingest(parse_summary(payload, prov), prov)
                ids.append(str(gid))
                gid += 1
                day += timedelta(hours=26)
    return ids
