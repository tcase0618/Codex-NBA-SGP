"""User line rule: standard unders only; alternate thresholds are over-only."""

VERSION = "standard-unders-over-alternates-0.1.0"
STANDARD_MARKET_KEYS = frozenset({
    "player_points", "player_rebounds", "player_assists", "player_threes",
    "player_blocks", "player_steals", "player_turnovers",
    "player_points_rebounds_assists", "player_points_rebounds",
    "player_points_assists", "player_rebounds_assists",
})


def line_policy_reason(side, offer):
    if side != "under":
        return None
    if not isinstance(offer, dict):
        return "Unders require a source-identified standard line; under alternatives are disabled"
    # Never infer 'standard' from price, position in a ladder or market wording.
    # The connected provider's regular versus _alternate market key is explicit.
    standard = (offer.get("provider") == "The Odds API"
                and offer.get("market_key") in STANDARD_MARKET_KEYS
                and offer.get("alternate") is False
                and offer.get("line_type") == "standard"
                and offer.get("line_type_source") == "PROVIDER_MARKET_KEY")
    if standard:
        return None
    return "Under alternate or unknown line classification excluded; only source-identified standard unders are allowed"
