"""Correlation-aware parlay construction from shared game simulations.

A leg is an indicator over simulated games; a ticket's probability is the share
of simulated games in which every leg hits, so same-game correlation is priced
exactly from the shared event stream rather than by multiplying marginals.
Cross-game tickets multiply per-game probabilities (games simulate independently).

This produces research tickets with fair odds. It does not see sportsbook player
prop prices, so it cannot certify value: play a ticket only where the offered
payout exceeds its fair odds, and note the model has not beaten the market in
walk-forward tests (docs/INTELLIGENCE-PLATFORM.md, section 8).
"""
import itertools
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import models as M
from .sim import simulate

MILESTONES = {"pts": [10, 15, 20, 25, 30, 35], "reb": [4, 6, 8, 10, 12], "ast": [3, 4, 6, 8, 10],
              "fg3m": [1, 2, 3, 4, 5], "pra": [20, 25, 30, 35, 40, 45]}
LABEL = {"pts": "points", "reb": "rebounds", "ast": "assists", "fg3m": "made threes", "pra": "pts+reb+ast"}


def american(p):
    if p <= 0 or p >= 1:
        return None
    return round(-100 * p / (1 - p)) if p >= .5 else round(100 * (1 - p) / p)


def implied(ml):
    if ml is None:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def market_lines(event):
    comp = event["competitions"][0]
    o = (comp.get("odds") or [{}])[0]
    out = {"provider": (o.get("provider") or {}).get("name"), "details": o.get("details"), "total": o.get("overUnder"),
           "home_spread": o.get("spread"),
           "home_ml": (o.get("homeTeamOdds") or {}).get("moneyLine"), "away_ml": (o.get("awayTeamOdds") or {}).get("moneyLine")}
    if out["home_spread"] is None and out["details"]:
        # e.g. "LAL -1.5": the named team is favored by the number.
        try:
            abbr, num = out["details"].rsplit(" ", 1)
            home_abbr = next(c["team"]["abbreviation"] for c in comp["competitors"] if c["homeAway"] == "home")
            out["home_spread"] = float(num) if abbr == home_abbr else -float(num)
        except (ValueError, StopIteration):
            pass
    return out


def simulate_game(pj, home, away, cutoff, tipoff, sims=20000, seed=7):
    built = pj.build_inputs(home, away, cutoff, None, None, tipoff)
    pj.calibrate_shifts(built, seed)
    return built, simulate(built["inputs"], sims, seed)


def legs_for_game(built, r, names, market, min_play=0.9, min_minutes=20.0):
    S = r["sims"]
    score = r["score"]
    margin = score[:, 0] - score[:, 1]
    total = score.sum(axis=1)
    legs = []
    home, away = (built["meta"][s]["team_id"] for s in (0, 1))
    ha, aa = names[home]["abbreviation"], names[away]["abbreviation"]
    legs.append({"kind": "game", "key": f"ml:{home}", "label": f"{ha} moneyline", "hit": margin > 0, "player": None})
    legs.append({"kind": "game", "key": f"ml:{away}", "label": f"{aa} moneyline", "hit": margin < 0, "player": None})
    if market.get("home_spread") is not None:
        hs = float(market["home_spread"])
        legs.append({"kind": "game", "key": f"sp:{home}", "label": f"{ha} {hs:+g}", "hit": margin + hs > 0, "player": None})
        legs.append({"kind": "game", "key": f"sp:{away}", "label": f"{aa} {-hs:+g}", "hit": margin + hs < 0, "player": None})
    if market.get("total"):
        t = float(market["total"])
        legs.append({"kind": "game", "key": "tot:over", "label": f"Over {t:g} total points", "hit": total > t, "player": None})
        legs.append({"kind": "game", "key": "tot:under", "label": f"Under {t:g} total points", "hit": total < t, "player": None})
    for s in (0, 1):
        b = r["box"][s]
        for j, pm in enumerate(built["meta"][s]["players"]):
            played = b["minutes"][:, j] > 0
            if pm["active_prob"] < min_play or b["minutes"][:, j].mean() < min_minutes:
                continue
            stat = {"pts": b["pts"][:, j], "reb": b["reb"][:, j], "ast": b["ast"][:, j], "fg3m": b["fg3m"][:, j]}
            stat["pra"] = stat["pts"] + stat["reb"] + stat["ast"]
            for k, ts in MILESTONES.items():
                for t in ts:
                    legs.append({"kind": "player", "key": f"{pm['id']}:{k}:{t}", "player": pm["id"], "stat": k,
                                 "label": f"{pm['name']} {t}+ {LABEL[k]}", "hit": stat[k] >= t,
                                 "played_prob": round(float(played.mean()), 4), "team": names[built['meta'][s]['team_id']]['abbreviation'],
                                 "injury_status": pm.get("injury_status")})
    for leg in legs:
        leg["p"] = float(leg["hit"].mean())
    return legs, S


def best_combo(legs, size, lo, hi, require_distinct_players=True, max_game_legs=1, pool=28):
    """Highest joint-probability combination of `size` legs whose marginals lie in [lo, hi]."""
    cands = [l for l in legs if lo <= l["p"] <= hi]
    # One threshold per (player, stat): keep the most likely so the pool stays diverse.
    seen, uniq = set(), []
    for l in sorted(cands, key=lambda x: -x["p"]):
        tag = (l.get("player"), l.get("stat")) if l["kind"] == "player" else l["key"].split(":")[0]
        if tag in seen:
            continue
        seen.add(tag)
        uniq.append(l)
    uniq = uniq[:pool]
    best = None
    for combo in itertools.combinations(uniq, size):
        if sum(l["kind"] == "game" for l in combo) > max_game_legs:
            continue
        players = [l["player"] for l in combo if l["player"]]
        if require_distinct_players and len(players) != len(set(players)):
            continue
        if len({l.get("stat") for l in combo if l["kind"] == "player"}) < min(2, len(players)):
            continue  # mix statistic types
        hit = np.logical_and.reduce([l["hit"] for l in combo])
        p = float(hit.mean())
        if best is None or p > best[0]:
            best = (p, combo)
    return best


def best_correlated(legs, size, joint_lo=.25, joint_hi=.45, leg_lo=.45, leg_hi=.85, max_game_legs=1, pool=30):
    """Within a target payout band, the combination whose legs move together most:
    maximize joint probability / product of marginals (correlation lift)."""
    cands = [l for l in legs if leg_lo <= l["p"] <= leg_hi]
    seen, uniq = set(), []
    for l in sorted(cands, key=lambda x: -x["p"]):
        tag = (l.get("player"), l.get("stat")) if l["kind"] == "player" else l["key"].split(":")[0]
        if tag in seen:
            continue
        seen.add(tag)
        uniq.append(l)
    # Keep game legs in the pool even if many player legs exist.
    games = [l for l in uniq if l["kind"] == "game"]
    uniq = [l for l in uniq if l["kind"] == "player"][:pool - len(games)] + games
    best = None
    for combo in itertools.combinations(uniq, size):
        if sum(l["kind"] == "game" for l in combo) > max_game_legs:
            continue
        players = [l["player"] for l in combo if l["player"]]
        if len(players) != len(set(players)):
            continue
        hit = np.logical_and.reduce([l["hit"] for l in combo])
        p = float(hit.mean())
        if not joint_lo <= p <= joint_hi:
            continue
        lift = p / math.prod(l["p"] for l in combo)
        if best is None or lift > best[2]:
            best = (p, combo, lift)
    return best[:2] if best else None


def ticket(p, combo, S, scope):
    indep = math.prod(l["p"] for l in combo)
    se = math.sqrt(max(p * (1 - p), 1e-12) / S)
    return {"scope": scope, "legs": [{"label": l["label"], "probability": round(l["p"], 4), "fair_odds": american(l["p"]),
                                      "injury_status": l.get("injury_status")} for l in combo],
            "probability": round(p, 4), "probability_mc_se": round(se, 4), "fair_american_odds": american(p),
            "fair_decimal_odds": round(1 / p, 3) if p > 0 else None,
            "independent_product": round(indep, 4), "correlation_lift": round(p / indep, 3) if indep else None}


def build_slate(pj, events, names, cutoff=None, sims=20000, seed=7, progress=print):
    now = datetime.now(timezone.utc)
    cutoff = M.iso(cutoff or now)
    games = []
    for e in events:
        comp = e["competitions"][0]
        t = {c["homeAway"]: c for c in comp["competitors"]}
        home, away = t["home"]["team"]["id"], t["away"]["team"]["id"]
        if comp["status"]["type"].get("completed"):
            continue
        market = market_lines(e)
        built, r = simulate_game(pj, home, away, cutoff, e["date"], sims, seed)
        legs, S = legs_for_game(built, r, names, market)
        margin = r["score"][:, 0] - r["score"][:, 1]
        g = {"event_id": e["id"], "tipoff": e["date"], "home": names[home]["abbreviation"], "away": names[away]["abbreviation"],
             "market": market, "sims": S, "home_win_prob": round(float((margin > 0).mean()), 4),
             "proj_score": {names[away]["abbreviation"]: round(float(r["score"][:, 1].mean()), 1), names[home]["abbreviation"]: round(float(r["score"][:, 0].mean()), 1)},
             "proj_total": round(float(r["score"].sum(1).mean()), 1), "injury_reports": [m.get("injury_report") for m in built["meta"]],
             "outs": [pm["name"] for m in built["meta"] for pm in m["players"] if pm.get("injury_status") in ("Out", "Doubtful")],
             "questionable": [pm["name"] for m in built["meta"] for pm in m["players"] if pm.get("injury_status") == "Questionable"]}
        if market.get("home_ml") is not None:
            g["market_home_win_prob_devig"] = round(implied(market["home_ml"]) / (implied(market["home_ml"]) + implied(market["away_ml"])), 4)
        if market.get("home_spread") is not None:
            g["model_cover_home"] = round(float((margin + float(market["home_spread"]) > 0).mean()), 4)
        if market.get("total"):
            g["model_over"] = round(float((r["score"].sum(1) > float(market["total"])).mean()), 4)
        sgp3 = best_combo(legs, 3, .62, .86)
        sgp4 = best_combo(legs, 4, .70, .90)
        corr3 = best_correlated(legs, 3)
        g["sgp_3"] = ticket(*sgp3, S, "same-game") if sgp3 else None
        g["sgp_4"] = ticket(*sgp4, S, "same-game") if sgp4 else None
        g["sgp_correlated"] = ticket(*corr3, S, "same-game") if corr3 else None
        g["top_legs"] = [{"label": l["label"], "probability": round(l["p"], 4), "fair_odds": american(l["p"])}
                         for l in sorted([l for l in legs if l["kind"] == "player" and .7 <= l["p"] <= .92], key=lambda x: -x["p"])[:8]]
        g["_legs"] = legs
        games.append(g)
        progress(f"  {g['away']} @ {g['home']}: home win {g['home_win_prob']:.3f}, SGP3 {g['sgp_3']['probability'] if g['sgp_3'] else None}")
    # Cross-game tickets: best single player leg per game in a moderate band, games independent.
    picks = []
    for g in games:
        legs = [l for l in g["_legs"] if l["kind"] == "player" and .74 <= l["p"] <= .88]
        if legs:
            best = max(legs, key=lambda l: l["p"])
            picks.append((g, best))
    picks.sort(key=lambda x: -x[1]["p"])
    cross = {}
    for size in (3, 5, 8):
        if len(picks) >= size:
            chosen = picks[:size]
            p = math.prod(l["p"] for _, l in chosen)
            cross[f"{size}_leg"] = {"scope": "cross-game", "legs": [{"game": f"{g['away']} @ {g['home']}", "label": l["label"],
                                                                     "probability": round(l["p"], 4), "fair_odds": american(l["p"])} for g, l in chosen],
                                    "probability": round(p, 4), "fair_american_odds": american(p), "fair_decimal_odds": round(1 / p, 3)}
    for g in games:
        g.pop("_legs")
    return {"created_at": now.isoformat(), "cutoff": cutoff, "sims_per_game": sims, "seed": seed, "games": games,
            "cross_game": cross,
            "method": "Joint probabilities from shared possession simulations; same-game legs priced from the same simulated games; "
                      "player milestones assume the player plays (only players with >= 90% play probability and 20+ projected minutes).",
            "warning": "Research only. No sportsbook player-prop prices were available; fair odds are the break-even price. "
                       "The model trails the closing market in walk-forward tests."}


def to_markdown(slate):
    lines = [f"# NBA parlays — {slate['created_at'][:10]}", "",
             f"Information cutoff {slate['cutoff']} · {slate['sims_per_game']:,} simulations per game · seed {slate['seed']}", "",
             f"> {slate['warning']}", ""]
    for g in slate["games"]:
        m = g["market"]
        lines += [f"## {g['away']} @ {g['home']} — {g['tipoff']}", "",
                  f"Projected {g['away']} {g['proj_score'][g['away']]} – {g['proj_score'][g['home']]} {g['home']} "
                  f"(total {g['proj_total']}); model {g['home']} win {g['home_win_prob']:.1%}"
                  + (f" vs market {g['market_home_win_prob_devig']:.1%}" if g.get("market_home_win_prob_devig") else "")
                  + f". Line: {m.get('details')} / O/U {m.get('total')}.", ""]
        if g.get("outs"):
            lines.append(f"Out/doubtful: {', '.join(g['outs'])}. ")
        if g.get("questionable"):
            lines.append(f"Questionable: {', '.join(g['questionable'])}.")
        lines.append("")
        for key, title in (("sgp_3", "Core 3-leg same-game (highest joint probability)"),
                           ("sgp_4", "Core 4-leg same-game"),
                           ("sgp_correlated", "Correlated 3-leg same-game (largest correlation lift at +120 to +300)")):
            t = g.get(key)
            if not t:
                continue
            lines += [f"**{title}** — model {t['probability']:.1%} (±{t['probability_mc_se']:.1%}), fair odds {t['fair_american_odds']:+d}, "
                      f"correlation lift ×{t['correlation_lift']}", ""]
            lines += [f"- {l['label']} ({l['probability']:.0%})" for l in t["legs"]]
            lines.append("")
    for key, t in slate["cross_game"].items():
        lines += [f"## Cross-game {key.replace('_', '-')} — model {t['probability']:.1%}, fair odds {t['fair_american_odds']:+d}", ""]
        lines += [f"- {l['game']}: {l['label']} ({l['probability']:.0%})" for l in t["legs"]]
        lines.append("")
    return "\n".join(lines)


def save(slate, out_dir="output/parlays"):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stamp = slate["created_at"][:16].replace(":", "")
    jp, mp = Path(out_dir) / f"parlays-{stamp}.json", Path(out_dir) / f"parlays-{stamp}.md"
    jp.write_text(json.dumps(slate, indent=1))
    mp.write_text(to_markdown(slate))
    return jp, mp
