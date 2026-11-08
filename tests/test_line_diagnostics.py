import pytest

from nba_quant.line_diagnostics import exposure_decomposition


def test_both_minutes_and_rate_components_reconcile():
    d = exposure_decomposition(20, 40, 10, 30)
    assert d["exposure_component"] == -5
    assert d["rate_component"] == -5
    assert d["total_difference"] == -10


def test_missing_dnp_and_zero_exposure_do_not_invent_rates():
    assert exposure_decomposition(20, 40, None, None) is None
    d = exposure_decomposition(20, 40, 0, 0)
    assert d["actual_rate"] is None
    assert d["exposure_component"] == -20
    assert d["rate_component"] == 0
    assert exposure_decomposition(0, 0, 0, 1) is None


@pytest.mark.parametrize("bad", [float('nan'), float('inf'), -1, True])
def test_invalid_inputs_rejected(bad):
    with pytest.raises(ValueError):
        exposure_decomposition(20, 40, bad, 30)
