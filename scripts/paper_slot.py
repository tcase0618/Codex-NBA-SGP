"""Apply ops rules to a slot_scan.py output and append to ledger/paper_ledger.jsonl.

Engine families are logged only when every leg has a positive uncertainty-adjusted edge (else PASS);
every positive-edge leg is logged as a 1-unit paper single (Codex-NBA-SGP#1: NBA is paper only).
Usage: .venv/bin/python scripts/paper_slot.py output/nightly-priority/slot-<date>-<tag>.json "<slot label>"
"""
import json, sys
from datetime import datetime, timezone
path, slot = sys.argv[1], sys.argv[2]
r = json.load(open(path)); now = datetime.now(timezone.utc).isoformat(); rows, orders = [], {}
def leg(gid, c):
    a = c["aggressive_evidence"]
    return {"game_id": gid, "player": c["player_name"], "stat": c["stat"], "side": "over", "line": c["line"], "ask": c["offer"]["best_ask"],
            "proxy_p": round(a["proxy_probability"], 4), "edge": round(a["proxy_return_gross"], 3), "asset_id": c["offer"]["asset_id"]}
for g in r["games"]:
    C = {(c["player_name"], c["stat"], c["line"]): c for c in g["candidates"]}
    for c in g["candidates"]:
        if c["aggressive_evidence"]["proxy_return_gross"] > 0: orders[(c["player_name"], c["stat"])] = leg(g["game_id"], c)
    for f in g["families"]:
        nm = f"{f['leg_count']}-leg"
        if f["status"] == "BLOCKED" or not f["legs"]:
            rows.append({"type": "ticket", "slot": slot, "game_id": g["game_id"], "name": nm, "logged_at": now, "status": "PASS", "note": "engine BLOCKED"}); continue
        L, bad = [], []
        for l in f["legs"]:
            c = C.get((l["player_name"], l["stat"], l["line"]))
            (L.append(leg(g["game_id"], c)) if c and c["aggressive_evidence"]["proxy_return_gross"] > 0 else bad.append(l["player_name"]))
        if bad:
            rows.append({"type": "ticket", "slot": slot, "game_id": g["game_id"], "name": nm, "logged_at": now, "status": "PASS", "note": "edge<=0: " + ", ".join(bad)}); continue
        p = q = 1
        for x in L: p *= x["proxy_p"]; q *= x["ask"]
        rows.append({"type": "ticket", "slot": slot, "game_id": g["game_id"], "name": nm, "logged_at": now, "legs": L, "joint_indep_proxy": round(p, 4),
                     "break_even_decimal": round(1 / q, 2), "status": "OPEN", "recommendation": "PAPER"})
        print(g["game_id"], nm, [(x["player"], x["line"], x["stat"], x["ask"]) for x in L], round(p, 3), round(1 / q, 2))
for x in orders.values():
    rows.append({"type": "paper_order", "slot": slot, "logged_at": now, "venue": "polymarket.com (international CLOB, read-only quote)",
                 "executable_for_account": False, "side": "BUY_YES", "price": x["ask"], "units": 1, **x, "status": "OPEN"})
open("ledger/paper_ledger.jsonl", "a").write("".join(json.dumps(x) + "\n" for x in rows))
print("tickets", sum(1 for x in rows if x["type"] == "ticket" and x["status"] == "OPEN"), "PASS", sum(1 for x in rows if x.get("status") == "PASS"), "paper", len(orders))
