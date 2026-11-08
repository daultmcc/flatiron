"""The joint baseline (fit/decompose.py)."""

import numpy as np

from flatiron.audio.biquad import peaking_db_fast
from flatiron.fit.decompose import fit_joint_baseline
from flatiron.fit.pipeline import CorrectionSettings, correct_ear
from flatiron.model.grid import log_grid
from flatiron.model.probit import ProbitFit

G = log_grid()
X = np.log2(G / 1000)


def peaks(*fgq):
    return peaking_db_fast(*map(np.array, zip(*fgq)), G).sum(0)


def test_slow_trends_are_all_tonality():
    trend = 2.0 - 1.5 * X + 0.3 * X**2
    b = fit_joint_baseline(G, trend, np.full(G.size, 0.5), 1.0)
    assert np.max(np.abs(b.values - trend)) < 0.3
    assert np.max(b.feature_prob) < 0.2


def test_a_known_decomposition_is_recovered():
    trend = 1.0 - 1.0 * X
    feats = peaks((2500, 5.0, 4.0), (6200, -5.0, 6.0), (9000, 7.0, 5.0))
    b = fit_joint_baseline(G, trend + feats, np.full(G.size, 0.5), 1.0)
    assert np.max(np.abs(b.values - trend)) < 0.6
    near = np.abs(feats) > 3
    assert np.all(b.feature_prob[near] > 0.9) and np.median(b.feature_prob[np.abs(feats) < 0.05]) < 0.1


def test_the_same_data_give_the_same_baseline():
    D = 1.0 - X + peaks((7000, 8.0, 3.0))
    a, b = (fit_joint_baseline(G, D, np.full(G.size, 0.8), 1.0).values for _ in range(2))
    assert np.array_equal(a, b)


def test_no_step_along_the_error_bars_scale():
    """A block far above the reference, like SundaraTone L's 6 to 13 kHz, is
    where the robust baseline tips. The joint one moves smoothly as the SDs
    are scaled."""
    block = 10.0 / (1 + np.exp(-(X - np.log2(6.2)) / 0.08)) * (1 - 1 / (1 + np.exp(-(X - np.log2(13)) / 0.08)))
    D = -1.5 * X + block + peaks((11000, 5.0, 6.0), (2100, -4.0, 5.0))
    sd = 0.9 + 0.3 * np.clip(X / 4, 0, 1)
    bs = [fit_joint_baseline(G, D, sd * s, 1.0, draws=8000).values for s in np.arange(0.8, 1.2001, 0.025)]
    steps = [np.max(np.abs(a - b)) for a, b in zip(bs, bs[1:])]
    assert max(steps) < 0.3


def test_the_jacobian_matches_finite_differences_and_the_residual_covariance():
    g = log_grid(step_oct=1 / 12)
    x = np.log2(g / 1000)
    D = 1.0 - x + peaking_db_fast(np.array([3000.0, 8000.0]), np.array([6.0, -5.0]), np.array([4.0, 4.0]), g).sum(0)
    sd = np.full(g.size, 0.5)
    fit = lambda d: fit_joint_baseline(g, d, sd, 1.0, draws=4000)
    b = fit(D)
    rng = np.random.default_rng(0)
    u = rng.standard_normal(g.size)
    fd = (fit(D + 1e-4 * u).values - fit(D - 1e-4 * u).values) / 2e-4
    assert np.max(np.abs(fd - b.jacobian @ u)) < 0.1 * np.max(np.abs(fd))  # J is exact for the true mean
    # The residual's covariance, (I − J) Σ (I − J)ᵀ, against Monte Carlo.
    M = np.eye(g.size) - b.jacobian
    pred = np.sqrt(np.diag(M @ np.diag(sd**2) @ M.T))
    Rs = []
    for _ in range(100):
        Dn = D + sd * rng.standard_normal(g.size)
        Rs.append(Dn - fit(Dn).values)
    obs = np.std(Rs, axis=0)
    assert 0.75 < np.median(obs / pred) < 1.25


def test_the_pipeline_uses_it():
    L = 1.0 - X + peaks((4000, 5.0, 4.0), (8400, -4.0, 8.0))
    fit = ProbitFit(G, L - L.mean(), np.eye(G.size) * 0.3**2, 1.0, 0.5, 0.02, 600, 0.0, True)
    r, j = (correct_ear("L", fit, CorrectionSettings(reference="none", baseline=b)) for b in ("robust", "joint"))
    assert j.baseline_sd is not None and r.baseline_sd is None
    assert np.max(np.abs(j.baseline - r.baseline)) < 0.5
    fcs = lambda c: sorted(round(f.fc, -2) for f in c.features if f.accepted)
    assert fcs(j) == fcs(r)
    assert np.max(np.abs(j.target - r.target)) < 0.5
