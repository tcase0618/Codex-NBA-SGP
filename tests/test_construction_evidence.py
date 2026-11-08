from test_construction import fixture
from nba_quant.construction import construct_game_drafts


def test_full_sample_evidence_and_regularized_frequency_are_not_a_forecast():
    game, dataset, players, now = fixture()
    candidate = construct_game_drafts(game, dataset, players, as_of=now)["candidates"][0]
    observations = candidate["sample_observations"]
    assert len(observations) == candidate["descriptive"]["games"]
    assert candidate["descriptive"]["smoothed_frequency"] == 6 / 7
    assert candidate["historical_distribution"]["mean"] == sum(s["value"] for s in observations) / len(observations)
    assert candidate["historical_distribution"]["pooled_per_minute"] == sum(s["value"] for s in observations) / sum(s["minutes"] for s in observations)
    assert candidate["selection_evidence"]["selected_on_same_history"] is True
    assert candidate["selection_evidence"]["availability_verified"] is False
    assert candidate["selection_evidence"]["matchup_adjustment"] is None
    assert candidate["probability"] is None
