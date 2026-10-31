import numpy as np
import pytest

from flatiron import iso226


@pytest.mark.parametrize("phon", [20, 40, 60, 80])
def test_1khz_identity(phon):
    assert iso226.contour([1000.0], phon)[0] == pytest.approx(phon, abs=0.1)


def test_ear_gain_region_is_most_sensitive():
    f = np.geomspace(1000, 12500, 200)
    c = iso226.contour(f, 60)
    assert 2800 < f[np.argmin(c)] < 4200


def test_extrapolation_is_continuous():
    a, b = iso226.contour([12499.0, 12501.0], 60)
    assert abs(a - b) < 0.05


def test_perceived_level_is_negated_contour():
    f = np.array([1000.0, 4000.0])
    assert np.allclose(iso226.perceived_level(f, 60), 60 - iso226.contour(f, 60))
