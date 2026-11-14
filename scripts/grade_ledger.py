"""Grade paper_ledger.jsonl tickets and paper orders against ESPN final box scores. Appends settlement rows."""
import json, sys, urllib.request, re, unicodedata
from datetime import datetime, timezone
NBA_EVENTS = {}
def get(p, e):
    return json.load(urllib.request.urlopen(f"https://site.api.espn.com/apis/site/v2/sports/{p}/summary?event={e}", timeout=30))
def norm(s):
    s = re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()).split()
    return " ".join(w for w in s if w not in {"jr", "sr", "ii", "iii", "iv", "v"})
def nba_box(e):
    j = get("basketball/nba", e); st = j["header"]["competitions"][0]["status"]["type"]
    out = {}
    for team in j["boxscore"]["players"]:
        g = team["statistics"][0]; k = g["keys"]
        for a in g["athletes"]:
            n = norm(a["athlete"]["displayName"])
            if a.get("didNotPlay") or not a["stats"]: out[n] = None; continue
            v = dict(zip(k, a["stats"]))
            out[n] = {"pts": int(v["points"]), "reb": int(v["rebounds"]), "ast": int(v["assists"]),
                      "fg3m": int(v["threePointFieldGoalsMade-threePointFieldGoalsAttempted"].split("-")[0])}
    return out, st["completed"]
def nfl_box(e):
    j = get("football/nfl", e); c = j["header"]["competitions"][0]
    out = {}
    for team in j["boxscore"]["players"]:
        for g in team["statistics"]:
            for a in g["athletes"]:
                n = norm(a["athlete"]["displayName"]); d = out.setdefault(n, {}); v = dict(zip(g["keys"], a["stats"]))
                if g["name"] == "passing": d["pass_yds"] = int(v["passingYards"])
                if g["name"] == "rushing": d["rush_yds"] = int(v["rushingYards"])
                if g["name"] == "receiving": d["rec_yds"] = int(v["receivingYards"]); d["rec"] = int(v["receptions"])
    sc = {t["team"]["abbreviation"]: int(t["score"]) for t in c["competitors"]}
    return out, sc, c["status"]["type"]["completed"]
def grade_val(val, line, final):
    if val is None: return "VOID" if final else "PENDING"
    if val > line: return "WON"
    return "LOST" if final else "PENDING"
def combine(gs):
    if "LOST" in gs: return "LOST"
    if all(g == "WON" for g in gs): return "WON"
    if "PENDING" in gs: return "PENDING"
    return "VOID_AFFECTED"

def main_nba(path):
    rows = [json.loads(l) for l in open(path)]
    cache = {}
    def box(e):
        if e not in cache: cache[e] = nba_box(e)
        return cache[e]
    def leg_grade(l, gid):
        b, fin = box(l.get("game_id") or gid); n = norm(l["player"])
        if n not in b: return ("VOID" if fin else "PENDING"), None
        v = None if b[n] is None else b[n][l["stat"]]
        return grade_val(v, l["line"], fin), v
    out = []
    for i, r in enumerate(rows):
        if r.get("type") == "ticket" and r.get("legs"):
            g = [leg_grade(l, r.get("game_id")) for l in r["legs"]]
            out.append({"ref": i, "kind": "ticket", "slot": r["slot"], "name": r["name"], "result": combine([x[0] for x in g]),
                        "legs": [f'{l["player"]} o{l["line"]} {l["stat"]}: {v} {gr}' for l, (gr, v) in zip(r["legs"], g)],
                        "be": r.get("break_even_decimal"), "rec": r.get("recommendation", "PLAY")})
        elif r.get("type") == "paper_order":
            gr, v = leg_grade(r, r.get("game_id"))
            pnl = {"WON": 1 - r["price"], "LOST": -r["price"]}.get(gr, 0.0)
            out.append({"ref": i, "kind": "paper", "slot": r["slot"], "leg": f'{r["player"]} o{r["line"]} {r["stat"]}', "actual": v, "result": gr, "price": r["price"], "pnl": round(pnl, 3)})
    return out

def main_nfl(path, eid="401873051"):
    rows = [json.loads(l) for l in open(path)]
    b, sc, fin = nfl_box(eid); m = sc["BUF"] - sc["MIN"]; tot = sc["BUF"] + sc["MIN"]
    def leg(s):
        if s == "BUF ML": return ("WON" if m > 0 else "LOST") if fin else "PENDING", f"{sc['BUF']}-{sc['MIN']}"
        if s == "BUF -3.5": return ("WON" if m > 3.5 else "LOST"), m
        if s == "Over 46.5": return ("WON" if tot > 46.5 else "LOST"), tot
        mm = re.match(r"(.+) (\w+) o([\d.]+)$", s); n, st, ln = norm(mm[1]), mm[2], float(mm[3])
        v = b.get(n, {}).get(st, 0 if n in b else None)
        return grade_val(v, ln, fin), v
    out = []
    for i, r in enumerate(rows):
        if r.get("type") == "ticket":
            g = [leg(s) for s in r["legs"]]
            out.append({"ref": i, "kind": "ticket", "slot": r["slot"], "name": r["name"], "result": combine([x[0] for x in g]),
                        "legs": [f"{s}: {v} {gr}" for s, (gr, v) in zip(r["legs"], g)], "fair": r.get("fair_decimal")})
        elif r.get("type") == "paper_order":
            gr, v = leg(r["market"]); pnl = {"WON": 1 - r["price"], "LOST": -r["price"]}.get(gr, 0.0)
            out.append({"ref": i, "kind": "paper", "slot": r["slot"], "leg": r["market"], "actual": v, "result": gr, "price": r["price"], "pnl": round(pnl, 3)})
    return out

if __name__ == "__main__":
    res = main_nba(sys.argv[2]) if sys.argv[1] == "nba" else main_nfl(sys.argv[2])
    print(json.dumps(res, indent=1, default=str))
