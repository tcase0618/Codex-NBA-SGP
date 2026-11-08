"""Point-forecast agreement screens for shadow research combinations, never P(hit)."""
import math

from .line_policy import line_policy_reason

STATS = ("pts", "reb", "ast", "fg3m")
VERSION = "three-model-agreement-sweep-0.1.0"


def select_prediction_family(candidates, size, teams):
    if size not in (3, 5, 8) or len(set(teams)) != 2:
        raise ValueError("Use3/5/8 legs from one exact two-team game")
    grouped, rejected = {}, []
    for leg in candidates:
        evidence = leg.get("prediction_evidence", {})
        values = evidence.get("point_predictions", {})
        scale = evidence.get("tuning_rmse_scale")
        if (set(values) != {"autogluon", "xgboost", "historical_baseline"}
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values.values())
            or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0):
            rejected.append({"player_id": leg.get("player_id"), "reason": "Missing finite predictions or tuning scale"})
            continue
        if leg.get("stat") not in STATS or leg.get("team_id") not in teams or not leg.get("coverage_eligible"):
            continue
        if line_policy_reason(leg.get("side"), leg.get("offer")):
            continue
        # These are numerical point gaps, not quantiles, odds, calibrated z-scores,
        # model independence, or evidence that P(over)>0.5.
        gap = (min(values.values())-leg["line"] if leg["side"] == "over"
               else leg["line"]-max(values.values()) if leg["side"] == "under" else -math.inf)
        if gap <= 0:
            rejected.append({"player_id": leg["player_id"], "stat": leg["stat"], "line": leg["line"],
                "reason": "At least one model point estimate does not clear this line", "point_predictions": values})
            continue
        candidate = {**leg, "prediction_evidence": {**evidence, "minimum_point_gap": gap,
            "normalized_point_gap": gap/scale, "status": "UNCALIBRATED_SHADOW_AGREEMENT",
            "calibrated_probability": None, "verified_expected_value": None}}
        grouped.setdefault(leg["player_id"], []).append(candidate)
    target, cap = {3: 1, 5: 2, 8: 3}[size], math.ceil(size/2)
    # One pass per player prevents a player supplying multiple legs. DP explores
    # source-qualified alternatives rather than depending on greedy feasibility.
    states = {((0, 0, 0, 0), 0, 0): (0., 0., ())}
    for pid in sorted(grouped):
        updated = dict(states)
        for (counts, mask, anchors), (anchor_score, support_score, selected) in states.items():
            if sum(counts) >= size:
                continue
            for leg in grouped[pid]:
                i = STATS.index(leg["stat"])
                if counts[i] >= cap:
                    continue
                roles = ["support"]
                if leg["offer"]["decimal_odds"] >= 2 and anchors < target:
                    roles.append("anchor")
                for role in roles:
                    point_score = leg["prediction_evidence"]["normalized_point_gap"]
                    ncounts = list(counts); ncounts[i] += 1
                    key = (tuple(ncounts), mask | (1 if leg["team_id"] == teams[0] else 2), anchors+int(role == "anchor"))
                    record = (anchor_score+(point_score if role == "anchor" else 0), support_score+point_score,
                              selected+({**leg, "family_role": role},))
                    if key not in updated or record[:2] > updated[key][:2]:
                        updated[key] = record
        states = updated
    eligible = [(key, val) for key, val in states.items() if sum(key[0]) == size]
    if not eligible:
        return {"leg_count": size, "legs": [], "status": "BLOCKED_MODEL_AGREEMENT_COVERAGE",
            "distinct_agreeing_players": len(grouped), "rejected": rejected, "joint_probability": None,
            "combined_odds": None, "expected_value": None, "version": VERSION}
    key, val = max(eligible, key=lambda item: (item[0][2], item[1][0], item[1][1], item[0][1] == 3))
    return {"leg_count": size, "legs": list(val[2]), "status": "SHADOW_RESEARCH_ONLY", "version": VERSION,
        "anchors_target": target, "anchors_selected": key[2], "anchor_shortfall": target-key[2],
        "stat_cap": cap, "distinct_agreeing_players": len(grouped), "rejected": rejected,
        "joint_probability": None, "combined_odds": None, "expected_value": None,
        "policy": "Fixed point-agreement/plus-price anchor policy; not optimized on tonight's outcomes or a calibrated hit probability"}
