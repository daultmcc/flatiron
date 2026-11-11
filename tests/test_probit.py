import numpy as np
import pytest
from scipy import optimize

from flatiron.model.grid import log_grid
from flatiron.model.hierarchical import _Model, enough_groups, fit_hierarchical, fit_shared_noise, tau_curve
from flatiron.model.probit import Noise, Priors, Trials, noise_sd
from flatiron.sim.design import random_pairs, uniform_design
from flatiron.sim.listener import EarTruth, SimulatedListener, design_example


def simulate(truth, n, seed, grid, **kw):
    """Data from the model's own assumptions: an ideal point probe."""
    rng = np.random.default_rng(seed)
    lis = SimulatedListener({"L": truth}, seed=seed, stimulus=None, **kw)
    f_a, f_b, off, _ = uniform_design(grid, n, rng)
    return Trials(f_a, f_b, off, lis.respond("L", f_a, f_b, off))


def _model(n_groups=3):
    g = log_grid(1000, 4000)
    t = simulate(design_example(), 300, 0, g)
    t.order = np.random.default_rng(2).choice([-1.0, 1.0], len(t))
    t.group = np.random.default_rng(3).integers(0, n_groups, len(t))
    m = _Model(t, g, Priors())
    m.set_noise(m.mu + [0.1, -0.2, 0.3, 0.2])
    m.set_tau(m.mu_tau)
    rng = np.random.default_rng(1)
    return m, rng.normal(0, 2, m.n + 1), rng.normal(0, 1, (m.n_groups, m.r))


def test_gradient_matches_finite_differences():
    m, a, v = _model()
    ga, gv = m.gradient(a, v)
    x0 = np.concatenate([a, v.ravel()])
    num = optimize.approx_fprime(x0, lambda x: m.objective(x[:m.n + 1], x[m.n + 1:].reshape(v.shape)), 1e-6)
    assert np.max(np.abs(num - np.concatenate([ga, gv.ravel()]))) < 1e-3


def test_block_arrow_solve_matches_the_dense_fisher_matrix():
    m, a, v = _model()
    fac = m.factor(a, v)
    # Assemble the same matrix by hand: rows d(eta)/d(a, v) with Fisher weights.
    _, w = m._trial_terms(a, v)
    n_a, r = m.n + 1, m.r
    J = np.zeros((len(w), n_a + m.n_groups * r))
    J[:, :m.n] = m.A.toarray()
    J[:, m.n] = m.order
    for k in m.nonempty:
        s, e = m.starts[k], m.starts[k + 1]
        J[s:e, n_a + k * r:n_a + (k + 1) * r] = m.B[k]
    H = J.T @ (w[:, None] * J)
    H[:n_a, :n_a] += m.P0
    H[n_a:, n_a:] += np.eye(m.n_groups * r)
    ga, gv = m.gradient(a, v)
    step = -np.linalg.solve(H, np.concatenate([ga, gv.ravel()]))
    assert np.allclose(fac.da, step[:n_a], atol=1e-8)
    assert np.allclose(np.concatenate([fac.dv[k] for k in range(m.n_groups)]), step[n_a:], atol=1e-8)
    assert fac.logdet == pytest.approx(np.linalg.slogdet(H)[1])


def test_group_covariances_match_the_dense_inverse():
    m, a, v = _model()
    a, v, _, fac, ok = m.solve(a, v)
    assert ok
    cov_L, _, focus_cov, focus_cross, gc, guc = m.covariances(a, v, fac, True)
    _, w = m._trial_terms(a, v)
    n_a, r = m.n + 1, m.r
    J = np.zeros((len(w), n_a + m.n_groups * r))
    J[:, :m.n] = m.A.toarray()
    J[:, m.n] = m.order
    for k in m.nonempty:
        s, e = m.starts[k], m.starts[k + 1]
        J[s:e, n_a + k * r:n_a + (k + 1) * r] = m.B[k]
    H = J.T @ (w[:, None] * J)
    H[:n_a, :n_a] += m.P0
    H[n_a:, n_a:] += np.eye(m.n_groups * r)
    C = np.linalg.inv(H)
    P = np.eye(m.n) - 1.0 / m.n
    assert np.allclose(cov_L, P @ C[:m.n, :m.n] @ P, atol=1e-6)
    k = 1
    # L_k = P L + Phi v_k as one linear map of the whole parameter vector.
    T = np.zeros((m.n, H.shape[0]))
    T[:, :m.n] = P
    T[:, n_a + k * r:n_a + (k + 1) * r] = m.Phi
    assert np.allclose(gc[k], T @ C @ T.T, atol=1e-6)
    U = np.zeros_like(T)
    U[:, n_a + k * r:n_a + (k + 1) * r] = m.Phi
    assert np.allclose(guc[k], U @ C @ U.T, atol=1e-6)
    last = m.n_groups - 1
    assert np.allclose(focus_cov, gc[last]) and np.allclose(np.diag(focus_cross + focus_cross.T),
                                                            np.diag(gc[last] + cov_L - guc[last]), atol=1e-6)


def test_tau_is_three_values_log_interpolated():
    f = np.array([1000.0, 5000.0, np.sqrt(5000 * 8000.0), 8000.0, np.sqrt(8000 * 12000.0), 12000.0, 16000.0])
    assert tau_curve(f, 0.5, 2.0, 8.0) == pytest.approx([0.5, 0.5, 1.0, 2.0, 4.0, 8.0, 8.0])


def test_noise_then_tau_does_not_run_away():
    """Fitted together, tau ran to several times the truth and sigma0 collapsed."""
    from flatiron.sim.listener import seating_deviation

    g = log_grid()
    rng = np.random.default_rng(7)
    parts = [simulate(EarTruth(phon=None, deviation=seating_deviation(2.0, 2.0, rng)), 110, 30 + s, g) for s in range(3)]
    f = fit_hierarchical(Trials.grouped(parts), g)
    assert f.tau_fitted and 0.7 < f.sigma0 < 2.5
    taus = np.array([f.tau_lo, f.tau_mid, f.tau_hi])
    # Each value has a third of the data, so each alone is noisy; together they don't run away.
    assert np.all(taus < 2 * 2) and 2 / 1.5 < np.exp(np.mean(np.log(taus))) < 2 * 1.5


def test_a_single_session_stays_at_the_prior_and_its_error_bars_allow_for_other_seatings():
    g = log_grid()
    t = simulate(EarTruth(phon=None), 400, 1, g)
    assert not enough_groups(t)
    f = fit_hierarchical(t, g)
    assert not f.tau_fitted and (f.tau_lo, f.tau_mid, f.tau_hi) == (Priors().tau_lo, Priors().tau_mid, Priors().tau_hi)
    tight = fit_hierarchical(t, g, Priors(tau_lo=0.02, tau_mid=0.02, tau_hi=0.02))
    top = g > 9000
    assert np.median(f.sd[top]) > 1.15 * np.median(tight.sd[top])


def test_sd_of_the_average_stops_shrinking_once_each_seating_is_known():
    g = log_grid()
    p = Priors(tau_lo=2.0, tau_mid=2.0, tau_hi=2.0)
    sd = {}
    for per in (300, 1500):
        parts = [simulate(EarTruth(phon=None), per, 10 + s, g) for s in range(2)]
        sd[per] = np.median(fit_hierarchical(Trials.grouped(parts), g, p, fit_hyper=False, fit_tau=False).sd)
    assert sd[1500] > 0.6 * sd[300]  # five times the trials: far from a 1/sqrt(5) = 0.45 fall


def test_a_group_with_few_trials_is_pulled_toward_the_average():
    g = log_grid()
    # Every seating has the same 4 dB peak the others' average is free to
    # explain differently: seating 2 has it, seatings 0 and 1 don't.
    bump = lambda f: 4 * np.exp(-0.5 * (np.log2(f / 3000) / 0.1) ** 2)
    flat = EarTruth(phon=None)
    peak = EarTruth(phon=None, deviation=bump)
    k = np.argmin(np.abs(g - 3000))
    p = Priors(tau_lo=2.0, tau_mid=2.0, tau_hi=2.0)
    level = {}
    for n in (600, 12):
        parts = [simulate(flat, 600, 20, g), simulate(flat, 600, 21, g), simulate(peak, n, 22, g)]
        f = fit_hierarchical(Trials.grouped(parts), g, p, fit_hyper=False, fit_tau=False)
        level[n] = f.u[2][k] - np.median(f.u[2])
        assert f.group_trials.tolist() == [600, 600, n]
        assert np.allclose(f.u.mean(axis=1), 0, atol=1e-9)  # centred: a constant offset cancels within a group
    assert level[600] > 2.5 and level[12] < 0.5 * level[600]


def test_an_empty_current_group_is_the_prior_around_the_average():
    g = log_grid()
    t = simulate(EarTruth(phon=None), 200, 3, g)
    f = fit_hierarchical(t, g, n_groups=2, fit_hyper=False)
    assert np.allclose(f.u[1], 0) and f.n_groups == 1
    assert np.allclose(f.focus_cross, f.cov)  # nothing about this seating is known yet
    assert np.all(np.diag(f.focus_cov) > np.diag(f.cov))


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
    fits = [fit_hierarchical(simulate(truth, 1200, s, g), g) for s in range(3)]
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
    shapes = []
    for f in fits:
        err = f.L - truth
        assert abs(f.L.mean()) < 1e-9
        # Uniform random pairs pin the overall tilt only weakly (one seed is
        # off by ~0.9 dB/oct); the correction's baseline removes tilt anyway,
        # so judge the shape with a best-fit line removed.
        x = np.log2(f.grid)
        shape = err - np.polyval(np.polyfit(x, err, 1), x)
        shapes.append(np.sqrt(np.mean(shape**2)))
        assert np.sqrt(np.mean(err**2)) < 1.6
    # The lapse term makes the MAP non-concave, so a run near the boundary of two
    # solutions can read up to about 1.1 dB here: judge the average, and the worst
    # against that (0.66 dB mean, 1.12 worst over 45 seeds).
    assert np.mean(shapes) < 0.9 and max(shapes) < 1.2
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
    f = fit_hierarchical(simulate(EarTruth(phon=None), 600, 5, g), g)
    assert np.max(np.abs(f.L)) < 3 * f.sd.max()


def test_warm_start_without_hyper_refit():
    g = log_grid()
    t = simulate(design_example(), 600, 2, g)
    full = fit_hierarchical(t, g)
    fast = fit_hierarchical(t, g, init=full, fit_hyper=False)
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



def test_time_order_bias_is_recovered_and_kept_out_of_the_curve():
    g = log_grid()
    truth = design_example()
    rng = np.random.default_rng(4)
    lis = SimulatedListener({"L": truth}, seed=4, stimulus=None, order_bias_db=1.2)
    f_a, f_b, off, _ = uniform_design(g, 1200, rng)
    a_first = rng.random(f_a.size) < 0.5
    y = lis.respond("L", f_a, f_b, off, a_first)
    with_order = fit_hierarchical(Trials(f_a, f_b, off, y, np.where(a_first, 1.0, -1.0)), g)
    without = fit_hierarchical(Trials(f_a, f_b, off, y), g)
    assert abs(with_order.bias_db - 1.2) < 3 * with_order.bias_sd and with_order.bias_sd < 0.3
    assert without.bias_db == 0.0
    # Ignoring the bias shows up as extra judgment noise.
    assert with_order.sigma0 < without.sigma0
    err = with_order.L - (truth.level(g) - truth.level(g).mean())
    x = np.log2(g)
    shape = err - np.polyval(np.polyfit(x, err, 1), x)  # uniform pairs pin overall tilt weakly
    assert np.sqrt(np.mean(shape**2)) < 0.9


def test_trials_order_defaults_to_unknown():
    t = Trials([1000.0], [2000.0], [0.0], [True])
    assert t.order.tolist() == [0.0]
    both = Trials.concat([t, Trials([1000.0], [1100.0], [1.0], [False], [-1.0])])
    assert both.order.tolist() == [0.0, -1.0]


def test_stimulus_keys_and_band_rows():
    from flatiron.audio.stimulus import POINT, StimulusSpec, spectrum_offsets_oct, stimulus_key
    from flatiron.model.probit import band_matrix
    from flatiron.model.grid import interp_matrix

    assert stimulus_key(None) == stimulus_key(StimulusSpec(kind="tone")) == POINT
    wide, narrow = stimulus_key(StimulusSpec()), stimulus_key(StimulusSpec(bandwidth_oct=1 / 24))
    assert wide == "noise:0.083333" and narrow == "noise:0.041667"
    assert np.ptp(spectrum_offsets_oct(wide)) == pytest.approx(1 / 12, rel=1e-3)
    g = log_grid()
    f = np.array([5000.0, 5000.0, 5000.0])
    rows = band_matrix(g, f, [POINT, narrow, wide])
    assert np.allclose(rows.sum(1), 1)
    assert np.allclose(rows[0], interp_matrix(g, f[:1])[0])
    spread = lambda r: np.sqrt(r @ (np.log2(g) - r @ np.log2(g)) ** 2)
    assert spread(rows[0]) < spread(rows[1]) < spread(rows[2])  # wider stimulus, wider average


def test_trials_keep_stimulus_through_concat():
    a = Trials([1000.0], [2000.0], [0.0], [True], stimulus=["noise:0.083333"])
    b = Trials([1000.0], [1100.0], [1.0], [False])
    both = Trials.concat([a, b])
    assert both.stimulus.tolist() == ["noise:0.083333", "point"]


def test_noise_ramps_up_in_the_treble():
    assert noise_sd(1000.0, 1000.0, 2.0, 0.5, 3.0) == pytest.approx(2.0)
    assert noise_sd(6000.0, 6000.0, 2.0, 0.5, 3.0) == pytest.approx(2.0)
    assert noise_sd(10000.0, 10000.0, 2.0, 0.5, 3.0) == pytest.approx(6.0)
    assert noise_sd(14000.0, 14000.0, 2.0, 0.5, 3.0) == pytest.approx(6.0)
    # Halfway along the ramp in log frequency: the geometric mean.
    mid = np.sqrt(6000.0 * 10000.0)
    assert noise_sd(mid, mid, 2.0, 0.5, 3.0) == pytest.approx(2.0 * np.sqrt(3.0))
    # A pair straddling it averages the two tones' variances, then the gap applies.
    d = np.log2(10000.0 / 1000.0)
    assert noise_sd(1000.0, 10000.0, 2.0, 0.5, 3.0) == pytest.approx(2.0 * np.sqrt(5.0) * np.sqrt(1 + (d / 0.5) ** 2))


def test_treble_noise_recovered():
    g = log_grid()
    high = fit_hierarchical(simulate(EarTruth(phon=None), 3000, 1, g, treble_noise=2.5), g)
    assert high.treble_noise > 1.6 and 0.7 < high.sigma0 < 1.4
    flat = fit_hierarchical(simulate(EarTruth(phon=None), 3000, 2, g), g)
    assert flat.treble_noise < 1.4 and flat.treble_noise < high.treble_noise


def test_treble_noise_stays_at_the_prior_until_the_noise_is_fitted():
    g = log_grid()
    assert fit_hierarchical(simulate(EarTruth(phon=None), 100, 3, g, treble_noise=2.5), g).treble_noise == 1.0


def test_warm_start_keeps_treble_noise():
    g = log_grid()
    t = simulate(EarTruth(phon=None), 800, 4, g, treble_noise=2.5)
    full = fit_hierarchical(t, g)
    assert fit_hierarchical(t, g, init=full, fit_hyper=False).treble_noise == pytest.approx(full.treble_noise)


# -- channel balance -----------------------------------------------------------

def _two_ears(n=300, seed=0, r_extra=()):
    from flatiron.audio.biquad import PeakingFilter

    g = log_grid()
    gr = log_grid(step_oct=1 / 20)  # the ears' grids needn't match
    base = [PeakingFilter(3000.0, 5.0, 4.0), PeakingFilter(8000.0, -4.0, 6.0)]
    tL = EarTruth(phon=None, features=base)
    tR = EarTruth(phon=None, features=base + list(r_extra))
    parts = {"L": [simulate(tL, n, seed + s, g) for s in range(2)],
             "R": [simulate(tR, n, seed + 10 + s, gr) for s in range(2)]}
    trials = {e: Trials.grouped(p) for e, p in parts.items()}
    fits = {"L": fit_hierarchical(trials["L"], g), "R": fit_hierarchical(trials["R"], gr)}
    return trials, fits, {"L": tL, "R": tR}


def _shape(f, freqs):
    from flatiron.model.grid import interp_matrix

    x = interp_matrix(f.grid, freqs) @ f.L
    return x - x.mean()


def test_a_weak_balance_prior_leaves_each_ear_as_it_was():
    from flatiron.model.hierarchical import fit_balanced
    from flatiron.model.probit import BalancePrior

    trials, fits, _ = _two_ears()
    out = fit_balanced(trials, fits, BalancePrior(100.0, 100.0))
    for e in "LR":
        assert np.abs(out[e].L - fits[e].L).max() < 0.05
        assert np.allclose(out[e].sd, fits[e].sd, rtol=0.01)
        assert out[e].sigma0 == fits[e].sigma0 and out[e].tau_hi == fits[e].tau_hi
        assert out[e].group_cov.shape == fits[e].group_cov.shape


def test_the_balance_prior_pulls_equal_ears_together_and_keeps_a_large_real_difference_in_part():
    from flatiron.audio.biquad import PeakingFilter
    from flatiron.model.hierarchical import fit_balanced

    f = np.geomspace(1000, 13000, 200)
    trials, fits, _ = _two_ears()
    out = fit_balanced(trials, fits)
    before = np.sqrt(np.mean((_shape(fits["L"], f) - _shape(fits["R"], f)) ** 2))
    after = np.sqrt(np.mean((_shape(out["L"], f) - _shape(out["R"], f)) ** 2))
    assert after < 0.7 * before
    for e in "LR":  # the coupling adds information, so error bars shrink
        assert np.median(out[e].sd) < np.median(fits[e].sd)

    # An 8 dB peak in one ear alone is kept, but shrunk: a Gaussian prior pulls
    # hardest on the largest differences (about 55% kept at 600 trials per ear).
    trials, fits, truth = _two_ears(r_extra=[PeakingFilter(5000.0, 8.0, 3.0)])
    out = fit_balanced(trials, fits)
    d = lambda fs: float((_shape(fs["R"], f) - _shape(fs["L"], f))[np.argmin(np.abs(f - 5000))])
    assert d(out) > 0.45 * d(fits) and d(out) > 4.0


def _profile_sets(n, sigma0, seed=0):
    """Three 'profiles' of one listener: different curves, two sessions each."""
    g = log_grid(1000, 8000)
    truths = [EarTruth(tilt_db_per_oct=t, phon=None) for t in (-1.0, 0.5, 0.0)]
    truths[2] = design_example()
    return [(Trials.grouped([simulate(tr, n, seed + 10 * k + s, g, sigma0=sigma0) for s in range(2)]), g)
            for k, tr in enumerate(truths)]


def test_shared_noise_is_the_prior_until_one_profile_has_enough_trials():
    """Several small profiles don't add up: each one's curve absorbs its
    answers, and sigma0 comes out far too small."""
    sets = _profile_sets(60, 1.6)  # 360 trials in all, 120 a profile
    assert fit_shared_noise(sets) == Noise.prior(Priors())
    big = _profile_sets(150, 1.6, seed=3)[0]
    assert fit_shared_noise(sets + [big]).n_trials == 300


def test_shared_noise_is_recovered_from_several_profiles():
    sets = _profile_sets(150, 1.6, seed=3)
    n = fit_shared_noise(sets)
    assert n.n_trials == 900 and 1.6 / 1.3 < n.sigma0 < 1.6 * 1.3


def test_a_given_noise_is_used_and_not_estimated():
    t, g = _profile_sets(150, 1.6)[0]
    given = Noise(1.234, 0.4, 0.02, 1.5)
    f = fit_hierarchical(t, g, noise=given)
    got = Noise.of(f)
    assert [got.sigma0, got.d0, got.lapse, got.treble_noise] == pytest.approx([1.234, 0.4, 0.02, 1.5]) and f.tau_fitted
