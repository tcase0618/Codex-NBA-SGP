"""Outcome accounting, not causal explanations or calibrated forecasts."""
import math


def exposure_decomposition(reference_count, reference_exposure, actual_count, actual_exposure):
    """Exactly split a count difference into exposure and realized-rate terms.

    Difference = prior_rate * (actual_exposure - reference_exposure)
               + actual_count - prior_rate * actual_exposure.
    Null exposure/count stays unavailable, including explicit DNP callers.
    The second term is defined even when actual exposure is zero.
    """
    values = (reference_count, reference_exposure, actual_count, actual_exposure)
    if any(v is None for v in values):
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("Counts and exposures must be finite nonnegative numbers")
    if reference_exposure <= 0:
        return None
    rate = reference_count / reference_exposure
    exposure = rate * (actual_exposure - reference_exposure)
    residual = actual_count - rate * actual_exposure
    difference = actual_count - reference_count
    assert math.isclose(exposure + residual, difference, abs_tol=1e-9)
    return {"reference_rate": rate, "actual_rate": actual_count / actual_exposure if actual_exposure else None,
            "exposure_component": exposure, "rate_component": residual, "total_difference": difference,
            "label": "Exact accounting identity; does not establish why minutes, attempts or rates changed"}
