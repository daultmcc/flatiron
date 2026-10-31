import numpy as np
import pytest

from flatiron.apo import parse_apo
from flatiron.audio.biquad import PeakingFilter, response_db

AUTOEQ = """# AutoEQ-style preset
Preamp: -6.2 dB
Filter 1: ON LSC Fc 105 Hz Gain 5.5 dB Q 0.70
Filter 2: ON PK Fc 6150 Hz Gain -4.2 dB Q 6.50
Filter 3: OFF PK Fc 3000 Hz Gain 9 dB Q 1
Filter 4: ON HSC Fc 10000 Hz Gain -3.0 dB Q 0.70
"""


def test_parse_autoeq_preset():
    eq = parse_apo(AUTOEQ)
    assert eq.preamp_db == pytest.approx(-6.2)
    assert [f.kind for f in eq.filters] == ["LS", "PK", "HS"]
    assert eq.filters[1].fc == 6150 and eq.filters[1].q == 6.5


def test_gain_matches_components():
    eq = parse_apo(AUTOEQ)
    pk = parse_apo("Filter 1: ON PK Fc 6150 Hz Gain -4.2 dB Q 6.50")
    assert np.allclose(pk.gain_db([1000, 6150]), response_db([PeakingFilter(6150, -4.2, 6.5)], [1000, 6150]))
    g = eq.gain_db([20, 6150, 20000])
    assert g[0] == pytest.approx(5.5, abs=0.3)  # low shelf plateau
    assert g[2] == pytest.approx(-3.0, abs=0.3)  # high shelf plateau
    assert eq.gain_db([1000], include_preamp=True)[0] == pytest.approx(eq.gain_db([1000])[0] - 6.2)


def test_shelf_midpoint_is_half_gain():
    for kind in ("LSC", "HSC"):
        eq = parse_apo(f"Filter 1: ON {kind} Fc 1000 Hz Gain 6 dB Q 0.707")
        assert eq.gain_db([1000])[0] == pytest.approx(3.0, abs=0.05)


@pytest.mark.parametrize("bad", ["Filter 1: ON BP Fc 100 Hz Q 1", "Filter 1: ON PK Fc 100 Hz Gain 3 dB",
                                 "Volume: loud"])
def test_rejects_what_it_cannot_model(bad):
    with pytest.raises(ValueError):
        parse_apo(bad)
