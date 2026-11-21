import numpy as np
import pytest

from flatiron.model import pitch
from flatiron.model.grid import log_grid
from flatiron.model.hierarchical import fit_hierarchical, fit_pitch_bias
from flatiron.model.probit import Noise, Trials
from flatiron.sim.design import uniform_design
from flatiron.sim.listener import EarTruth, SimulatedListener, design_example

TRUE = (-0.5, 1.0, 2.5)
NOISE = Noise(1.0, 0.5, 0.03, 1.0)


def test_the_bias_favours_the_higher_band_and_saturates():
    X = pitch.design([4000.0, 2000.0, 4000.0 * 2 ** 0.125, 4000.0 * 2 ** 0.6], [2000.0, 4000.0, 4000.0, 4000.0])
    b = np.array([1.0, 1.0, 1.0])
    assert X @ b == pytest.approx([1.0, -1.0, 0.5, 1.0])  # antisymmetric; half at 1/8 octave, full beyond 1/4
    assert pitch.curve([500.0, 2000.0, 12000.0, 20000.0], TRUE) == pytest.approx([-0.5, -0.5, 2.5, 2.5])  # flat beyond the knots
    assert pitch.offsets([3000.0], [6000.0], None) == pytest.approx([0.0])


def _profiles(bias, n=1500):
    """Four 'profiles' of one listener, n trials each, 1 to 13 kHz."""
    g = log_grid(1000, 13000)
    truths = [EarTruth(tilt_db_per_oct=t, phon=None) for t in (-1.0, 0.5, 0.0)] + [design_example()]
    sets = []
    for k, tr in enumerate(truths):
        rng = np.random.default_rng(k)
        lis = SimulatedListener({"L": tr}, sigma0=NOISE.sigma0, d0=NOISE.d0, lapse=NOISE.lapse, seed=k,
                                stimulus=None, pitch=bias)
        f_a, f_b, off, _ = uniform_design(g, n, rng)
        sets.append((Trials(f_a, f_b, off, lis.respond("L", f_a, f_b, off)), g, NOISE, tr))
    return sets


@pytest.fixture(scope="module")
def biased():
    return _profiles(TRUE)


def test_the_bias_is_recovered_across_profiles(biased):
    """About 0.3 to 0.7 dB SD per knot at 6000 trials (most of them 1/8 octave
    apart or less): within about two of them."""
    got = np.array(fit_pitch_bias([(t, g, n) for t, g, n, _ in biased]))
    assert np.abs(got - TRUE).max() < 1.3 and got[2] > 1.0


def test_no_bias_is_found_where_there_is_none():
    got = np.array(fit_pitch_bias([(t, g, n) for t, g, n, _ in _profiles(pitch.ZERO)]))
    assert np.abs(got).max() < 1.0


def test_allowing_for_the_bias_recovers_the_curve(biased):
    t, g, _, truth = biased[3]
    want = truth.level(g) - truth.level(g).mean()
    err = {}
    for name, p in (("without", pitch.ZERO), ("with", TRUE)):
        f = fit_hierarchical(t, g, noise=Noise(1.0, 0.5, 0.03, 1.0, pitch=p), fit_tau=False)
        assert f.pitch == p
        err[name] = np.sqrt(np.mean((f.L - want) ** 2))
    assert err["with"] < 0.6 * err["without"]


def test_no_trials_no_bias():
    assert fit_pitch_bias([]) == pitch.ZERO


def test_a_fit_cached_before_the_pitch_bias_loads_without_one(tmp_path, biased):
    from flatiron.fit.run import _load_fit, _save_fit

    t, g, _, _ = biased[0]
    f = fit_hierarchical(t, g, noise=Noise(1.0, 0.5, 0.03, 1.0, pitch=TRUE), fit_tau=False)
    path = tmp_path / "probit.npz"
    _save_fit(path, f)
    assert _load_fit(path).pitch == pytest.approx(TRUE)
    old = dict(np.load(path))
    del old["pitch"]
    np.savez(path, **old)
    assert _load_fit(path).pitch == pitch.ZERO
