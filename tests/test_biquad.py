import numpy as np
import pytest

from flatiron import FS
from flatiron.audio.biquad import PeakingFilter, apply, bw_oct_to_q, peaking_coeffs, response_db


def rbj_magnitude_db(fc, gain_db, q, f, fs=FS):
    """Cookbook formulas evaluated directly on the unit circle."""
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * fc / fs
    alpha = np.sin(w0) / (2 * q)
    b = [1 + alpha * A, -2 * np.cos(w0), 1 - alpha * A]
    a = [1 + alpha / A, -2 * np.cos(w0), 1 - alpha / A]
    z = np.exp(-1j * 2 * np.pi * np.asarray(f) / fs)
    h = (b[0] + b[1] * z + b[2] * z**2) / (a[0] + a[1] * z + a[2] * z**2)
    return 20 * np.log10(np.abs(h))


@pytest.mark.parametrize("fc,g,q", [(1000, -3, 2), (6150, -4.2, 6.5), (11000, 6, 0.7), (14000, -12, 16)])
def test_matches_rbj_cookbook(fc, g, q):
    f = np.geomspace(20, 20000, 500)
    got = response_db([PeakingFilter(fc, g, q)], f)
    assert np.max(np.abs(got - rbj_magnitude_db(fc, g, q, f))) < 0.01


def test_gain_at_centre_and_unity_far_away():
    f = PeakingFilter(5000, -5, 6)
    assert response_db([f], [5000])[0] == pytest.approx(-5, abs=0.01)
    assert abs(response_db([f], [100])[0]) < 0.01


def test_cascade_adds_in_db():
    a, b = PeakingFilter(3000, 3, 2), PeakingFilter(8000, -4, 5)
    f = np.geomspace(100, 20000, 200)
    assert np.allclose(response_db([a, b], f), response_db([a], f) + response_db([b], f), atol=1e-9)


def test_sos_row_normalised():
    row = peaking_coeffs(1000, 3, 2)
    assert row[3] == 1.0 and row.shape == (6,)


def test_apply_matches_response():
    filt = PeakingFilter(4000, -6, 4)
    n = FS
    x = np.sin(2 * np.pi * 4000 * np.arange(n) / FS)
    y = apply([filt], x)
    rms = lambda s: np.sqrt(np.mean(s[n // 2:] ** 2))
    assert 20 * np.log10(rms(y) / rms(x)) == pytest.approx(-6, abs=0.05)


def test_bw_to_q():
    assert bw_oct_to_q(1.0) == pytest.approx(1.4142, abs=1e-3)
