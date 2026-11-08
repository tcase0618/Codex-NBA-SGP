"""Explicitly uncalibrated live leg scenarios; no independence SGP product."""
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.stats import nbinom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nba_quant.board import merged_history
from nba_quant.schemas import utc
from nba_quant.store import Store

OUT = ROOT / "output/review-2026-11-04"


def remaining_regulation(period, clock):
    if isinstance(period, bool) or not isinstance(period, int) or not 1 <= period <= 24 or not isinstance(clock, str):
        raise ValueError("Valid source period and game clock required")
    if ":" in clock:
        a, b = clock.split(":"); seconds = int(a) * 60 + float(b)
    else:
        seconds = float(clock)
    if not math.isfinite(seconds) or not 0 <= seconds <= (720 if period <= 4 else 300):
        raise ValueError("Source game clock is out of range")
    return max(0, (4 - period) * 12 + seconds / 60) if period <= 4 else seconds / 60


def main():
    report = json.loads((OUT / "graded-review.json").read_text(encoding="utf-8"))
    frozen = json.loads((OUT / "frozen-input.json").read_text(encoding="utf-8"))
    history = merged_history(Store(ROOT / "data/nba_quant.sqlite"))
    cutoff = utc(frozen["scan_meta"]["scanned_at"])
    method = {"status": "UNCALIBRATED_CONDITIONAL_SCENARIOS", "family_point_probability": None,
        "formula": "Gamma-Poisson posterior predictive per leg; Frechet bounds across legs, never independent multiplication",
        "prior": "Latest 10 completed positive-minute current-team games known before frozen PDF scan; prior rate has 20 minute pseudo-exposure, plus 0.5 count",
        "playing_time": "Low:0.5x, central:1x, high:1.5x historical minutes/48 participation share times remaining regulation clock, capped at clock",
        "assumptions": ["No new injury, ejection or rotation change; game continues with comparable role/event rate.",
            "Rounded current minutes accepted as source precision; overtime and late-game tactical changes excluded.",
            "Counts treated as stationary Poisson events conditional on uncertain rate; scoring clusters, defense, usage and shared opportunities are not modeled.",
            "Displayed bounds are scenario/dependence bounds, NOT confidence intervals and NOT validated live SGP probabilities.",
            "No sportsbook market/void/settlement probabilities are inferred."]}
    for game in report["games"]:
        if game["box"]["completed"]:
            continue
        clock = remaining_regulation(game["box"]["period"], game["box"]["clock"])
        cache = {}
        for family in game["families"]:
            probs = []
            complete = True
            for leg in family["legs"]:
                if leg["status"] == "PASS":
                    probs.append((1., 1., 1.)); continue
                if leg["status"] == "FAIL":
                    probs.append((0., 0., 0.)); continue
                if leg["status"] != "PENDING" or leg.get("minutes") is None:
                    complete = False; continue
                key = (leg["player_id"], leg["team_id"], leg["stat"], leg["side"], leg["line"])
                if key not in cache:
                    rows = [(g, b) for g in history["games"] if utc(g["known_at"]) <= cutoff and utc(g["completed_at"]) < cutoff
                        for b in g["boxes"] if b["player_id"] == leg["player_id"] and b["team_id"] == leg["team_id"] and b["minutes"] > 0]
                    rows.sort(key=lambda gb: utc(gb[0]["tipoff"]), reverse=True); rows = rows[:10]
                    if len(rows) < 3:
                        cache[key] = None
                    else:
                        counts = [b[leg["stat"]] if leg["stat"] != "reb" else b["oreb"] + b["dreb"] for _, b in rows]
                        exposure = sum(b["minutes"] for _, b in rows)
                        rate = sum(counts) / exposure
                        mean_minutes = exposure / len(rows)
                        mid = min(clock, clock * mean_minutes / 48)
                        scenarios = [max(0, .5 * mid), mid, min(clock, 1.5 * mid)]
                        shape = rate * 20 + leg["actual"] + .5
                        beta = 20 + leg["minutes"]
                        needed = math.ceil(leg["line"] - leg["actual"])
                        values = [float(nbinom.sf(needed - 1, shape, beta / (beta + minutes))) if minutes > 0 else 0 for minutes in scenarios]
                        cache[key] = {"conditional_low": values[0], "conditional_central": values[1], "conditional_high": values[2],
                            "remaining_minutes_scenarios": [round(x, 2) for x in scenarios], "historical_games": len(rows),
                            "prior_rate_per_minute": rate, "required_additional": needed,
                            "status": "ROUGH_UNCALIBRATED", "history_game_ids": [g["id"] for g, _ in rows]}
                estimate = cache[key]
                leg["live_estimate"] = estimate
                if estimate:
                    probs.append(tuple(estimate[x] for x in ("conditional_low", "conditional_central", "conditional_high")))
                else:
                    complete = False
            if family["status"] == "PENDING" and complete:
                low = max(0., sum(p[0] for p in probs) - (len(probs) - 1))
                high = min(p[2] for p in probs)
                central_upper = min(p[1] for p in probs)
                central_lower = min(central_upper, max(0., sum(p[1] for p in probs) - (len(probs) - 1)))
                family["conditional_probability_bounds"] = {"low": low, "high": high,
                    "central_minutes_dependence_bounds": [central_lower, central_upper],
                    "label": "Uncalibrated scenario/dependence range; no fitted joint hit probability"}
    report["live_probability_method"] = method
    (OUT / "graded-review-live.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for g in report["games"]:
        if not g["box"]["completed"]:
            print(g["game"])
            for f in g["families"]:
                print(f["size"], f["status"],f.get("conditional_probability_bounds"))


if __name__ == "__main__":
    main()
