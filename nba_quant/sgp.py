"""Research SGP probabilities from one frozen, coherent set of game samples.

This module never authorizes a bet. A valid manually supplied quote is provenance
metadata, not proof that the sportsbook actually offered it. Pricing arithmetic is
illustrative until independently calibrated forecasts and verified sources exist.
"""
import json
import math
from datetime import datetime, timezone
from typing import Literal

import numpy as np
from pydantic import Field, ValidationError, field_validator, model_validator

from .schemas import Record, utc

COMPONENTS = {
    "pts": ("pts",), "reb": ("reb",), "ast": ("ast",), "fg3m": ("fg3m",),
    "stl": ("stl",), "blk": ("blk",), "tov": ("tov",),
    "pra": ("pts", "reb", "ast"), "points_rebounds": ("pts", "reb"),
    "points_assists": ("pts", "ast"), "rebounds_assists": ("reb", "ast"),
}
MAX_QUOTE_AGE_SECONDS = 300
MAX_ROSTER_AGE_SECONDS = 3600


class Leg(Record):
    player_id: str = Field(min_length=1, max_length=80)
    stat: Literal["pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "pra",
                  "points_rebounds", "points_assists", "rebounds_assists"]
    side: Literal["over", "under", "at_least"]
    line: float = Field(ge=0, le=1000)

    @field_validator("stat", mode="before")
    @classmethod
    def normalize_stat(cls, value):
        return value.lower() if isinstance(value, str) else value

    @field_validator("line", mode="before")
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("A boolean is not a threshold")
        return value

    @model_validator(mode="after")
    def no_unsupported_pushes(self):
        if self.side == "at_least" and not self.line.is_integer():
            raise ValueError("at_least requires an integer threshold")
        if self.side in {"over", "under"} and not math.isclose(self.line % 1, .5, abs_tol=1e-9):
            raise ValueError("over/under requires a half-line; pushes and void rules are unsupported")
        return self


class TicketQuote(Record):
    quote_id: str = Field(min_length=1, max_length=200)
    sportsbook: str = Field(min_length=1, max_length=100)
    decimal_odds: float = Field(gt=1, le=1000000)
    leg_keys: list[str] = Field(min_length=2, max_length=12)
    home_id: str = Field(min_length=1)
    away_id: str = Field(min_length=1)
    tipoff: datetime
    observed_at: datetime
    retrieved_at: datetime
    source_reference: str = Field(min_length=1, max_length=1000)
    source_url: str | None = Field(default=None, max_length=1000)

    _timestamps = field_validator("tipoff", "observed_at", "retrieved_at")(utc)

    @field_validator("sportsbook", "quote_id", "source_reference")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Quote identity and provenance cannot be blank")
        return value

    @field_validator("decimal_odds", mode="before")
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("A boolean is not odds")
        return value


def leg_key(leg):
    """An unambiguous exact market key; no display-name joins or rounded lines."""
    leg = leg if isinstance(leg, Leg) else Leg.model_validate(leg)
    return json.dumps([leg.player_id, leg.stat, leg.side, leg.line], separators=(",", ":"))


def wilson(hits, count):
    p = hits / count
    z = 1.96
    denominator = 1 + z**2 / count
    center = (p + z**2 / (2 * count)) / denominator
    radius = z * math.sqrt(p * (1 - p) / count + z**2 / (4 * count**2)) / denominator
    return [max(0., center - radius), min(1., center + radius)]


def _quote_check(raw, request, expected_keys, as_of):
    if raw is None:
        return None, "MISSING", ["Exact combined sportsbook quote is missing; individual leg odds are not multiplied."]
    try:
        quote = raw if isinstance(raw, TicketQuote) else TicketQuote.model_validate(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        return None, "INVALID", [f"Combined quote failed its schema: {exc}"]
    reasons = []
    if len(quote.leg_keys) != len(expected_keys) or set(quote.leg_keys) != set(expected_keys):
        reasons.append("Combined quote must bind every exact leg key, once each.")
    if (quote.home_id, quote.away_id, quote.tipoff) != (
        request["home_id"], request["away_id"], utc(request["tipoff"])):
        reasons.append("Combined quote event identity does not match the frozen forecast.")
    if not quote.observed_at <= quote.retrieved_at <= as_of:
        reasons.append("Require quote observed_at <= retrieved_at <= evaluation as_of.")
    if (as_of - quote.observed_at).total_seconds() > MAX_QUOTE_AGE_SECONDS:
        reasons.append("Combined quote is stale (maximum quote age: 300 seconds).")
    if quote.observed_at >= utc(request["tipoff"]):
        reasons.append("Pregame quote must have been observed before tipoff.")
    return quote.model_dump(mode="json"), "INVALID" if reasons else "VALID", reasons


def _samples(player, components, count):
    values = np.zeros(count, dtype=float)
    for component in components:
        source = player.get("samples", {}).get(component)
        if not isinstance(source, list) or len(source) != count:
            raise ValueError("Every SGP component must use the same complete frozen game sample index")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
               or x < 0 or x != math.floor(x) for x in source):
            raise ValueError("SGP stat samples must be finite nonnegative integer counts")
        values += np.asarray(source, dtype=float)
    return values


def evaluate_sgp(forecast, legs, quote=None, as_of=None):
    """Evaluate a 2–12-leg pregame research ticket against shared game draws.

    Half-line over/under and integer at-least thresholds have no push state in
    this research contract. Real book DNP/void/settlement rules remain unmodeled.
    """
    now = datetime.now(timezone.utc)
    as_of = utc(as_of) if as_of is not None else now
    if as_of > now:
        raise ValueError("Evaluation as_of cannot be in the future")
    if not isinstance(legs, (list, tuple)) or not 2 <= len(legs) <= 12:
        raise ValueError("An SGP research ticket requires 2–12 legs")
    legs = [x if isinstance(x, Leg) else Leg.model_validate(x) for x in legs]
    request = forecast["request"]
    cutoff, tipoff = utc(request["cutoff"]), utc(request["tipoff"])
    created_at = utc(forecast["created_at"])
    if not cutoff <= created_at <= as_of < tipoff or cutoff >= tipoff:
        raise ValueError("Require a frozen pregame forecast with cutoff <= creation <= as_of < tipoff")
    if request["home_id"] == request["away_id"]:
        raise ValueError("SGP requires two distinct teams in one game")
    players = forecast["result"]["players"]
    ids = [p["id"] for p in players]
    if len(ids) != len(set(ids)):
        raise ValueError("Ambiguous duplicate player IDs in frozen forecast")
    player_map = {p["id"]: p for p in players}
    count = request["simulations"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 20:
        raise ValueError("At least 20 aligned frozen game simulations are required")
    if len(forecast["result"].get("score_samples", [])) != count:
        raise ValueError("Frozen game sample count does not match forecast simulation count")
    components_used = {}
    masks, rows = [], []
    for leg in legs:
        player = player_map.get(leg.player_id)
        if player is None:
            raise ValueError(f"Unknown forecast player: {leg.player_id}")
        if player["team_id"] not in {request["home_id"], request["away_id"]}:
            raise ValueError("All SGP players must belong to this exact game")
        components = set(COMPONENTS[leg.stat])
        prior = components_used.setdefault(leg.player_id, set())
        if prior & components:
            raise ValueError("Duplicate or overlapping same-player stat constructions are unsupported")
        prior.update(components)
        values = _samples(player, COMPONENTS[leg.stat], count)
        hit = values > leg.line if leg.side == "over" else values < leg.line if leg.side == "under" else values >= leg.line
        masks.append(hit)
        hits = int(hit.sum())
        rows.append({"key": leg_key(leg), **leg.model_dump(), "player_name": player["name"],
                     "team_id": player["team_id"], "probability": hits / count,
                     "interval_95": wilson(hits, count)})
    joint_mask = np.logical_and.reduce(masks)
    hits = int(joint_mask.sum())
    p = hits / count
    independent = math.prod(row["probability"] for row in rows)
    quote_payload, quote_status, quote_reasons = _quote_check(quote, request, [r["key"] for r in rows], as_of)
    synthetic = forecast.get("synthetic") is not False
    blockers = ["Independent prospective SGP calibration has not been completed.",
                "Current roster and availability source authenticity has not been verified.",
                "Book-specific DNP, void, market eligibility and settlement rules are not modeled.",
                "Scenario-specific injury/minutes robustness is unavailable; an aggregate Monte Carlo interval is not robustness evidence."]
    if synthetic:
        blockers.insert(0, "Synthetic forecast: betting use is blocked; these samples do not describe real NBA outcomes.")
    if quote_status != "VALID":
        blockers.extend(quote_reasons)
    else:
        blockers.append("Quote provenance is caller-supplied; sportsbook authenticity has not been independently verified.")
    if (as_of - cutoff).total_seconds() > MAX_ROSTER_AGE_SECONDS:
        blockers.append("Frozen forecast cutoff is older than the 3600-second source freshness policy.")
    selected_ids = {leg.player_id for leg in legs}
    snapshots = [p for t in forecast.get("model", {}).get("teams", []) for p in t.get("players", [])
                 if p.get("player_id") in selected_ids]
    fresh_count = 0
    for snapshot in snapshots:
        known_at = snapshot.get("known_at")
        if known_at:
            known_at = utc(known_at)
            if known_at <= cutoff and 0 <= (as_of - known_at).total_seconds() <= MAX_ROSTER_AGE_SECONDS:
                fresh_count += 1
    if fresh_count != len(selected_ids):
        blockers.append("One or more selected players lack a fresh pre-cutoff availability/rotation snapshot.")
    odds = quote_payload["decimal_odds"] if quote_status == "VALID" else None
    measurable = odds is not None and not synthetic
    provenance = {"dataset_id": request.get("dataset_id"), "input_hash": forecast.get("input_hash"),
                  "feature_hash": forecast.get("model", {}).get("feature_hash"), "seed": request.get("seed"),
                  "model_version": forecast.get("model", {}).get("version"), "cutoff": cutoff.isoformat(),
                  "created_at": created_at.isoformat(), "as_of": as_of.isoformat(),
                  "source": forecast.get("source"), "source_url": forecast.get("source_url")}
    return {"use_status": "RESEARCH_ONLY", "actionable": False, "forecast_id": forecast["id"],
            "synthetic": synthetic, "joint_probability": p, "interval_95": wilson(hits, count),
            "sample_count": count, "joint_hits": hits, "legs": rows,
            "weakest_leg": min(rows, key=lambda r: (r["probability"], r["key"])),
            "dependence": {"independence_product_diagnostic": independent,
                           "lift_vs_independence": p / independent if independent > 0 else None,
                           "difference_vs_independence": p - independent,
                           "method": "shared_game_sample_conjunction"},
            "quote_status": quote_status, "quote_reasons": quote_reasons, "quote": quote_payload,
            "pricing_status": "ILLUSTRATIVE_RESEARCH_ONLY" if measurable else "NOT_MEASURABLE",
            "break_even_probability": 1 / odds if odds is not None else None,
            "expected_return_per_unit": p * odds - 1 if measurable else None,
            "expected_return_interval_95": [x * odds - 1 for x in wilson(hits, count)] if measurable else None,
            "betting_block_reasons": list(dict.fromkeys(blockers)),
            "scenario_robustness": {"status": "UNAVAILABLE", "reason": "No separate injury/minutes scenario-conditioned forecasts have been evaluated."},
            "availability_evidence": {"status": "UNVERIFIED", "selected_players": len(selected_ids),
                                      "fresh_snapshot_count": fresh_count, "maximum_age_seconds": MAX_ROSTER_AGE_SECONDS},
            "provenance": provenance,
            "event": {"home_id": request["home_id"], "away_id": request["away_id"], "tipoff": tipoff.isoformat()},
            "limitations": ["Wilson intervals quantify Monte Carlo sampling noise only; they do not measure forecast accuracy or model uncertainty.",
                            "Joint probability uses matching shared game sample indices, never a product of marginal leg probabilities.",
                            "Illustrative expected return uses one exact combined quote; it is not an approved price edge or a betting recommendation.",
                            "Player nonparticipation currently yields zero simulated stats; actual sportsbook DNP/void handling may differ.",
                            "No stake sizing, wager submission, live betting or guarantee of accuracy."]}
