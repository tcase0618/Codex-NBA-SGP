"""Append settlement rows for graded NBA tickets/paper orders (append-only ledger).

Usage: .venv/bin/python scripts/settle_nba.py [YYYY-MM-DD]   # date prefix for the 'yesterday' summary
Tickets are scored at break_even_decimal (fair odds); paper singles at 1 - price / -price.
"""
import datetime as dt, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from grade_ledger import main_nba

L = "ledger/paper_ledger.jsonl"
rows = [json.loads(l) for l in open(L)]
done = {r["ref_row"] for r in rows if r.get("type") == "settlement"}
now = dt.datetime.now(dt.timezone.utc).isoformat()
new = []
for g in main_nba(L):
    if g["ref"] in done or g["result"] not in ("WON", "LOST", "VOID"):
        continue
    s = {"type": "settlement", "ref_row": g["ref"], "settled_at": now, "source": "ESPN final box score", "result": g["result"]}
    if g["kind"] == "paper":
        s.update(pnl_units=g["pnl"], actual=g["actual"])
    else:
        f = rows[g["ref"]].get("break_even_decimal") or 0
        s["legs"] = g["legs"]
        s["pnl_units_at_fair"] = round(f - 1, 2) if g["result"] == "WON" else (-1.0 if g["result"] == "LOST" else 0)
    new.append(s)
with open(L, "a") as fh:
    fh.write("".join(json.dumps(s) + "\n" for s in new))
rows += new

def agg(pred):
    w = l = 0; p = 0.0
    for r in rows:
        if r.get("type") != "settlement": continue
        o = rows[r["ref_row"]]
        if not pred(o): continue
        w += r["result"] == "WON"; l += r["result"] == "LOST"
        p += r.get("pnl_units", r.get("pnl_units_at_fair", 0)) or 0
    return w, l, round(p, 2)

day = sys.argv[1] if len(sys.argv) > 1 else ""
y = lambda o: str(o.get("slot", "")).startswith(day)
print("new settlements", len(new))
print("day", day, "tickets", agg(lambda o: y(o) and o.get("type") == "ticket"), "paper", agg(lambda o: y(o) and o.get("type") == "paper_order"))
print("cum tickets", agg(lambda o: o.get("type") == "ticket"), "paper", agg(lambda o: o.get("type") == "paper_order"))
