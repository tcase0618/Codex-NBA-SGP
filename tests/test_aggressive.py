from copy import deepcopy
import math

import pytest

from nba_quant.aggressive import evaluate_aggressive_ladder


VALUES = [15, 18, 20, 21, 24, 25, 26, 28, 30, 34]


def offer(side, line, price):
    return (side, line, {"side": side, "line": line, "decimal_odds": price,
                         "provider": "The Odds API", "market_key": "player_points", "alternate": False, "line_type": "standard", "line_type_source": "PROVIDER_MARKET_KEY",
                         "source_reference": "Fictional unit-test offer; binding occurs in caller"})


def test_higher_actually_offered_line_can_pass_while_low_line_has_no_positive_proxy():
    result = evaluate_aggressive_ladder(VALUES, [offer("over", 15.5, 1.1), offer("over", 24.5, 2.4)])
    assert len(result["eligible"]) == 1
    chosen = result["eligible"][0]
    assert chosen["side"] == "over" and chosen["line"] == 24.5
    assert chosen["hits"] == 5 and chosen["proxy_probability"] == pytest.approx(17 / 36)
    assert chosen["proxy_return_gross"] == pytest.approx(2 / 15)
    assert chosen["legacy_probability"] == .5
    assert chosen["legacy_proxy_return_gross"] == pytest.approx(.2)
    assert chosen["calibrated_probability"] is chosen["verified_expected_value"] is None
    assert result["policy"]["status"] == "UNCALIBRATED_RESEARCH_PROXY"
    assert "actionable" not in result


def test_large_payout_with_zero_observed_hits_cannot_qualify_from_soft_prior():
    result = evaluate_aggressive_ladder(VALUES, [offer("over", 34.5, 100)])
    assert result["eligible"] == []
    row = result["ladder"][0]
    assert row["legacy_proxy_return_gross"] > 0
    assert row["hits"] == 0
    assert any("pseudocounts" in reason for reason in row["block_reasons"])


def test_no_actual_quotes_means_no_analytical_fallback():
    result = evaluate_aggressive_ladder(VALUES, [("at_least", 10, None)])
    assert result["eligible"] == []
    assert result["ladder"][0]["proxy_return_gross"] is None
    assert any("fallback" in reason for reason in result["ladder"][0]["block_reasons"])
    assert evaluate_aggressive_ladder(VALUES, [])["eligible"] == []


@pytest.mark.parametrize("values", [[], [30], [30, 30, 30, 30]])
def test_fewer_than_five_samples_cannot_qualify_even_if_all_hits(values):
    result = evaluate_aggressive_ladder(values, [offer("at_least", 20, 10)])
    assert result["eligible"] == []
    assert any("five" in reason for reason in result["ladder"][0]["block_reasons"])


def test_minimum_sample_hits_and_frequency_boundaries_are_exact():
    result = evaluate_aggressive_ladder([0, 0, 0, 10, 10], [offer("at_least", 10, 3)])
    assert result["eligible"][0]["hits"] == 2
    assert result["eligible"][0]["historical_hit_rate"] == .4
    assert result["eligible"][0]["proxy_probability"] == pytest.approx(11 / 30)
    assert result["eligible"][0]["proxy_return_gross"] == pytest.approx(.1)
    assert result["eligible"][0]["legacy_probability"] == pytest.approx(3 / 7)
    blocked = evaluate_aggressive_ladder([0] * 8 + [10, 10], [offer("at_least", 10, 10)])
    assert blocked["eligible"] == []
    assert any("40%" in reason for reason in blocked["ladder"][0]["block_reasons"])


def test_under_and_integer_milestones_use_their_exact_market_predicates():
    result = evaluate_aggressive_ladder([0, 1, 2, 3, 4], [offer("under", 2.5, 2.5), offer("at_least", 2, 2.5)])
    assert [row["hits"] for row in result["ladder"]] == [3, 3]
    assert len(result["eligible"]) == 2


@pytest.mark.parametrize("price", [True, 1, 0, -1, float("nan"), float("inf"), "2.5"])
def test_invalid_actual_prices_fail_closed(price):
    result = evaluate_aggressive_ladder(VALUES, [offer("over", 24.5, price)])
    assert result["eligible"] == []
    assert result["ladder"][0]["proxy_return_gross"] is None


@pytest.mark.parametrize("side,line", [("over", 20), ("under", 20), ("at_least", 20.5),
                                      ("over", True), ("over", float("nan")), ("over", -.5)])
def test_unsupported_push_or_invalid_threshold_is_not_qualified(side, line):
    assert evaluate_aggressive_ladder(VALUES, [offer(side, line, 3)])["eligible"] == []


@pytest.mark.parametrize("bad", [True, -1, 1.5, float("nan"), float("inf"), "10"])
def test_invalid_historical_counts_raise_instead_of_becoming_evidence(bad):
    with pytest.raises(ValueError, match="nonnegative integer"):
        evaluate_aggressive_ladder([bad] * 5, [offer("at_least", 10, 3)])


def test_choice_quote_mismatch_cannot_bind_a_different_threshold_or_side():
    wrong = offer("over", 24.5, 3)[2]
    result = evaluate_aggressive_ladder(VALUES, [("under", 24.5, wrong)])
    assert result["eligible"] == []
    assert any("differs" in reason for reason in result["ladder"][0]["block_reasons"])


def test_break_even_and_slightly_negative_proxy_do_not_qualify():
    assert evaluate_aggressive_ladder(VALUES, [offer("over", 24.5, 2)])["eligible"] == []
    assert evaluate_aggressive_ladder(VALUES, [offer("over", 24.5, 1.99999)])["eligible"] == []


def test_no_input_mutation_and_all_ladder_outcomes_are_retained():
    choices = [offer("over", 24.5, 2.4), offer("over", 34.5, 100), ("at_least", 10, None)]
    original = deepcopy((VALUES, choices))
    result = evaluate_aggressive_ladder(VALUES, choices)
    assert (VALUES, choices) == original
    assert len(result["ladder"]) == 3
    assert "unknown fees unavailable" in result["policy"]["fees"]
    assert result["policy"]["slippage"].startswith("UNAVAILABLE")
    assert result["policy"]["post_selection_adjusted"] is False
    assert result["policy"]["calibrated_probability"] is result["policy"]["verified_expected_value"] is None


def polymarket(side="over", line=24.5, ask=.3, bid=.25, fees_enabled=True, rate=.05):
    row = {"side": side, "line": line, "decimal_odds": 1 / ask,
           "sportsbook": "polymarket", "provider": "Polymarket", "best_ask": ask,
           "quote_basis": "ORDERBOOK_BEST_ASK", "fees_enabled": fees_enabled,
           "fee_schedule": {"exponent": 1, "rate": rate, "takerOnly": True},
           "orderbook": {"best_bid": bid, "best_ask": ask}}
    return (side, line, row)


def test_polymarket_market_anchored_posterior_sd_and_entry_fee_ranking_are_exact():
    result = evaluate_aggressive_ladder(VALUES, [polymarket()])
    row = result["eligible"][0]
    reference = (.3 + .25) / 2
    alpha, beta = 5 + 5 * reference, 5 + 5 * (1 - reference)
    mean = alpha / (alpha + beta)
    sd = math.sqrt(mean * (1 - mean) / 16)
    fee = .05 * .3 * .7
    assert row["quote_reference_probability"] == reference
    assert row["posterior_alpha"] == alpha and row["posterior_beta"] == beta
    assert row["proxy_probability"] == mean
    assert row["probability_sd"] == sd
    assert row["uncertainty_adjusted_probability"] == mean - .5 * sd
    assert row["fee_per_share"] == pytest.approx(fee)
    assert row["entry_cost"] == pytest.approx(.3 + fee)
    assert row["proxy_return_after_entry_fee"] == pytest.approx(mean / (.3 + fee) - 1)
    assert row["ranking_score"] == pytest.approx((mean - .5 * sd) / (.3 + fee) - 1)
    assert row["ranking_basis"] == "ENTRY_FEE_ADJUSTED_RESEARCH_PROXY"
    assert row["calibrated_probability"] is row["verified_expected_value"] is None


def test_known_entry_fee_can_remove_tiny_positive_mean_gross_proxy_without_changing_sample_gates():
    # 9/10 hits and a locked .895 book give mean .898333... > ask, but
    # recorded entry cost .89969875 exceeds that mean.
    values = [30] * 9 + [0]
    result = evaluate_aggressive_ladder(values, [polymarket(ask=.895, bid=.895)])
    row = result["ladder"][0]
    assert row["proxy_return_gross"] > 0
    assert row["proxy_return_after_entry_fee"] < 0
    assert result["eligible"] == []


def test_mean_eligibility_is_distinct_from_uncertainty_ranking_score():
    row = evaluate_aggressive_ladder([0] + [30] * 9,
                                    [polymarket(ask=.88, bid=.88, fees_enabled=False)])["eligible"][0]
    assert row["proxy_return_after_entry_fee"] > 0
    assert row["ranking_score"] < 0


def test_higher_offered_line_can_outrank_low_line_when_price_compensates_for_evidence():
    low = polymarket(line=15.5, ask=.9, bid=.85)
    high = polymarket(line=24.5, ask=.3, bid=.25)
    result = evaluate_aggressive_ladder(VALUES, [low, high])
    assert result["ladder"][1]["ranking_score"] > result["ladder"][0]["ranking_score"]
    assert result["eligible"][0]["line"] == 24.5


@pytest.mark.parametrize("bid", [.19, .01, None, 0, .31, True, float("nan")])
def test_wide_missing_or_crossed_polymarket_book_is_not_eligible(bid):
    choice = polymarket(bid=bid)
    result = evaluate_aggressive_ladder(VALUES, [choice])
    assert result["eligible"] == []
    assert result["ladder"][0]["liquidity_flags"]


def test_spread_boundary_is_inclusive_but_no_market_probability_is_claimed():
    result = evaluate_aggressive_ladder(VALUES, [polymarket(ask=.3, bid=.2)])
    row = result["eligible"][0]
    assert row["spread"] == pytest.approx(.10)
    assert row["reference_probability_basis"] == "BID_ASK_MIDPOINT_NOT_FAIR_PROBABILITY"
    assert result["policy"]["market_prior_strength"] == 5
    assert "not fitted" in result["policy"]["market_prior_note"]


@pytest.mark.parametrize("change", ["missing_fee_flag", "missing_schedule", "exponent", "rate_bool",
                                    "rate_nan", "rate_too_large", "maker", "string_fee_flag"])
def test_unknown_or_unsupported_polymarket_fees_are_blocked_without_zero_assumption(change):
    choice = polymarket()
    row = choice[2]
    if change == "missing_fee_flag":
        del row["fees_enabled"]
    elif change == "missing_schedule":
        del row["fee_schedule"]
    elif change == "exponent":
        row["fee_schedule"]["exponent"] = 2
    elif change == "rate_bool":
        row["fee_schedule"]["rate"] = True
    elif change == "rate_nan":
        row["fee_schedule"]["rate"] = float("nan")
    elif change == "rate_too_large":
        row["fee_schedule"]["rate"] = 1.1
    elif change == "maker":
        row["fee_schedule"]["takerOnly"] = False
    else:
        row["fees_enabled"] = "false"
    result = evaluate_aggressive_ladder(VALUES, [choice])
    assert result["eligible"] == []
    assert result["ladder"][0]["fee_per_share"] is None
    assert result["ladder"][0]["entry_cost"] is None
    assert result["ladder"][0]["proxy_return_after_entry_fee"] is None
    assert result["ladder"][0]["ranking_score"] is None


def test_explicit_fee_free_polymarket_and_camel_case_recorded_schedule():
    free = polymarket(fees_enabled=False)
    del free[2]["fee_schedule"]
    row = evaluate_aggressive_ladder(VALUES, [free])["eligible"][0]
    assert row["fee_per_share"] == 0 and row["entry_cost"] == .3
    assert row["entry_fee_status"] == "EXPLICIT_FEES_DISABLED"
    recorded = polymarket()
    recorded[2]["feeSchedule"] = recorded[2].pop("fee_schedule")
    assert evaluate_aggressive_ladder(VALUES, [recorded])["eligible"][0]["fee_per_share"] > 0


@pytest.mark.parametrize("change", ["indicative", "wrong_decimal", "different_nested_ask"])
def test_actual_ask_and_decimal_consistency_are_required(change):
    choice = polymarket()
    if change == "indicative":
        choice[2]["quote_basis"] = "INDICATIVE_METADATA"
    elif change == "wrong_decimal":
        choice[2]["decimal_odds"] = 10
    else:
        choice[2]["orderbook"]["best_ask"] = .4
    assert evaluate_aggressive_ladder(VALUES, [choice])["eligible"] == []


def test_non_polymarket_compatibility_is_explicitly_gross_only_with_unknown_net_costs():
    row = evaluate_aggressive_ladder(VALUES, [offer("over", 24.5, 2.4)])["eligible"][0]
    assert row["quote_reference_probability"] == 1 / 2.4
    assert row["ranking_basis"] == "GROSS_RESEARCH_ONLY_FEES_UNAVAILABLE"
    assert row["fee_per_share"] is row["entry_cost"] is row["proxy_return_after_entry_fee"] is None
    assert row["ranking_score"] == pytest.approx(row["uncertainty_adjusted_probability"] * 2.4 - 1)


def test_uncertainty_probability_is_clipped_at_zero_and_all_new_numeric_fields_are_finite():
    row = evaluate_aggressive_ladder([0] * 5, [polymarket(ask=.01, bid=.005)])["ladder"][0]
    assert row["uncertainty_adjusted_probability"] == 0
    assert row["eligible"] is False
    for key in ("proxy_probability", "probability_sd", "ranking_score", "fee_per_share", "entry_cost"):
        assert math.isfinite(row[key])


def test_polymarket_input_is_not_mutated():
    choices = [polymarket()]
    original = deepcopy(choices)
    evaluate_aggressive_ladder(VALUES, choices)
    assert choices == original
