"""Parse ESPN game summaries into normalized game, box-score and play records.

Every record is an OBSERVED provider value except fields explicitly named as
derived (elapsed seconds, shot distance/zone, shot family), which are
deterministic transformations documented in `metric_definitions`.
"""
import math
import re

SENTINEL = -1_000_000  # ESPN uses ~-2^31 for "no coordinate"
BOX_KEYS = ["minutes", "points", "fieldGoalsMade-fieldGoalsAttempted",
            "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
            "freeThrowsMade-freeThrowsAttempted", "rebounds", "assists", "turnovers", "steals",
            "blocks", "offensiveRebounds", "defensiveRebounds", "fouls", "plusMinus"]
SHOT_FAMILIES = [  # ordered: first match wins
    ("dunk", r"dunk"), ("tip", r"tip"), ("alley_oop", r"alley oop"), ("hook", r"hook"),
    ("layup", r"layup|finger roll"), ("floater", r"float"), ("stepback", r"step back"),
    ("fadeaway", r"fade ?away|turnaround"), ("pullup", r"pull ?up"), ("heave", r"heave"),
    ("jumper", r"jump|shot"),
]


def clock_seconds(display):
    """Seconds remaining in the period from '11:29' or '45.2'."""
    display = str(display or "0").strip()
    if ":" in display:
        parts = display.split(":")
        # Rare provider artifacts such as '1:39:00' carry a trailing field; keep m:s.
        m, s = parts[0], parts[1]
        return min(720.0, int(m) * 60 + float(s))
    return float(display)


def elapsed(period, remaining):
    if period <= 4:
        return (period - 1) * 720 + (720 - remaining)
    return 2880 + (period - 5) * 300 + (300 - remaining)


def period_length(period):
    return 720 if period <= 4 else 300


def _int(value):
    try:
        return int(str(value).replace("+", ""))
    except (TypeError, ValueError):
        return None


def _made_attempted(value):
    try:
        made, attempted = (int(x) for x in str(value).split("-"))
        return made, attempted
    except (TypeError, ValueError):
        return None, None


def _minutes(value):
    if value in (None, "", "--"):
        return 0.0
    text = str(value)
    if ":" in text:
        m, s = text.split(":")
        return int(m) + int(s) / 60
    return float(text)


def shot_family(type_text, text):
    blob = f"{type_text} {text}".lower()
    for name, pattern in SHOT_FAMILIES:
        if re.search(pattern, blob):
            return name
    return "other"


def shot_geometry(x, y, points_attempted):
    """Derived zone from ESPN half-court coordinates (feet; hoop at x=25, y≈0)."""
    if x is None or y is None:
        return {"distance_ft": None, "zone": "unknown", "corner_three": None, "angle_deg": None}
    dx, dy = x - 25, max(0.0, y)
    distance = math.hypot(dx, dy)
    three = points_attempted == 3
    corner = bool(three and dy <= 9)
    if three:
        zone = "corner_three" if corner else "above_break_three"
    elif distance <= 4:
        zone = "rim"
    elif distance <= 14:
        zone = "short_midrange"
    else:
        zone = "long_midrange"
    return {"distance_ft": round(distance, 1), "zone": zone, "corner_three": corner,
            "angle_deg": round(math.degrees(math.atan2(abs(dx), dy if dy else 0.01)), 1)}


def parse_summary(payload, provenance):
    header = payload["header"]
    comp = header["competitions"][0]
    competitors = {c["homeAway"]: c for c in comp["competitors"]}
    home, away = competitors["home"], competitors["away"]
    if not comp["status"]["type"].get("completed"):
        raise ValueError("Game is not completed")
    teams = {}
    for c in (home, away):
        t = c["team"]
        teams[t["id"]] = {"id": t["id"], "abbreviation": t.get("abbreviation"),
                          "name": t.get("displayName") or t.get("name"), "location": t.get("location")}
    game = {"id": header["id"], "season": header["season"]["year"], "season_type": header["season"]["type"],
            "tipoff": comp["date"], "home_id": home["team"]["id"], "away_id": away["team"]["id"],
            "home_score": int(home["score"]), "away_score": int(away["score"]),
            "neutral_site": bool(comp.get("neutralSite")),
            "venue": (payload.get("gameInfo", {}).get("venue") or {}).get("fullName"),
            "attendance": payload.get("gameInfo", {}).get("attendance"),
            "source_url": provenance["url"], "retrieved_at": provenance["retrieved_at"],
            "source_sha256": provenance["sha256"]}
    players, boxes = {}, []
    for team_block in payload["boxscore"].get("players", []):
        team_id = team_block["team"]["id"]
        for stat_block in team_block["statistics"]:
            keys = stat_block.get("keys") or []
            for a in stat_block.get("athletes", []):
                ath = a.get("athlete") or {}
                if not ath.get("id"):
                    continue  # provider placeholder row without an athlete identity
                players[ath["id"]] = {"id": ath["id"], "name": ath.get("displayName"),
                                      "position": (ath.get("position") or {}).get("abbreviation") or "UNKNOWN",
                                      "jersey": ath.get("jersey")}
                dnp = bool(a.get("didNotPlay")) or not a.get("stats")
                row = {"game_id": game["id"], "player_id": ath["id"], "team_id": team_id,
                       "starter": bool(a.get("starter")), "dnp": dnp, "dnp_reason": a.get("reason") if dnp else None,
                       "ejected": bool(a.get("ejected"))}
                values = dict(zip(keys, a.get("stats") or []))
                if dnp or keys[:len(BOX_KEYS)] != BOX_KEYS[:len(keys)]:
                    row.update({k: 0 for k in ("minutes", "pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
                                               "oreb", "dreb", "reb", "ast", "tov", "stl", "blk", "pf")}, plus_minus=None)
                    row["dnp"] = True
                else:
                    fgm, fga = _made_attempted(values["fieldGoalsMade-fieldGoalsAttempted"])
                    fg3m, fg3a = _made_attempted(values["threePointFieldGoalsMade-threePointFieldGoalsAttempted"])
                    ftm, fta = _made_attempted(values["freeThrowsMade-freeThrowsAttempted"])
                    row.update({"minutes": _minutes(values["minutes"]), "pts": _int(values["points"]),
                                "fgm": fgm, "fga": fga, "fg3m": fg3m, "fg3a": fg3a, "ftm": ftm, "fta": fta,
                                "oreb": _int(values["offensiveRebounds"]), "dreb": _int(values["defensiveRebounds"]),
                                "reb": _int(values["rebounds"]), "ast": _int(values["assists"]),
                                "tov": _int(values["turnovers"]), "stl": _int(values["steals"]),
                                "blk": _int(values["blocks"]), "pf": _int(values["fouls"]),
                                "plus_minus": _int(values["plusMinus"])})
                    if row["minutes"] == 0 and not any(row[k] for k in ("pts", "fga", "reb", "ast")):
                        row["dnp"] = True
                boxes.append(row)
    plays = []
    for p in payload.get("plays", []):
        period = p["period"]["number"]
        remaining = clock_seconds(p["clock"]["displayValue"])
        coord = p.get("coordinate") or {}
        x, y = coord.get("x"), coord.get("y")
        if x is None or y is None or x < SENTINEL or y < SENTINEL:
            x = y = None
        participants = [q["athlete"]["id"] for q in p.get("participants", []) if q.get("athlete")]
        plays.append({"game_id": game["id"], "seq": int(p["sequenceNumber"]), "period": period,
                      "clock": remaining, "elapsed": round(elapsed(period, remaining), 1),
                      "type_id": _int(p["type"].get("id")), "type": p["type"].get("text", "").replace("\n", " "),
                      "text": p.get("text", ""), "team_id": (p.get("team") or {}).get("id"),
                      "p1": participants[0] if participants else None,
                      "p2": participants[1] if len(participants) > 1 else None,
                      "p3": participants[2] if len(participants) > 2 else None,
                      "scoring": bool(p.get("scoringPlay")), "score_value": int(p.get("scoreValue") or 0),
                      "home_score": int(p.get("homeScore") or 0), "away_score": int(p.get("awayScore") or 0),
                      "shooting": bool(p.get("shootingPlay")), "points_attempted": int(p.get("pointsAttempted") or 0),
                      "x": x, "y": y})
    plays.sort(key=lambda r: r["seq"])
    # Drop exact duplicate rows; disambiguate distinct rows sharing a sequence number.
    unique, seen = [], {}
    for p in plays:
        key = (p["seq"], p["type"], p["text"], p["clock"])
        if key in seen:
            continue
        seen[key] = True
        if unique and unique[-1]["seq"] >= p["seq"]:
            p["seq"] = unique[-1]["seq"] + 0.01
        unique.append(p)
    plays = unique
    line = None
    for pc in payload.get("pickcenter") or []:
        try:
            spread = pc.get("spread")
            # ESPN 'spread' is quoted for the home team (negative = home favored).
            line = {"provider": (pc.get("provider") or {}).get("name"), "home_spread": float(spread) if spread is not None else None,
                    "total": float(pc["overUnder"]) if pc.get("overUnder") is not None else None,
                    "home_moneyline": (pc.get("homeTeamOdds") or {}).get("moneyLine"),
                    "away_moneyline": (pc.get("awayTeamOdds") or {}).get("moneyLine")}
            break
        except (TypeError, ValueError):
            continue
    return {"game": game, "teams": list(teams.values()), "players": list(players.values()),
            "boxes": boxes, "plays": plays, "market_line": line}
