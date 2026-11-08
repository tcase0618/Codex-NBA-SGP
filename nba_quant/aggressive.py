"""Experimental individual-line research screen, never verified expected value.

The caller must bind actual offers to the exact event/player and validate price
observation times before passing them here. This function cannot establish those
facts without that context and never fabricates an offered threshold or price.
"""
import math
from .line_policy import line_policy_reason

MIN_SAMPLES = 5
MIN_HITS = 2
MIN_HISTORICAL_FREQUENCY = .4
NUMERIC_ZERO_TOLERANCE = 1e-12
MARKET_PRIOR_STRENGTH = 5
UNCERTAINTY_SD_WEIGHT = .5
MAX_POLYMARKET_SPREAD = .10
VERSION = "aggressive-priced-research-0.3.0"


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _quote_evidence(offer, price):
    """Quote-derived reference and supported entry fee, never a fair probability."""
    evidence = {"quote_reference_probability": None, "reference_probability_basis": "UNAVAILABLE",
                "fee_per_share": None, "entry_cost": None, "entry_fee_status": "UNAVAILABLE",
                "ranking_basis": "UNAVAILABLE", "liquidity_flags": [], "block_reasons": []}
    if not isinstance(offer, dict) or not _number(price) or price <= 1:
        return evidence
    is_polymarket = any(str(offer.get(key, "")).strip().lower() == "polymarket" for key in ("provider", "sportsbook"))
    if not is_polymarket:
        evidence.update(quote_reference_probability=1 / price,
                        reference_probability_basis="QUOTED_BREAK_EVEN_NOT_FAIR_PROBABILITY",
                        ranking_basis="GROSS_RESEARCH_ONLY_FEES_UNAVAILABLE")
        return evidence
    book = offer.get("orderbook") or {}
    if not isinstance(book, dict):
        book = {}
    ask = offer.get("best_ask", book.get("best_ask"))
    bid = book.get("best_bid")
    valid_book = (_number(ask) and _number(bid) and 0 < bid <= ask < 1)
    if offer.get("quote_basis") != "ORDERBOOK_BEST_ASK":
        evidence["block_reasons"].append("Polymarket research requires an actual recorded BUY ask")
    if not valid_book:
        evidence["liquidity_flags"].append("TWO_SIDED_BOOK_UNAVAILABLE_OR_INVALID")
        evidence["block_reasons"].append("Polymarket research requires a valid two-sided bid/ask book")
        return evidence
    spread = ask - bid
    evidence.update(quote_reference_probability=(bid + ask) / 2,
                    reference_probability_basis="BID_ASK_MIDPOINT_NOT_FAIR_PROBABILITY", spread=spread)
    if spread > MAX_POLYMARKET_SPREAD + NUMERIC_ZERO_TOLERANCE:
        evidence["liquidity_flags"].append("SPREAD_EXCEEDS_RESEARCH_MAXIMUM")
        evidence["block_reasons"].append("Polymarket spread exceeds the fixed 0.10 research quality screen")
    else:
        evidence["liquidity_flags"].append("WITHIN_FIXED_SPREAD_SCREEN")
    if not math.isclose(price, 1 / ask, rel_tol=1e-7, abs_tol=1e-8):
        evidence["block_reasons"].append("Polymarket decimal odds are inconsistent with the recorded BUY ask")
    if book.get("best_ask") is not None and (not _number(book["best_ask"]) or not math.isclose(book["best_ask"], ask, abs_tol=1e-12)):
        evidence["block_reasons"].append("Polymarket top-level and nested BUY asks disagree")
    if offer.get("fees_enabled") is False:
        fee, fee_status = 0., "EXPLICIT_FEES_DISABLED"
    else:
        schedule = offer.get("fee_schedule") or offer.get("feeSchedule")
        supported = (offer.get("fees_enabled") is True and isinstance(schedule, dict)
                     and _number(schedule.get("rate")) and 0 <= schedule["rate"] <= 1
                     and _number(schedule.get("exponent")) and schedule["exponent"] == 1
                     and schedule.get("takerOnly") is True)
        if not supported:
            evidence["block_reasons"].append("Polymarket entry fee is unknown or uses an unsupported fee schedule")
            return evidence
        fee, fee_status = schedule["rate"] * ask * (1 - ask), "RECORDED_TAKER_RATE_EXPONENT_1"
    evidence.update(fee_per_share=fee, entry_cost=ask + fee, entry_fee_status=fee_status,
                    ranking_basis="ENTRY_FEE_ADJUSTED_RESEARCH_PROXY")
    return evidence


def evaluate_aggressive_ladder(values, choices):
    """Return screened individual offers and a full, auditable research ladder.

    ``choices`` contains ``(side, line, offer)`` tuples. Price-aware research uses
    a market-anchored Beta research proxy, not a next-game forecast. Prior strength
    and uncertainty weight are fixed assumptions, not fitted to one night's data.
    Known supported Polymarket entry fees are estimated separately. Slippage,
    availability probability and joint ticket price remain unavailable.
    """
    if not isinstance(values, (list, tuple)) or any(
        not _number(v) or v < 0 or v != math.floor(v) for v in values
    ):
        raise ValueError("Historical values must be finite nonnegative integer counts")
    if not isinstance(choices, (list, tuple)):
        raise ValueError("Offered choices must be a list of side/line/offer tuples")
    policy = {
        "version": VERSION,
        "status": "UNCALIBRATED_RESEARCH_PROXY",
        "minimum_samples": MIN_SAMPLES,
        "minimum_observed_hits": MIN_HITS,
        "minimum_historical_frequency": MIN_HISTORICAL_FREQUENCY,
        "smoothing": "Market-anchored Beta: alpha=hits+5*q; beta=games-hits+5*(1-q)",
        "market_prior_strength": MARKET_PRIOR_STRENGTH,
        "market_prior_note": "Fixed research assumption; not fitted to November 5 outcomes",
        "legacy_smoothing": "Beta(1,1) retained only as separately named audit diagnostics",
        "gross_proxy_formula": "proxy_probability * actual_decimal_odds - 1",
        "positive_mean_entry_fee_proxy_required_for_polymarket": True,
        "ranking_formula": "max(0, posterior_mean - 0.5*posterior_sd) / entry_cost - 1",
        "uncertainty_sd_weight": UNCERTAINTY_SD_WEIGHT,
        "maximum_polymarket_spread": MAX_POLYMARKET_SPREAD,
        "numeric_zero_tolerance": NUMERIC_ZERO_TOLERANCE,
        "actual_offer_required": True,
        "input_contract": "Caller must verify exact event/player identity and current offered-price observation times.",
        "fees": "Supported recorded Polymarket entry fees estimated; other or unknown fees unavailable, never assumed zero",
        "slippage": "UNAVAILABLE; excluded from gross research arithmetic",
        "calibrated_probability": None,
        "verified_expected_value": None,
        "post_selection_adjusted": False,
        "limitations": [
            "Fixed screening rules, not parameters fitted to the November 4 outcomes.",
            "Positive-minute history is conditional on participation and does not establish current eligibility or minutes.",
            "The quote reference includes market frictions and is not a fair or independently calibrated probability.",
            "Market-anchored posterior mean and standard deviation are research assumptions, not calibrated next-game uncertainty.",
            "Positive gross proxy arithmetic does not establish a profitable trade or a verified price edge.",
            "Known entry fees exclude transaction rounding, gas, later exit costs and slippage; net trade return remains unavailable.",
            "Threshold search uses the same small historical sample; selection bias is not corrected.",
            "No independent marginal multiplication, combined SGP price, joint probability, or net expected return.",
        ],
    }
    ladder = []
    n = len(values)
    for choice in choices:
        if not isinstance(choice, (tuple, list)) or len(choice) != 3:
            raise ValueError("Each offered choice must contain side, line and offer")
        side, line, offer = choice
        reasons = []
        threshold_valid = isinstance(side, str) and _number(line) and line >= 0 and (
            (side == "at_least" and line == math.floor(line))
            or (side in {"over", "under"} and math.isclose(line % 1, .5, abs_tol=1e-9))
        )
        if not threshold_valid:
            reasons.append("Unsupported threshold; use integer at_least or half-line over/under")
        actual_offer = isinstance(offer, dict) and bool(offer)
        side_reason = line_policy_reason(side, offer)
        if side_reason:
            reasons.append(side_reason)
        price = offer.get("decimal_odds") if actual_offer else None
        price_valid = _number(price) and price > 1
        if not actual_offer:
            reasons.append("No actual offered line; analytical fallback is disabled in aggressive mode")
        elif not price_valid:
            reasons.append("Actual offer decimal price is invalid")
        if actual_offer and (offer.get("side") != side or offer.get("line") != line):
            reasons.append("Offered market side or threshold differs from the evaluated choice")
        hits = None
        if threshold_valid:
            hits = sum(v >= line if side == "at_least" else v > line if side == "over" else v < line for v in values)
        frequency = hits / n if hits is not None and n else None
        legacy_probability = (hits + 1) / (n + 2) if hits is not None and n else None
        evidence = _quote_evidence(offer, price)
        reasons.extend(evidence["block_reasons"])
        q = evidence["quote_reference_probability"]
        proxy = sd = adjusted = gross = ranking = after_fee = alpha = beta = None
        if hits is not None and n and q is not None:
            alpha = hits + MARKET_PRIOR_STRENGTH * q
            beta = n - hits + MARKET_PRIOR_STRENGTH * (1 - q)
            proxy = alpha / (alpha + beta)
            sd = math.sqrt(proxy * (1 - proxy) / (alpha + beta + 1))
            adjusted = max(0., proxy - UNCERTAINTY_SD_WEIGHT * sd)
            gross = proxy * price - 1
            if evidence["entry_cost"] is not None:
                after_fee = proxy / evidence["entry_cost"] - 1
                ranking = adjusted / evidence["entry_cost"] - 1
            elif evidence["ranking_basis"] == "GROSS_RESEARCH_ONLY_FEES_UNAVAILABLE":
                ranking = adjusted * price - 1
        if n < MIN_SAMPLES:
            reasons.append("Fewer than five historical samples")
        if hits is not None and hits < MIN_HITS:
            reasons.append("Fewer than two observed threshold hits; prior pseudocounts cannot qualify a line")
        if frequency is not None and frequency < MIN_HISTORICAL_FREQUENCY:
            reasons.append("Historical threshold frequency is below the 40% research floor")
        evidence_eligible = not reasons
        eligibility_return = after_fee if evidence["entry_cost"] is not None else gross if evidence["ranking_basis"] == "GROSS_RESEARCH_ONLY_FEES_UNAVAILABLE" else None
        if eligibility_return is None or eligibility_return <= NUMERIC_ZERO_TOLERANCE:
            reasons.append("Positive mean research proxy return is unavailable or absent after the known entry fee")
        ladder.append({
            "side": side, "line": line, "offer": offer, "hits": hits,
            "sample_games": n, "historical_hit_rate": frequency,
            "proxy_probability": proxy, "proxy_return_gross": gross,
            "legacy_probability": legacy_probability,
            "legacy_proxy_return_gross": legacy_probability * price - 1 if legacy_probability is not None and price_valid else None,
            "probability_sd": sd, "uncertainty_adjusted_probability": adjusted,
            "posterior_alpha": alpha, "posterior_beta": beta,
            "ranking_score": ranking, "proxy_return_after_entry_fee": after_fee,
            **{key: value for key, value in evidence.items() if key != "block_reasons"},
            "break_even_probability": 1 / price if price_valid else None,
            "status": "UNCALIBRATED_RESEARCH_PROXY",
            "eligible": not reasons, "block_reasons": reasons,
            "evidence_eligible": evidence_eligible,
            "calibrated_probability": None, "verified_expected_value": None,
        })
    return {"eligible": [row for row in ladder if row["eligible"]], "ladder": ladder, "policy": policy}
