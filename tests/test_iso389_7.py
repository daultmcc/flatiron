import numpy as np
import pytest

from flatiron import iso226, iso389_7


def test_table_is_consistent():
    assert iso389_7.T_FREE - iso389_7.T_DIFFUSE == pytest.approx(iso389_7.DELTA_L, abs=1e-9)
    shared = np.isin(iso226.F, iso389_7.F)
    assert iso389_7.threshold(iso226.F[shared]) == pytest.approx(iso226.TF[shared])  # ISO 226's threshold is 389-7's


def test_thresholds_ends():
    t = iso389_7.threshold([100.0, 16000.0, 18000.0, 20000.0])
    assert t[0] == pytest.approx(iso226.threshold([100.0])[0])  # ISO 226 below 125 Hz
    assert t[1:] == pytest.approx([40.2, 70.4, 70.4])
    d = iso389_7.threshold([16000.0, 18000.0], "diffuse")
    assert d == pytest.approx([43.7, 73.9])  # 18 kHz: T_f less ΔL at 16 kHz
    f = np.geomspace(50, 20000, 200)
    assert iso389_7.threshold(f) - iso389_7.threshold(f, "diffuse") == pytest.approx(iso389_7.diffuse_minus_free(f))
