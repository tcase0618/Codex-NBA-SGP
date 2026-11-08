"""Complete family-specific composition of source-validated offered alternatives.

Roles describe research construction preferences, not approved price edges or
independent events. The log-support objective must never be exponentiated and
presented as an estimated joint probability.
"""
import hashlib
import json
import math
from .line_policy import line_policy_reason

STATS = ("pts", "reb", "ast", "fg3m")
VERSION = "anchor-support-family-0.3.0"
ANCHORS = {3: 1, 5: 2, 8: 3}


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _candidate(candidate, teams):
    if not isinstance(candidate, dict):
        raise ValueError("Each family alternative must be a source-validated candidate")
    pid, team, stat = candidate.get("player_id"), candidate.get("team_id"), candidate.get("stat")
    if not isinstance(pid, str) or not pid or team not in teams or stat not in STATS:
        raise ValueError("Candidate player, game team or statistic is invalid")
    offer = candidate.get("offer")
    reason = line_policy_reason(candidate.get("side"), offer)
    if reason:
        raise ValueError(reason)
    if not isinstance(offer, dict) or not _number(offer.get("decimal_odds")) or offer["decimal_odds"] <= 1:
        raise ValueError("Each alternative requires actual finite offered decimal odds above one")
    evidence = candidate.get("aggressive_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("Research probability evidence is required for each alternative")
    p, rank = evidence.get("uncertainty_adjusted_probability"), evidence.get("ranking_score")
    if not _number(p) or not 0 <= p <= 1 or not _number(rank):
        raise ValueError("Candidate support probability and ranking score must be finite and bounded")
    net = evidence.get("proxy_return_after_entry_fee")
    gross_fallback = net is None and evidence.get("ranking_basis") == "GROSS_RESEARCH_ONLY_FEES_UNAVAILABLE"
    mean = evidence.get("proxy_return_gross") if gross_fallback else net
    if not _number(mean):
        raise ValueError("Known-entry-fee mean proxy or explicitly gross-only research basis is required")
    fingerprint = hashlib.sha256(json.dumps(candidate, sort_keys=True, separators=(",", ":"),
                                            allow_nan=False, default=str).encode()).hexdigest()
    return {"candidate": candidate, "player": pid, "team": team, "stat": STATS.index(stat),
            "support": p, "rank": rank, "mean": mean, "gross_fallback": gross_fallback,
            "anchor_eligible": offer["decimal_odds"] >= 2 and mean > 0,
            "key": fingerprint}


def _better(left, right):
    if right is None:
        return True
    if (left[0], left[1]) != (right[0], right[1]):
        return (left[0], left[1]) > (right[0], right[1])
    return left[2] < right[2]


def select_family(candidates, size, team_ids):
    """Select exactly 3/5/8 distinct players within the exact game's roster pool.

    Each player/stat keeps its best anchor and best support alternative. Exact
    dynamic programming then maximizes achieved anchor count up to the target,
    anchor score sum, and log marginal support, in that order. Negative mean
    research edges can be support legs and are explicitly disclosed; absence of
    positive price edges alone never makes a feasible coverage family fail.

    Source identity, quote freshness/depth, market rules, roster availability and
    history gates are mandatory caller responsibilities. This function validates
    numeric evidence and composition and never fetches or invents any offer.
    """
    if isinstance(size, bool) or size not in ANCHORS:
        raise ValueError("Family size must be 3, 5 or 8")
    if not isinstance(team_ids, (list, tuple, set, frozenset)) or len(team_ids) != 2 or any(not isinstance(t, str) or not t for t in team_ids):
        raise ValueError("Two distinct exact game team IDs are required")
    teams = tuple(sorted(team_ids))
    if teams[0] == teams[1]:
        raise ValueError("Two distinct exact game team IDs are required")
    if not isinstance(candidates, (list, tuple)) or len(candidates) > 10000:
        raise ValueError("A bounded list of at most 10000 offered alternatives is required")
    items = [_candidate(c, teams) for c in candidates]
    by_player, reductions = {}, {}
    for item in items:
        for role in ("support", "anchor"):
            if role == "anchor" and not item["anchor_eligible"]:
                continue
            key = (item["player"], item["stat"], role)
            prior = reductions.get(key)
            score = (item["rank"], item["support"]) if role == "anchor" else (item["support"],)
            prior_score = ((prior["rank"], prior["support"]) if role == "anchor" else (prior["support"],)) if prior else None
            if prior is None or score > prior_score or (score == prior_score and item["key"] < prior["key"]):
                reductions[key] = item
    for (player, _, role), item in reductions.items():
        by_player.setdefault(player, []).append((role, item))
    target = ANCHORS[size]
    stat_cap = math.ceil(size / 2)
    # State = (four stat counts, team bit mask, anchor count). Player iteration
    # guarantees one selected alternative at most per player.
    states = {((0, 0, 0, 0), 0, 0): (0., 0., (), ())}
    explored = 0
    for player in sorted(by_player):
        options = sorted(by_player[player], key=lambda option: (option[0], option[1]["key"]))
        updated = dict(states)
        for (counts, mask, anchors), record in states.items():
            if sum(counts) >= size:
                continue
            for role, item in options:
                explored += 1
                next_anchors = anchors + (role == "anchor")
                if next_anchors > target or counts[item["stat"]] >= stat_cap:
                    continue
                next_counts = list(counts)
                next_counts[item["stat"]] += 1
                state = (tuple(next_counts), mask | (1 << teams.index(item["team"])), next_anchors)
                rank_sum = record[0] + (item["rank"] if role == "anchor" else 0.)
                log_sum = record[1] + math.log(max(item["support"], 1e-12))
                choice_key = f"{item['key']}:{role}"
                proposal = (rank_sum, log_sum, record[2] + (choice_key,), record[3] + ((role, item),))
                if _better(proposal, updated.get(state)):
                    updated[state] = proposal
        states = updated
    best, achieved, best_mask = None, -1, 0
    for (counts, mask, anchors), record in states.items():
        if sum(counts) != size:
            continue
        objective = (anchors, record[0], record[1], mask == 3)
        best_objective = (achieved, best[0], best[1], best_mask == 3) if best is not None else None
        if (best is None or objective > best_objective
            or (objective == best_objective and record[2] < best[2])):
            best, achieved, best_mask = record, anchors, mask
    diagnostics = {
        "version": VERSION, "status": "BLOCKED_INSUFFICIENT_FEASIBLE_SOURCE_COVERAGE" if best is None else "BEST_AVAILABLE_COVERAGE",
        "requested_leg_count": size, "anchors_target": target, "anchors_achieved": max(0, achieved),
        "anchor_target": target, "anchors_selected": max(0, achieved), "stat_cap": stat_cap,
        "anchor_shortfall": target - max(0, achieved),
        "input_alternatives": len(items), "distinct_players": len(by_player),
        "role_alternatives_after_reduction": len(reductions), "states_retained": len(states),
        "transitions_examined": explored, "joint_probability": None, "expected_return_per_unit": None,
        "combined_odds": None,
        "objective": "Anchor target coverage, anchor uncertainty-score sum, log marginal support, then two-team representation as an objective tie-break only; not a joint probability",
        "source_quality_scope": "Exact source/market/quote freshness/depth/history/eligibility gates are validated by the caller; no source is refreshed here",
        "constraints": "One alternative per player; at most ceil(family_size/2) per statistic; all players belong to the exact game; team diversity is a preference, not an eligibility rule",
        "limitations": [
            "Research composition only; source-backed coverage is not approval of every selected price edge.",
            "Support legs may have nonpositive mean research price edges; those are retained explicitly for coverage.",
            "Anchor/support probabilities and scores are uncalibrated research assumptions.",
            "Log marginal support is an optimization objective and must never be shown as a joint hit probability.",
            "No combined quote, independent probability product, guaranteed win, or stake instruction is supplied.",
        ],
    }
    if best is None:
        diagnostics.update(negative_mean_proxy_legs=0, nonpositive_mean_proxy_legs=0,
                           lack_positive_uncertainty_legs=0, nonpositive_uncertainty_legs=0,
                           min_probability=None, sum_log_support=None, team_counts={t: 0 for t in teams},
                           same_team_only=False, distinct_stats=0)
        return {"legs": [], "diagnostics": diagnostics}
    selected = list(best[3])
    selected.sort(key=lambda pair: (pair[0] != "anchor", -pair[1]["rank"] if pair[0] == "anchor" else -pair[1]["support"], pair[1]["key"]))
    legs = [{**item["candidate"], "family_role": role} for role, item in selected]
    diagnostics.update(
        negative_mean_proxy_legs=sum(item["mean"] < 0 for _, item in selected),
        nonpositive_mean_proxy_legs=sum(item["mean"] <= 0 for _, item in selected),
        lack_positive_uncertainty_legs=sum(item["rank"] <= 0 for _, item in selected),
        nonpositive_uncertainty_legs=sum(item["rank"] <= 0 for _, item in selected),
        gross_only_fee_unknown_legs=sum(item["gross_fallback"] for _, item in selected),
        min_probability=min(item["support"] for _, item in selected),
        sum_log_support=best[1], anchor_score_sum=best[0],
        anchor_source_players=[item["player"] for role, item in selected if role == "anchor"],
        team_counts={team: sum(item["team"] == team for _, item in selected) for team in teams},
        same_team_only=best_mask != 3, distinct_stats=len({item["stat"] for _, item in selected}),
        price_sources=sorted({str(item["candidate"]["offer"].get("sportsbook") or item["candidate"]["offer"].get("provider") or "UNSPECIFIED") for _, item in selected}),
    )
    return {"legs": legs, "diagnostics": diagnostics}
