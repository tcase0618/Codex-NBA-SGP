import json
from pathlib import Path

from .schemas import utc

CATALOG = json.loads(Path(__file__).with_name("metric_catalog.json").read_text(encoding="utf-8"))


def divide(n, d, scale=1):
    return n / d * scale if d else None


def totals(boxes):
    return {key: sum(b.get(key, 0) for b in boxes) for key in (
        "minutes", "pts", "fga", "fgm", "fg3a", "fg3m", "fta", "ftm", "oreb", "dreb", "ast", "stl", "blk", "tov", "pf")}


def metric_values(dataset, scope, entity_id, cutoff):
    games = [g for g in dataset["games"] if utc(g["known_at"]) <= cutoff and utc(g["completed_at"]) < cutoff]
    key = "team_id" if scope == "team" else "player_id"
    games = [g for g in games if any(b[key] == entity_id for b in g["boxes"])]
    boxes = [b for g in games for b in g["boxes"] if b[key] == entity_id]
    s = totals(boxes)
    n = len(games) if scope == "team" else sum(b["minutes"] > 0 for b in boxes)
    two_a, two_m = s["fga"] - s["fg3a"], s["fgm"] - s["fg3m"]
    values = {}
    estimates = set()
    if n:
        values = {"Points / game": divide(s["pts"], n), "Points / 36 minutes": divide(s["pts"], s["minutes"], 36),
            "True shooting %": divide(s["pts"], 2 * (s["fga"] + .44 * s["fta"]), 100),
            "Effective FG%": divide(s["fgm"] + .5 * s["fg3m"], s["fga"], 100),
            "2PT FG%": divide(two_m, two_a, 100), "3PT FG%": divide(s["fg3m"], s["fg3a"], 100),
            "FT%": divide(s["ftm"], s["fta"], 100), "3PA share": divide(s["fg3a"], s["fga"], 100),
            "Assist-to-turnover ratio": divide(s["ast"], s["tov"]),
            "Minutes / game": divide(s["minutes"], n), "Games played": sum(b["minutes"] > 0 for b in boxes),
            "Turnover rate": divide(s["tov"], s["fga"] + .44 * s["fta"] + s["tov"], 100),
            "Defensive rebounds secured": s["dreb"]}
        estimates.update({"True shooting %", "Turnover rate"})
        if scope == "team":
            opp = totals([b for g in games for b in g["boxes"] if b["team_id"] != entity_id])
            poss = .5 * (s["fga"] + .44 * s["fta"] - s["oreb"] + s["tov"] + opp["fga"] + .44 * opp["fta"] - opp["oreb"] + opp["tov"])
            rating = divide(s["pts"], poss, 100)
            defense = divide(opp["pts"], poss, 100)
            minutes = sum(48 + 5 * g["overtime_periods"] for g in games)
            values.update({"Offensive rating": rating, "Defensive rating": defense,
                "Net rating": rating - defense if rating is not None else None,
                "Pace": divide(poss, minutes, 48), "Points allowed / game": divide(opp["pts"], n),
                "Point differential": divide(s["pts"] - opp["pts"], n),
                "Team TS%": values["True shooting %"], "Team eFG%": values["Effective FG%"],
                "2PT%": values["2PT FG%"], "3PT%": values["3PT FG%"],
                "Free throw conversion %": values["FT%"], "Free throw attempt rate": divide(s["fta"], s["fga"], 100),
                "Offensive rebound %": divide(s["oreb"], s["oreb"] + opp["dreb"], 100),
                "Defensive rebound %": divide(s["dreb"], s["dreb"] + opp["oreb"], 100),
                "Opponent offensive rebound %": divide(opp["oreb"], opp["oreb"] + s["dreb"], 100),
                "Opponent eFG%": divide(opp["fgm"] + .5 * opp["fg3m"], opp["fga"], 100),
                "Opponent TS%": divide(opp["pts"], 2 * (opp["fga"] + .44 * opp["fta"]), 100),
                "Opponent free throw rate": divide(opp["fta"], opp["fga"], 100),
                "Assists / 100": divide(s["ast"], poss, 100), "Blocks / 100": divide(s["blk"], poss, 100),
                "Turnover %": divide(s["tov"], s["fga"] + .44 * s["fta"] + s["tov"], 100)})
            estimates.update({"Offensive rating", "Defensive rating", "Net rating", "Pace", "Assists / 100", "Blocks / 100", "Team TS%", "Opponent TS%", "Turnover %"})
    result = []
    for m in CATALOG:
        if m["scope"] != scope:
            continue
        value = values.get(m["label"])
        available = value is not None
        result.append({**m, "value": round(value, 4) if available else None,
            "status": ("SYNTHETIC_DERIVED" if dataset["synthetic"] else "ESTIMATED" if m["label"] in estimates else "DERIVED") if available else "UNAVAILABLE",
            "sample_games": len(games), "games_played": n, "source": dataset["source"] if available else None,
            "source_url": dataset["source_url"] if available else None,
            "known_at": max((g["known_at"] for g in games), default=None) if available else None,
            "formula_basis": "Box-score aggregates; percentages 0–100; TS/ratings use 0.44 FTA possession approximation" if available else "Required inputs or validated formula not implemented",
            "uncertainty": "Descriptive sample; confidence interval not estimated" if available else None})
    return {"metrics": result, "coverage": {"available": sum(m["value"] is not None for m in result), "total": len(result)}}


def profiles(dataset, cutoff):
    players = []
    for p in dataset["players"]:
        rs = [r for r in dataset["rotations"] if r["player_id"] == p["id"] and utc(r["known_at"]) <= cutoff]
        rotation = max(rs, key=lambda r: utc(r["known_at"])) if rs else None
        players.append({**p, "team_id": rotation["team_id"] if rotation else None,
                        **metric_values(dataset, "player", p["id"], cutoff), "rotation": rotation})
    return {"players": players, "teams": [{**t, **metric_values(dataset, "team", t["id"], cutoff)} for t in dataset["teams"]]}
