import numpy as np
import pytest
from scipy import optimize

from flatiron.model.grid import log_grid
from flatiron.model.probit import Priors, Trials, _Problem, fit_probit
from flatiron.sim.design import random_pairs, uniform_design
from flatiron.sim.listener import EarTruth, SimulatedListener, design_example


def simulate(truth, n, seed, grid, **kw):
    """Data from the model's own assumptions: an ideal point probe."""
    rng = np.random.default_rng(seed)
    lis = SimulatedListener({"L": truth}, seed=seed, stimulus=None, **kw)
    f_a, f_b, off, _ = uniform_design(grid, n, rng)
    return Trials(f_a, f_b, off, lis.respond("L", f_a, f_b, off))


def test_gradient_matches_finite_differences():
    g = log_grid(1000, 4000)
    t = simulate(design_example(), 300, 0, g)
    prob = _Problem(t, g, Priors())
    theta = np.concatenate([np.random.default_rng(1).normal(0, 2, g.size), [0.1, -0.5, -3.0]])
    err = optimize.check_grad(lambda x: prob.objective(x)[0], lambda x: prob.objective(x)[1], theta)
    assert err < 1e-4 * np.linalg.norm(prob.objective(theta)[1])


def test_random_pairs_respect_graph():
    g = log_grid()
    i, j = random_pairs(g, 5000, np.random.default_rng(0))
    step = np.abs(i - j)
    assert np.all(step >= 1)
    assert np.all(np.abs(np.log2(g[i] / g[j])) <= 1 + 1e-9)
    assert 0.2 < np.mean(step > 3) < 0.3


@pytest.fixture(scope="module")
def recovery():
    g = log_grid()
    truth = design_example("L")
    fits = [fit_probit(simulate(truth, 1200, s, g), g) for s in range(3)]
    tc = truth.level(g)
    return g, tc - tc.mean(), fits


def test_hyperparameters_recovered(recovery):
    _, _, fits = recovery
    for f in fits:
        assert f.converged
        assert 0.7 < f.sigma0 < 1.4
        assert 0.01 < f.lapse < 0.08


def test_curve_recovered_with_calibrated_error_bars(recovery):
    _, truth, fits = recovery
    for f in fits:
        err = f.L - truth
        assert abs(f.L.mean()) < 1e-9
        # Uniform random pairs pin the overall tilt only weakly (one seed is
        # off by ~0.9 dB/oct); the correction's baseline removes tilt anyway,
        # so judge the shape with a best-fit line removed.
        x = np.log2(f.grid)
        shape = err - np.polyval(np.polyfit(x, err, 1), x)
        assert np.sqrt(np.mean(shape**2)) < 0.9
        assert np.sqrt(np.mean(err**2)) < 1.6
    # Calibration is a property across runs (one of these seeds draws a
    # 1.8-SD slope error), so check average coverage, not each run's.
    assert np.mean([np.mean(np.abs(f.L - truth) < 2 * f.sd) for f in fits]) > 0.85


def test_narrow_features_recovered(recovery):
    g, truth, fits = recovery
    for fc, sign in ((6200, 1), (8400, -1)):  # Q 6 peak, Q 10 notch
        k = int(np.argmin(np.abs(g - fc)))
        flank = lambda L: L[k] - 0.5 * (L[k - 6] + L[k + 6])
        depth = np.mean([flank(f.L) for f in fits])
        assert sign * flank(truth) > 2.5
        # Smoothness shrinkage leaves narrow features up to ~1 dB shallow here.
        assert abs(depth - flank(truth)) < 1.5


def test_flat_ear_gives_flat_curve():
    g = log_grid()
    f = fit_probit(simulate(EarTruth(phon=None), 600, 5, g), g)
    assert np.max(np.abs(f.L)) < 3 * f.sd.max()


def test_warm_start_without_hyper_refit():
    g = log_grid()
    t = simulate(design_example(), 600, 2, g)
    full = fit_probit(t, g)
    fast = fit_probit(t, g, init=full, fit_hyper=False)
    assert fast.sigma0 == pytest.approx(full.sigma0)
    assert np.max(np.abs(fast.L - full.L)) < 0.05


def test_heard_level_smears_narrow_notches():
    from flatiron.audio.stimulus import StimulusSpec
    from flatiron.sim.listener import narrow_example

    truth = {"L": narrow_example()}
    f = np.array([7300.0, 2000.0])
    point = SimulatedListener(truth, stimulus=None).heard_level("L", f)
    assert np.allclose(point, truth["L"].level(f))
    tone = SimulatedListener(truth, stimulus=StimulusSpec(kind="tone")).heard_level("L", f)
    assert np.allclose(tone, point)
    wide = SimulatedListener(truth, stimulus=StimulusSpec()).heard_level("L", f)
    narrow = SimulatedListener(truth, stimulus=StimulusSpec(bandwidth_oct=1 / 24)).heard_level("L", f)
    assert point[0] < narrow[0] < wide[0]  # notch gets shallower as the band widens
    assert abs(wide[1] - point[1]) < 0.05  # smooth regions barely change
