"""Deterministic source-backed analytical draft families, never offered SGPs.

Historical frequency is descriptive, not a calibrated next-game probability.
No marginal probabilities or individual prices are multiplied into a ticket.
"""
import math
from collections import Counter
from datetime import datetime, timezone

from .schemas import utc
from .sgp import wilson
from .store import digest
from .line_policy import line_policy_reason, VERSION as LINE_POLICY_VERSION

VERSION = "analytical-draft-0.7.0"
LADDERS = {"pts": tuple(range(10, 61, 5)), "reb": (2, 4, 6, 8, 10, 12, 14, 16, 18, 20),
           "ast": (2, 4, 6, 8, 10, 12, 14, 16), "fg3m": (1, 2, 3, 4, 5, 6, 7, 8)}
MIN_GAMES = 3
MIN_MINUTES = 18
MIN_FREQUENCY = .8
MAX_CONTEXT_AGE = 21600
MAX_PRICE_AGE = 300


def _offered_alternative(base, row):
    """Reuse source history without mutating the base or copying entire ladders."""
    reason = line_policy_reason(row["side"], row["offer"])
    if reason:
        raise ValueError(reason)
    candidate = {**base, "side": row["side"], "line": row["line"], "offer": row["offer"]}
    hits, games = row["hits"], row["sample_games"]
    candidate["descriptive"] = {**base["descriptive"], "hits": hits, "hit_rate": hits / games,
        "interval_95": wilson(hits, games), "smoothed_frequency": (hits + 1) / (games + 2)}
    candidate["aggressive_evidence"] = {k: v for k, v in row.items() if k != "offer"}
    candidate["family_eligible"] = row["eligible"] and row["ranking_score"] > 1e-12
    candidate["coverage_eligible"] = row.get("evidence_eligible") is True
    candidate["selection_basis"] = "Verified offered alternative evaluated for a size-specific anchor/support research draft; negative price-edge estimates are disclosed, not certified as value."
    candidate["id"] = digest({"base": base["id"], "side": row["side"], "line": row["line"], "version": VERSION})[:24]
    return candidate


def _blank_family(size, reasons):
    return {"leg_count": size, "status": "BLOCKED", "legs": [], "actionable": False,
            "joint_probability": None, "expected_return_per_unit": None,
            "combined_odds": None, "block_reasons": list(reasons)}


def _offered_rows(rows, player_id, team_id, stat, game, as_of):
    valid = []
    for row in rows:
        if row.get("player_id") != player_id or row.get("team_id") != team_id or row.get("stat") != stat:
            continue
        try:
            if not (row.get("event_id") == game["id"] or (
                row.get("home_id") == game["home"]["id"] and row.get("away_id") == game["away"]["id"]
                and utc(row["tipoff"]) == utc(game["tipoff"]))):
                continue
            if ((row.get("home_id") is not None and row["home_id"] != game["home"]["id"])
                or (row.get("away_id") is not None and row["away_id"] != game["away"]["id"])
                or (row.get("tipoff") is not None and utc(row["tipoff"]) != utc(game["tipoff"]))):
                continue
            observed, retrieved = utc(row["observed_at"]), utc(row["retrieved_at"])
            price, line = row["decimal_odds"], row["line"]
            if not observed <= retrieved <= as_of or (as_of - observed).total_seconds() > MAX_PRICE_AGE:
                continue
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (price, line)):
                continue
            if price <= 1 or line < 0:
                continue
            side = row.get("side")
            if line_policy_reason(side, row):
                continue
            if side == "at_least":
                if line != math.floor(line):
                    continue
            elif side in {"over", "under"}:
                if not math.isclose(line % 1, .5, abs_tol=1e-9):
                    continue
            else:
                continue
            if not row.get("sportsbook", "").strip():
                continue
            if row.get("sportsbook", "").lower() == "polymarket" and not (
                row.get("quote_basis") == "ORDERBOOK_BEST_ASK" and row.get("executable") is True
            ):
                continue
            if not (row.get("source_url") or row.get("source_reference")) or not row.get("source_sha256"):
                continue
            valid.append(row)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return valid


def _select(candidates, size, team_ids):
    """Find a feasible family, preserving deterministic balance preferences.

    The Wilson lower bound ranks descriptive sample support. It is a screening
    heuristic, not a calibrated probability or a post-selection confidence claim.
    Backtracking is necessary: a flexible player's first stat choice can otherwise
    consume the only player available to fill another required stat slot.
    """
    candidates = [c for c in candidates if c["team_id"] in team_ids and c["stat"] in LADDERS]
    stat_names = tuple(LADDERS)
    team_names = tuple(sorted(team_ids))
    lower_bounds = {
        id(c): wilson(c["descriptive"].get("hits", c["descriptive"]["hit_rate"] * c["descriptive"]["games"]),
                      c["descriptive"]["games"])[0]
        for c in candidates
    }
    failed = set()

    def search(selected, players, stats, teams):
        needed = size - len(selected)
        if not needed:
            return selected if set(teams) == set(team_names) else None
        state = (frozenset(players), tuple(stats[s] for s in stat_names), tuple(teams[t] for t in team_names))
        if state in failed:
            return None
        available = [c for c in candidates if c["player_id"] not in players and stats[c["stat"]] < 2]
        missing_teams = set(team_names) - set(teams)
        capacity = sum(min(2 - stats[s], len({c["player_id"] for c in available if c["stat"] == s}))
                       for s in stat_names)
        if (len({c["player_id"] for c in available}) < needed or capacity < needed
            or len(missing_teams) > needed
            or not missing_teams <= {c["team_id"] for c in available}):
            failed.add(state)
            return None
        available.sort(key=lambda c: (teams[c["team_id"]], stats[c["stat"]],
            -c.get("aggressive_evidence", {}).get("ranking_score", c.get("aggressive_evidence", {}).get("proxy_return_gross", 0.)) if c.get("selection_mode") == "aggressive" else 0.,
            -lower_bounds[id(c)], -c["descriptive"]["games"], -c["descriptive"]["hit_rate"],
            -c["minutes"]["mean"], stat_names.index(c["stat"]), c["player_id"],
            c.get("side", ""), c.get("line", 0)))
        for candidate in available:
            next_stats, next_teams = stats.copy(), teams.copy()
            next_stats[candidate["stat"]] += 1
            next_teams[candidate["team_id"]] += 1
            result = search(selected + [candidate], players | {candidate["player_id"]}, next_stats, next_teams)
            if result is not None:
                return result
        failed.add(state)
        return None

    return search([], set(), Counter(), Counter()) or []


def construct_game_drafts(game, dataset, context_players, offered_props=None, as_of=None, selection_mode="standard"):
    if selection_mode not in {"standard", "aggressive"}:
        raise ValueError("Selection mode must be standard or aggressive")
    actual_now = datetime.now(timezone.utc)
    as_of = utc(as_of) if as_of is not None else actual_now
    if as_of > actual_now:
        raise ValueError("Draft evaluation time cannot be in the future")
    home, away = game["home"]["id"], game["away"]["id"]
    tipoff = utc(game["tipoff"])
    if home == away:
        raise ValueError("Draft game requires two distinct teams")
    blockers = [
        "Analytical draft only; thresholds are not sportsbook offers unless explicitly source-bound.",
        "Historical hit frequency is descriptive and is not a calibrated next-game probability.",
        "Current starting lineups, injury eligibility and minutes restrictions require verified game-specific review.",
        "Exact combined sportsbook ticket quote is unavailable; individual leg prices are never multiplied.",
        "Shared scenario-conditioned joint forecasts and independent prospective SGP calibration are unavailable.",
        "Book-specific market eligibility, DNP, void and settlement rules are unvalidated.",
        "Under selections require source-identified standard lines; alternate lines are over-only.",
    ]
    if selection_mode == "aggressive":
        blockers.extend([
            "Aggressive mode requires fresh matched offered lines; no unpriced threshold fallback is allowed.",
            "Aggressive ranking uses a market-shrunk uncertainty-adjusted research proxy, with known entry fees; it is not a calibrated edge and excludes residual slippage/gas.",
        ])
    provenance = {"construction_version": VERSION, "game_id": game["id"], "home_id": home,
        "line_policy_version": LINE_POLICY_VERSION,
        "away_id": away, "tipoff": tipoff.isoformat(), "as_of": as_of.isoformat(),
        "dataset_id": dataset.get("id") if dataset else None,
        "source": dataset.get("source") if dataset else None,
        "source_url": dataset.get("source_url") if dataset else None,
        "sample_policy": {"minimum_games": MIN_GAMES, "minimum_mean_minutes": MIN_MINUTES,
            "window": "latest 10 positive-minute appearances for the current team",
            "descriptive_frequency_floor": MIN_FREQUENCY,
            "current_roster_max_age_seconds": MAX_CONTEXT_AGE},
        "selection_mode": selection_mode,
        "policy_note": "Screening choices are fixed research policies, not empirically calibrated confidence gates."}
    if selection_mode == "aggressive":
        provenance["sample_policy"].update(minimum_games=5, descriptive_frequency_floor=.4,
            minimum_historical_hits=2, positive_uncertainty_score_required_for_sizes=[3],
            actual_offered_quotes_required=True)
        provenance["nightly_priority_sizes"] = [5, 8]
        provenance["coverage_policy"] = "Five/eight-leg research drafts may use nonpositive-price-edge supports if quote/history quality gates pass; this is disclosed and never an approved betting edge."
    fatal = []
    if dataset is None or dataset.get("synthetic") is not False:
        fatal.append("Authentic non-synthetic historical dataset is required; no synthetic fallback is used.")
    if as_of >= tipoff or game.get("completed") or game.get("phase") in {"in", "post"}:
        fatal.append("Pregame construction window is closed; live SGP construction is unavailable.")
    if dataset and not {home, away} <= {t["id"] for t in dataset.get("teams", [])}:
        fatal.append("Historical dataset does not contain both exact game team identities.")
    if dataset and dataset.get("retrieved_at") and utc(dataset["retrieved_at"]) > as_of:
        fatal.append("Historical source retrieval follows the draft evaluation cutoff.")
    if fatal:
        return {"families": [{**_blank_family(n, fatal + blockers), "selection_mode": selection_mode} for n in (3, 5, 8)], "candidates": [],
                "block_reasons": fatal + blockers, "actionable": False, "use_status": "RESEARCH_ONLY",
                "selection_mode": selection_mode, "provenance": provenance, "joint_probability": None, "expected_return_per_unit": None}
    context_ids = [p.get("id") for p in context_players]
    if len(context_ids) != len(set(context_ids)):
        raise ValueError("Ambiguous duplicate current-roster player identities")
    historical_ids = {p["id"] for p in dataset.get("players", [])}
    games = {}
    for row in dataset.get("games", []):
        if utc(row["completed_at"]) < as_of and utc(row["known_at"]) <= as_of and utc(row["tipoff"]) < tipoff:
            if row["id"] in games and digest(games[row["id"]]) != digest(row):
                raise ValueError("Conflicting historical game revisions require resolution before construction")
            games[row["id"]] = row
    candidates, excluded, coverage_candidates = [], [], []
    for player in sorted(context_players, key=lambda p: str(p.get("id", ""))):
        pid, tid = player.get("id"), player.get("team_id")
        if tid not in {home, away} or pid not in historical_ids:
            continue
        status = str(player.get("availability", "UNVERIFIED")).upper()
        injury = str(player.get("injury_status", "")).upper()
        roster = str(player.get("roster_status", "")).upper()
        if status in {"OUT", "DOUBTFUL", "QUESTIONABLE"} or any(word in injury for word in ("OUT", "DOUBTFUL", "QUESTIONABLE")):
            excluded.append({"player_id": pid, "reason": "Injury status excluded from default draft", "availability": status})
            continue
        if roster in {"INACTIVE", "RELEASED", "WAIVED", "SUSPENDED", "CONFLICTING_MEMBERSHIP"}:
            excluded.append({"player_id": pid, "reason": "Current roster status is ineligible"})
            continue
        source = player.get("source") or {}
        try:
            stamp = utc(source["retrieved_at"])
            fresh = 0 <= (as_of - stamp).total_seconds() <= MAX_CONTEXT_AGE
        except (KeyError, ValueError, TypeError):
            fresh = False
        if not fresh:
            excluded.append({"player_id": pid, "reason": "Current roster source missing, future-dated or stale"})
            continue
        appearances = []
        for row in games.values():
            if tid not in {row["home_id"], row["away_id"]}:
                continue
            boxes = [b for b in row["boxes"] if b["player_id"] == pid and b["team_id"] == tid and b["minutes"] > 0]
            if len(boxes) > 1:
                raise ValueError("Duplicate historical player appearance")
            if boxes:
                appearances.append((row, boxes[0]))
        appearances.sort(key=lambda item: (utc(item[0]["tipoff"]), item[0]["id"]), reverse=True)
        sample = appearances[:10]
        if len(sample) < MIN_GAMES:
            excluded.append({"player_id": pid, "reason": "Fewer than three prior positive-minute appearances for current team"})
            continue
        minutes = [b["minutes"] for _, b in sample]
        if sum(minutes) / len(minutes) < MIN_MINUTES:
            excluded.append({"player_id": pid, "reason": "Historical sample mean minutes is below 18"})
            continue
        for stat, ladder in LADDERS.items():
            values = [b[stat] if stat in b else b["oreb"] + b["dreb"] if stat == "reb" else None for _, b in sample]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v != math.floor(v) for v in values):
                continue
            offers = _offered_rows(offered_props or [], pid, tid, stat, game, as_of)
            choices = [(row["side"], row["line"], row) for row in offers]
            if not choices and selection_mode == "standard":
                choices = [("at_least", float(threshold), None) for threshold in ladder]
            qualified = []
            offered_ladder = []
            aggressive_result = None
            if selection_mode == "aggressive":
                from .aggressive import evaluate_aggressive_ladder
                aggressive_result = evaluate_aggressive_ladder(values, choices)
                offered_ladder = aggressive_result["ladder"]
                quality_rows = [r for r in offered_ladder if r.get("evidence_eligible")]
                eligible = aggressive_result["eligible"] or quality_rows
                # Evaluate every offered alternative before choosing a threshold.
                # Higher lines win ties, rather than winning regardless of value.
                best = max(eligible, key=lambda r: (r["ranking_score"],
                    r["line"] if r["side"] != "under" else -r["line"], r["side"], str(r["offer"]))) if eligible else None
                for row in ([best] if best else []):
                    threshold, side = row["line"], row["side"]
                    qualified.append((threshold if side != "under" else -threshold, side, threshold, row["offer"], row["hits"]))
            for side, threshold, offer in (choices if selection_mode == "standard" else []):
                hits = sum(v > threshold if side == "over" else v < threshold if side == "under" else v >= threshold for v in values)
                if offer:
                    offered_ladder.append({"side": side, "line": threshold, "decimal_odds": offer["decimal_odds"],
                        "break_even_probability": 1 / offer["decimal_odds"],
                        "historical_hits": hits, "sample_games": len(values), "historical_hit_rate": hits / len(values),
                        "passes_descriptive_screen": hits / len(values) >= MIN_FREQUENCY,
                        "forecast_probability": None, "expected_value": None,
                        "observed_at": offer["observed_at"], "source_sha256": offer["source_sha256"]})
                if hits / len(values) >= MIN_FREQUENCY:
                    qualified.append((threshold if side != "under" else -threshold, side, threshold, offer, hits))
            if not qualified:
                continue
            _, side, threshold, offer, hits = max(qualified, key=lambda item: (item[0], item[1], str(item[3])))
            candidate = {"player_id": pid, "player_name": player["name"], "team_id": tid,
                "position": player.get("position"), "stat": stat, "side": side, "line": threshold,
                "threshold_kind": "OFFERED_LINE" if offer else "ANALYTICAL_THRESHOLD_NOT_OFFERED",
                "descriptive": {"hits": hits, "games": len(values), "hit_rate": hits / len(values),
                                "interval_95": wilson(hits, len(values)),
                                "interval_scope": "Naive fixed-threshold interval; threshold selected on this same sample, not post-selection coverage",
                                "smoothed_frequency": (hits + 1) / (len(values) + 2),
                                "smoothing_basis": "Beta(1,1) descriptive regularization; not a fitted forecast",
                                "label": "Historical sample frequency, not a forecast"},
                "minutes": {"mean": sum(minutes) / len(minutes), "minimum": min(minutes), "maximum": max(minutes)},
                "historical_distribution": {"mean": sum(values) / len(values), "minimum": min(values),
                    "maximum": max(values), "pooled_per_minute": sum(values) / sum(minutes)},
                "sample_observations": [{"game_id": row["id"], "tipoff": row["tipoff"],
                    "known_at": row["known_at"], "minutes": box["minutes"], "value": values[i]}
                    for i, (row, box) in enumerate(sample)],
                "selection_evidence": {"thresholds_tested": len(choices), "selected_on_same_history": True,
                    "ranking": "Team/stat balance, Wilson lower sample-support bound, sample size, frequency, minutes; deterministic feasibility search",
                    "matchup_adjustment": None, "minutes_forecast": None,
                    "availability_verified": False,
                    "availability_basis": "Roster status alone does not verify game-specific participation or minutes restrictions",
                    "limitations": ["Positive-minute-only history is conditional on participation",
                        "Small sample and threshold search can overstate sample support",
                        "No calibrated probability, opponent adjustment or offered-price edge"]},
                "recent5": [{"game_id": row["id"], "tipoff": row["tipoff"], "minutes": box["minutes"],
                             "value": values[i]} for i, (row, box) in enumerate(sample[:5])],
                "availability": status, "injury_status": player.get("injury_status"),
                "sample_game_ids": [row["id"] for row, _ in sample],
                "sample_known_at": [row["known_at"] for row, _ in sample],
                "roster_source": source, "historical_source": {"dataset_id": dataset.get("id"),
                    "source": dataset.get("source"), "source_url": dataset.get("source_url"),
                    "retrieved_at": dataset.get("retrieved_at")},
                "offer": offer, "actionable": False, "probability": None}
            candidate["offered_alt_ladder"] = offered_ladder
            candidate["selection_mode"] = selection_mode
            if aggressive_result is not None:
                selected = next(row for row in offered_ladder if row["side"] == side and row["line"] == threshold and row["offer"] == offer)
                candidate["aggressive_evidence"] = {k: v for k, v in selected.items() if k != "offer"}
                candidate["aggressive_policy"] = aggressive_result["policy"]
                candidate["family_eligible"] = selected["eligible"] and selected["ranking_score"] > 1e-12
                candidate["coverage_eligible"] = selected.get("evidence_eligible") is True
                candidate["selection_evidence"]["ranking"] = "Offered-line uncertainty-adjusted score; then team/stat balance and positive family score, sample support; deterministic feasibility search"
            candidate["selection_basis"] = "Highest qualifying offered threshold (or analytical ladder if no quotes), using descriptive history only; no fitted matchup probability or value claim."
            if selection_mode == "aggressive":
                candidate["selection_basis"] = "Best uncertainty-adjusted offered-line score using market-shrunk sample evidence and known entry fees; higher over/at-least or lower under breaks ties; aggressive research only."
            candidate["id"] = digest({"player_id": pid, "team_id": tid, "stat": stat, "side": side,
                "line": threshold, "sample_game_ids": candidate["sample_game_ids"], "selection_mode": selection_mode, "version": VERSION})[:24]
            candidates.append(candidate)
            if aggressive_result is not None:
                coverage_candidates.extend(_offered_alternative(candidate, row) for row in offered_ladder if row.get("evidence_eligible"))
    families = []
    family_candidates = [c for c in candidates if selection_mode != "aggressive" or c.get("family_eligible") is True]
    for size in (3, 5, 8):
        optimization = None
        if selection_mode == "aggressive" and size in {5, 8}:
            from .family_optimizer import select_family
            optimized = select_family(coverage_candidates, size, (home, away))
            selected, optimization = optimized["legs"], optimized["diagnostics"]
        else:
            selected = _select(family_candidates, size, (home, away))
        if not selected:
            reason = f"Insufficient verified, history-supported distinct players for the required {size}-leg research draft." if optimization is not None else f"Insufficient positive uncertainty-adjusted scores and distinct players for a {size}-leg two-team draft." if selection_mode == "aggressive" else f"Insufficient eligible distinct players and mixed stat coverage for a {size}-leg two-team draft."
            family = _blank_family(size, [reason] + blockers)
        else:
            family = {"leg_count": size, "status": "DRAFT_UNPRICED", "legs": selected,
                "actionable": False, "joint_probability": None, "expected_return_per_unit": None,
                "combined_odds": None, "block_reasons": blockers,
                "selection_policy": "One player per leg; at most two of each statistic; both teams represented."}
        if optimization is not None:
            family["optimization"] = optimization
            family["nightly_priority"] = True
            family["selection_policy"] = "Size-specific aggressive anchors and probability-supported legs; distinct players and one exact game; quote/history gates preserved."
        family["id"] = digest({"game_id": game["id"], "size": size,
                               "legs": [leg["id"] for leg in family["legs"]], "selection_mode": selection_mode, "version": VERSION})[:24]
        family["selection_mode"] = selection_mode
        families.append(family)
    provenance["historical_games_considered"] = len(games)
    provenance["context_player_ids"] = sorted(str(x) for x in context_ids)
    return {"families": families, "candidates": candidates, "excluded_players": excluded,
        "block_reasons": blockers, "actionable": False, "use_status": "RESEARCH_ONLY",
        "selection_mode": selection_mode, "provenance": provenance, "joint_probability": None, "expected_return_per_unit": None}
