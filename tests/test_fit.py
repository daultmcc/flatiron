import json

import numpy as np
import pytest

from flatiron.audio.biquad import PeakingFilter, response_db
from flatiron.fit.baseline import fit_baseline
from flatiron.fit.features import BROAD, NARROW, GroupCurve, find_features, seating_step, soft_weight
from flatiron.fit.filters import FilterBounds, fit_filters, preamp_db, response
from flatiron.fit.pipeline import CorrectionSettings, correct_ear, taper
from flatiron.fit.reference import read_curve_csv, reference
from flatiron.model.grid import log_grid
from flatiron.model.probit import ProbitFit

G = log_grid()
X = np.log2(G)


def bump(fc, fwhm_oct, height):
    return height * np.exp(-0.5 * ((X - np.log2(fc)) / (fwhm_oct / 2.355)) ** 2)


# -- reference -------------------------------------------------------------

def test_reference_shapes(tmp_path, monkeypatch):
    assert np.allclose(reference("none", G), 0)
    e = reference("iso226", G)
    assert abs(e.mean()) < 1e-9
    assert e[np.argmin(abs(G - 3500))] > e[np.argmin(abs(G - 1000))]  # ear-gain region sounds louder
    csv = tmp_path / "ref.csv"
    csv.write_text("freq,db\n1000,0\n8000,-6\n")
    r = reference(str(csv), G)
    assert np.allclose(np.diff(r[(G > 1000) & (G < 8000)]) < 0, True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(FileNotFoundError, match="ISO 389-7"):
        reference("diffuse", G)


def test_curve_csv_rejects_garbage(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("1000,0\nnot a row\n")
    with pytest.raises(ValueError):
        read_curve_csv(p)


# -- baseline --------------------------------------------------------------

def test_baseline_follows_slow_trends_exactly():
    trend = 2.0 - 1.5 * X
    b = fit_baseline(G, trend, np.full(G.size, 0.5), 1.0)
    assert np.max(np.abs(b.values - trend)) < 1e-6


def test_baseline_leaves_features_in_the_residual():
    trend = 2.0 - 1.0 * X
    data = trend + bump(4000, 0.4, 4) + bump(8400, 0.08, -5)
    b = fit_baseline(G, data, np.full(G.size, 0.5), 1.0)
    r = data - b.values
    assert r[np.argmin(abs(G - 4000))] > 3.5
    assert r[np.argmin(abs(G - 8400))] < -4.5
    assert np.max(np.abs(r[G < 2500])) < 0.3


# -- features --------------------------------------------------------------

def test_features_classify_and_test_significance():
    sd = 0.4
    r = bump(6200, 0.1, -3) + bump(3500, 0.6, 2.0) + bump(10000, 0.1, 0.5)
    cov = np.eye(G.size) * sd**2
    feats = find_features(G, r, cov)
    by = {round(f.fc, -2): f for f in feats if abs(f.peak_db) > 0.3}
    notch = min(by.values(), key=lambda f: abs(f.fc - 6200))
    broad = min(by.values(), key=lambda f: abs(f.fc - 3500))
    small = min(by.values(), key=lambda f: abs(f.fc - 10000))
    assert notch.kind == NARROW and notch.accepted
    assert broad.kind == BROAD and broad.accepted
    assert not small.accepted and "2 SD" in small.reason


def test_narrow_peak_on_a_weak_broad_hump_is_corrected():
    sd = 0.4
    r = bump(5000, 1.2, 0.3) + bump(5000, 0.06, 3)
    cov = np.eye(G.size) * sd**2
    feats = find_features(G, r, cov)
    assert any(f.accepted and abs(f.fc - 5000) < 200 and f.peak_db > 2.5 for f in feats)


def _groups(curves, sd):
    cov = np.eye(G.size) * sd**2
    return [GroupCurve(c, cov, cov) for c in curves]


def test_seating_step_drops_features_most_seatings_lack():
    sd = 0.3
    cov = np.eye(G.size) * sd**2
    avg = bump(6000, 0.1, -3) + bump(9000, 0.1, -3)
    feats = find_features(G, avg, cov)
    groups = _groups([bump(6000, 0.1, -3) + bump(9000 * s, 0.1, -3) for s in (0.85, 1.0, 1.18)], sd * 1.7)
    seating_step(feats, G, avg, cov, groups)
    six = min(feats, key=lambda f: abs(f.fc - 6000))
    nine = min(feats, key=lambda f: abs(f.fc - 9000))
    assert six.accepted and six.scale == pytest.approx(1, abs=0.01)
    assert not nine.accepted and "show little of it" in nine.reason  # it's only where the average puts it in one of three


def test_a_feature_missing_from_one_seating_is_corrected_to_the_median_seating():
    sd = 0.3
    cov = np.eye(G.size) * sd**2
    avg = bump(7000, 0.2, 6.0)
    feats = find_features(G, avg, cov)
    groups = _groups([bump(7000, 0.2, h) for h in (0.5, 8.0, 9.0)], sd * 1.7)
    seating_step(feats, G, avg, cov, groups)
    f = min(feats, key=lambda f: abs(f.fc - 7000))
    # The average sits under the seatings that have it, so the share is above 1.
    assert f.accepted and "median seating" in f.reason and 8.0 / 6.0 - 0.1 < f.scale < 8.0 / 6.0 + 0.1
    assert f.seat_spread > 3
    from flatiron.fit.pipeline import build_target

    c = build_target(feats, avg, CorrectionSettings(max_cut_db=24))
    assert c.min() == pytest.approx(-avg.max() * f.scale)


def test_seatings_that_agree_leave_a_feature_alone():
    sd = 0.3
    cov = np.eye(G.size) * sd**2
    avg = bump(7000, 0.2, 6.0)
    feats = find_features(G, avg, cov)
    seating_step(feats, G, avg, cov, _groups([avg, avg + 0.05, avg - 0.05], sd))
    f = min(feats, key=lambda f: abs(f.fc - 7000))
    assert f.accepted and f.scale == pytest.approx(1, abs=0.02) and not f.reason
    assert f.level_sd > 0 and f.seat_spread is not None


def test_one_seating_gives_the_numbers_but_scales_nothing():
    sd = 0.3
    cov = np.eye(G.size) * sd**2
    avg = bump(7000, 0.2, 6.0)
    feats = find_features(G, avg, cov)
    seating_step(feats, G, avg, cov, _groups([avg * 1.3], sd))
    f = min(feats, key=lambda f: abs(f.fc - 7000))
    assert f.scale == 1 and f.seat_spread > 0


# -- filters ---------------------------------------------------------------

def test_filter_fit_recovers_known_filters():
    truth = [PeakingFilter(3000, -3, 2), PeakingFilter(6150, -4.2, 6.5), PeakingFilter(9000, 3, 8)]
    f = log_grid(850, 15000, 1 / 96)
    target = response_db(truth, f)
    ff = fit_filters(f, target, np.full(f.size, 0.1), budget=6, tol_db=0.05)
    assert len(ff.filters) == 3
    for got, want in zip(ff.filters, truth):
        assert abs(np.log2(got.fc / want.fc)) < 1 / 96
        assert got.gain_db == pytest.approx(want.gain_db, abs=0.1)
        assert got.q == pytest.approx(want.q, rel=0.05)
    assert np.max(np.abs(ff.response - target)) < 0.05


def test_filter_fit_respects_budget_and_bounds():
    truth = [PeakingFilter(fc, -3, 4) for fc in (1500, 2500, 4000, 6000, 9000)]
    f = log_grid(850, 15000, 1 / 96)
    ff = fit_filters(f, response_db(truth, f), np.full(f.size, 0.1), budget=3,
                     bounds=FilterBounds(q_max=16))
    assert len(ff.filters) == 3 and ff.budget_bound
    assert all(1000 <= x.fc <= 14000 and 0.7 <= x.q <= 16 for x in ff.filters)


def test_preamp():
    assert preamp_db([PeakingFilter(1000, -4, 2)]) == pytest.approx(-0.5)
    assert preamp_db([PeakingFilter(1000, 3, 2)]) == pytest.approx(-3.5, abs=0.01)


def test_taper():
    f = np.array([700, 900, 1000, 5000, 13000, 14000, 16500.0])
    w = taper(f, 1000, 13000, 1 / 3, 1 / 3)
    assert w[0] == 0 and w[-1] == 0  # beyond the fade
    assert w[2] == 1 and w[3] == 1 and w[4] == 1  # full correction across [f_lo, f_hi]
    assert 0 < w[1] < 1 and 0 < w[5] < 1
    hard = taper(f, 1000, 13000, 0, 0)
    assert hard.tolist() == [0, 0, 1, 1, 1, 0, 0]


# -- end to end ------------------------------------------------------------

def _simulate(name):
    from flatiron.sim.listener import EarTruth, SimulatedListener, design_example
    from flatiron.sim.run import run

    truth = design_example() if name == "design" else EarTruth(tilt_db_per_oct=-1.0)
    lis = SimulatedListener({"L": truth}, seed=11)
    _, fits = run(lis, 600, seed=3)
    return lis, fits["L"]


# One fixture per listener, so tests needing different ones can run on
# different workers.
@pytest.fixture(scope="module")
def sim_design():
    return _simulate("design")


@pytest.fixture(scope="module")
def sim_null():
    return _simulate("null")


def test_ear_matching_the_reference_gets_no_correction(sim_null):
    lis, fit = sim_null
    c = correct_ear("L", fit, CorrectionSettings(reference="iso226", consistency=False, preserve_tonality=False))
    assert np.max(np.abs(response(c.filters, c.freqs))) < 1.5  # FALSE_DB: the agreed 1.5 dB tolerance


def test_design_features_are_corrected_in_the_right_direction(sim_design):
    from flatiron.sim.validate import validate

    lis, fit = sim_design
    c = correct_ear("L", fit, CorrectionSettings(reference="iso226", consistency=False, preserve_tonality=False))
    v = validate(lis.ears["L"], c)
    # The features well above the seating spread get a correction of the right
    # sign near the right place. With one session the −3 dB dip and +3 dB peak
    # in the treble are about twice the prior's spread there, and the error
    # bars allow for that (DESIGN.md, criterion 11).
    for fc in v.features[:2]:
        assert np.sign(fc.corrected_db) == -np.sign(fc.gain_db)
        assert abs(fc.freq_err_oct) < 1 / 12
    assert v.worst_false_db < 1.5


def test_config_and_report_cli(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.session.store import Profile
    from flatiron.sim.listener import design_example
    from flatiron.sim.profile import simulate_profile

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    prof = Profile.open("Sim")
    simulate_profile(prof, {e: design_example(e) for e in "LR"}, sessions=2, trials_per_ear=80, seed=2,
                     config=SchedulerConfig(min_trials_noise_fit=10_000))
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    assert main(["report", "-H"]) == 0  # the only profile; the fit is computed on demand
    out = capsys.readouterr().out
    assert "Fitting the new trials" in out and "L ear: 160 trials" in out and "preamp" in out
    report = prof.dir / "cache" / "report.html"
    assert opened == [report.as_uri()]
    html = report.read_text()
    assert html.count("<picture>") == 2 and "Left ear" in html and "Settings" in html
    assert main(["report"]) == 0 and len(opened) == 1  # no -H: text only
    assert "Fitting" not in capsys.readouterr().out  # cached

    # Settings live in the profile; only changes from the defaults are stored.
    assert main(["config", "-w", "0.5", "--broad-strength", "50"]) == 0
    out = capsys.readouterr().out
    assert "width: 1 -> 0.5" in out and "broad-strength: 0 -> 50" in out
    assert prof.read()["settings"] == {"max_width_oct": 0.5, "broad_strength": 0.5, "preserve_tonality": False}
    assert main(["config", "--broad-strength", "60", "--f-hi", "12.5k"]) == 0
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "* width" in out and "(default 1)" in out and "  bands" in out and "checkpoint: none" in out
    assert "* f-hi               12.5k" in out and "(unused without --method smooth)" in out
    assert main(["config", "--reset", "width", "-b", "3"]) == 1  # --reset stands alone
    assert main(["config", "--reset", "width,f-hi"]) == 0
    assert "max_width_oct" not in prof.read()["settings"] and "f_hi" not in prof.read()["settings"]
    assert main(["config", "--reset", "nonsense"]) == 1
    assert main(["config", "-w", "5"]) == 1  # invalid: nothing saved
    assert main(["config", "-r", "no-such.csv"]) == 1
    assert prof.read()["settings"] == {"broad_strength": 0.6, "preserve_tonality": False}
    assert main(["config", "--broad-strength", "0"]) == 0  # 0: off, and the strength back to its default
    assert prof.read()["settings"] == {}
    assert main(["config", "--broad-strength", "40"]) == 0
    assert main(["config", "--reset", "broad-strength"]) == 0
    assert prof.read()["settings"] == {}
    capsys.readouterr()

    # Changing settings doesn't redo the perceptual fits.
    import flatiron.fit.run as run_mod

    monkeypatch.setattr(run_mod, "fit_hierarchical", lambda *a, **k: (_ for _ in ()).throw(AssertionError("refit")))
    monkeypatch.setattr(run_mod, "fit_balanced", lambda *a, **k: (_ for _ in ()).throw(AssertionError("refit")))
    assert main(["config", "-b", "2"]) == 0
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert out.count("PK ") <= 4

    # A checkpoint fixes the current filters; later settings only add to them.
    assert main(["config", "--checkpoint"]) == 0
    fixed = run_mod.checkpoint(prof)
    assert main(["config", "-b", "6"]) == 0
    pf = run_mod.current_fit(prof)
    for e, c in pf.correction.ears.items():
        assert c.fixed == fixed[e] and all(f in c.filters for f in fixed[e])
    assert main(["config", "--reset", "all"]) == 0 and run_mod.checkpoint(prof) is not None  # only if named
    assert main(["config", "--reset", "checkpoint"]) == 0 and run_mod.checkpoint(prof) is None

    ids = [s.id for s in prof.sessions()]
    assert main(["config", "--sessions", "nope"]) == 1
    monkeypatch.undo()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert main(["config", "--sessions", ids[0]]) == 0
    assert run_mod.current_fit(prof).fits["L"].n_trials == 80
    assert main(["config", "--sessions", "Sim"]) == 0 and run_mod.profile_session_ids(prof) is None
    assert main(["config", "--sessions", ids[1]]) == 0
    assert main(["config", "--reset", "sessions"]) == 0 and run_mod.profile_session_ids(prof) is None

    # The balance prior: a scale on its SDs, 0 for each ear on its own.
    from flatiron.model.grid import interp_matrix

    def lr(pf):
        f = np.geomspace(1000, 13000, 100)
        d = interp_matrix(pf.fits["L"].grid, f) @ pf.fits["L"].L - interp_matrix(pf.fits["R"].grid, f) @ pf.fits["R"].L
        return float(np.std(d))

    coupled = lr(run_mod.current_fit(prof))
    assert main(["config", "--balance", "-1"]) == 1
    assert main(["config", "--balance", "0"]) == 0 and "balance: 1 -> 0" in capsys.readouterr().out
    alone = run_mod.current_fit(prof)
    assert lr(alone) > coupled
    trials, _ = prof.history()
    for e in "LR":  # off, the fit is each ear's own
        own = run_mod.cached_fit(prof, trials[e], alone.fits[e].grid)
        assert np.allclose(alone.fits[e].L, own.L)
    assert main(["config", "--balance", "3"]) == 0
    assert coupled < lr(run_mod.current_fit(prof)) < lr(alone)


def test_preserve_tonality_keeps_the_broad_shape(sim_design):
    from flatiron.fit.pipeline import smooth_log

    lis, fit = sim_design
    plain = correct_ear("L", fit, CorrectionSettings(broad_strength=0, consistency=False, preserve_tonality=False))
    kept = correct_ear("L", fit, CorrectionSettings(preserve_tonality=True, consistency=False))
    f = kept.freqs
    w = taper(f, 1000, 13000, 0.234, 0.206)
    inside = (f >= 1200) & (f <= 12000)
    broad = lambda c: smooth_log(f, response(c.filters, f), 1.0, w)[inside]
    assert np.max(np.abs(broad(kept))) < 0.6  # broad shape kept
    assert np.max(np.abs(broad(kept))) < np.max(np.abs(broad(plain)))
    assert kept.compensation and all(c.q <= 1.5 for c in kept.compensation)
    assert not any(x.accepted and x.kind == BROAD for x in kept.features)
    narrow = [x for x in kept.filters if x not in kept.compensation]
    assert len(narrow) == len([x for x in plain.filters])  # same narrow corrections


def _fit_of(L, sd=0.3):
    from flatiron.model.probit import ProbitFit

    g = log_grid()
    return ProbitFit(g, L - L.mean(), np.eye(g.size) * sd**2, 1.0, 0.5, 0.02, 600, 0.0, True)


def test_narrow_features_inside_a_broad_region_are_corrected():
    g = G
    # A broad 1-octave excess with a narrow dip on its flank, and a 0.4-octave peak.
    L = bump(2500, 1.0, 6.0) + bump(2100, 0.12, -3.0) + bump(10000, 0.4, 7.0)
    c = correct_ear("L", _fit_of(L), CorrectionSettings(reference="none"))
    inner = [f for f in c.features if f.inside_broad and f.accepted]
    assert any(abs(np.log2(f.fc / 2100)) < 1 / 12 and f.peak_db < 0 for f in inner)
    broad = [f for f in c.features if f.kind == BROAD]
    assert broad and not any(f.accepted for f in broad)  # the excess itself is kept as tonality
    # The dip is filled; the 0.4-octave peak counts as narrow and is cut.
    h = response(c.added, np.array([2100.0, 10000.0]))
    assert h[0] > 1.5 and h[1] < -4
    assert any(f.kind == NARROW and f.accepted and abs(np.log2(f.fc / 10000)) < 1 / 12 for f in c.features)
    # With --broad the whole region is corrected instead, so it isn't searched.
    c = correct_ear("L", _fit_of(L), CorrectionSettings(reference="none", preserve_tonality=False))
    assert not any(f.inside_broad for f in c.features)


def test_baseline_follows_the_max_feature_width():
    g = G
    D = bump(3000, 0.8, 6.0)
    sd = np.full(g.size, 0.3)
    wide, narrow = (fit_baseline(g, D, sd, w).values for w in (1.0, 1 / 3))
    i = np.argmin(np.abs(g - 3000))
    assert narrow[i] - wide[i] > 2  # the narrower W lets the baseline take more of a 0.8-octave bump


def test_smooth_method_corrects_every_residual():
    g = G
    L = bump(2500, 1.0, 6.0) + bump(1800, 0.15, 2.0) + bump(7000, 0.2, 8.0)
    st = CorrectionSettings(reference="none", method="smooth", smooth_oct=0.5)
    c = correct_ear("L", _fit_of(L), st)
    h = response(c.added, np.array([1800.0, 7000.0]))
    assert h[0] < -0.5 and h[1] < -3
    with pytest.raises(ValueError, match="--broad"):
        CorrectionSettings(method="smooth", preserve_tonality=False)
    with pytest.raises(ValueError):
        CorrectionSettings(method="nonsense")


def test_config_method_and_pool_cli(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.session.store import Profile
    from flatiron.sim.listener import design_example
    from flatiron.sim.profile import simulate_profile

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    a, b = Profile.open("Noise"), Profile.open("Tone")
    for p, seed in ((a, 3), (b, 4)):
        simulate_profile(p, {e: design_example(e) for e in "LR"}, sessions=1, trials_per_ear=60, seed=seed,
                         config=SchedulerConfig(min_trials_noise_fit=10_000))
    assert main(["config", "Noise", "--smooth-width", "0.4"]) == 1  # needs --method smooth
    assert main(["config", "Noise", "--method", "smooth", "--broad-strength", "50"]) == 1
    assert main(["config", "Noise", "--method", "smooth", "--smooth-width", "0.4"]) == 0
    assert a.read()["settings"] == {"method": "smooth", "smooth_oct": 0.4}
    capsys.readouterr()
    assert main(["report", "Noise"]) == 0
    assert "Method: smooth (experimental)" in capsys.readouterr().out

    assert main(["config", "Noise", "--sessions", "Noise,Nope"]) == 1 and "pool" not in a.read()
    assert main(["config", "Noise", "--sessions", "Noise,Tone"]) == 0
    assert a.read()["pool"] == ["tone"] and a.read()["sessions_used"] is None
    capsys.readouterr()
    assert main(["report", "Noise"]) == 0
    out = capsys.readouterr().out
    assert "Pooled with Tone: 2 sessions" in out and "L ear: 120 trials" in out
    assert main(["config", "Noise"]) == 0 and "and all of Tone's (pooled)" in capsys.readouterr().out
    # A session of this profile's and all of the other's, new ones included.
    assert main(["config", "Noise", "--sessions", f"{a.sessions()[0].id},Tone"]) == 0
    simulate_profile(b, {e: design_example(e) for e in "LR"}, sessions=1, trials_per_ear=30, seed=5,
                     config=SchedulerConfig(min_trials_noise_fit=10_000))
    capsys.readouterr()
    assert main(["report", "Noise"]) == 0 and "L ear: 150 trials" in capsys.readouterr().out
    assert main(["config", "Noise", "--reset", "sessions"]) == 0 and a.read()["pool"] is None
    capsys.readouterr()
    assert main(["report", "Noise"]) == 0 and "L ear: 60 trials" in capsys.readouterr().out


def test_session_panel_shades_the_middle_90_percent_above_five_sessions(monkeypatch):
    from flatiron.report import plots

    vals = np.array([[0.0], [1.0], [2.0], [3.0]])
    # Equal weights: the median is halfway; a heavy session pulls it.
    assert plots.weighted_quantiles(vals, [1, 1, 1, 1], [0.5])[0, 0] == pytest.approx(1.5)
    assert 1.8 < plots.weighted_quantiles(vals, [1, 1, 10, 1], [0.5])[0, 0] < 2.0

    g = G
    c = correct_ear("L", _fit_of(bump(7000, 0.2, 6.0)), CorrectionSettings(reference="none"))
    fills = []
    real_fill = plots.plt.Axes.fill_between
    monkeypatch.setattr(plots.plt.Axes, "fill_between",
                        lambda self, *a, **k: (fills.append(self.get_title()), real_fill(self, *a, **k))[1])
    for n in (5, 6):
        c.session_R = [c.R + 0.1 * i for i in range(n)]
        c.session_trials = [100] * n
        fills.clear()
        assert plots.ear_figure(c, c.session_R)[:4] == b"\x89PNG"
        # Panels 1 and 2 each shade ±1 SD; the session panel adds one band above five sessions.
        assert len(fills) == (2 if n == 5 else 3)


def test_none_is_not_a_profile_name(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.session.store import Profile

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="veto none"):
        Profile.open("None")
    assert main(["run", "none", "--new", "--dry-run"]) == 1
    assert "can't be a profile name" in capsys.readouterr().out and not Profile.all()
    assert main(["run", "!!!", "--new", "--dry-run"]) == 1  # no letters or digits: a message, not a traceback
    assert main(["run", "Typo", "--dry-run"]) == 1  # without --new, run doesn't create a profile
    assert "--new" in capsys.readouterr().out and not Profile.all()


def _bump_residual(height, width_oct=0.25, sd=1.0):
    g = log_grid(1000, 13000, 1 / 24)
    x = np.log2(g / 5000)
    return g, height * np.exp(-0.5 * (x / (width_oct / 2.355)) ** 2), np.eye(g.size) * sd**2


def test_soft_weight_ramps_across_the_threshold():
    assert soft_weight(1.5, 2.0, 0.5) == 0 and soft_weight(2.5, 2.0, 0.5) == 1
    assert soft_weight(2.0, 2.0, 0.5) == pytest.approx(0.5)
    assert soft_weight(2.01, 2.0, 0.0) == 1 and soft_weight(1.99, 2.0, 0.0) == 0


def test_soft_width_keeps_the_hard_test_at_its_edges():
    """Hard acceptance is the same statistic at width 0, and wide of the ramp soft agrees."""
    g, r, cov = _bump_residual(1.9)
    assert not max(find_features(g, r, cov), key=lambda f: abs(f.peak_db)).accepted
    g, r, cov = _bump_residual(3.0)
    hard = max(find_features(g, r, cov), key=lambda f: abs(f.peak_db))
    soft = max(find_features(g, r, cov, soft_width=0.5), key=lambda f: abs(f.peak_db))
    assert hard.accepted and soft.weight == 1


def _fit_with_bump(height, sd=0.8):
    """A curve with one narrow peak around 5 kHz, whose significance is set by its height."""
    g = log_grid(850, 15000, 1 / 24)
    x = np.log2(g / 5000)
    L = height * np.exp(-0.5 * (x / (0.25 / 2.355)) ** 2)
    return ProbitFit(g, L, np.eye(g.size) * sd**2, 1.0, 0.5, 0.02, 100, 0.0, True)


def _max_step(settings, heights):
    prev = None
    out = [0.0, 0.0]
    for h in heights:
        c = correct_ear("L", _fit_with_bump(h), settings)
        if prev is not None:
            out = [max(out[0], float(np.abs(c.target - prev.target).max())),
                   max(out[1], float(np.abs(c.fit.response - prev.fit.response).max()))]
        prev = c
    return out


def test_soft_rejection_removes_the_jump_at_the_threshold():
    """A peak growing through the threshold: hard, the correction appears all at
    once. Soft, the target and the filters both follow it, the filters to within
    about the fit's tolerance."""
    heights = np.arange(0.8, 3.4, 0.05)
    base = dict(reference="none", consistency=False)
    target, resp = _max_step(CorrectionSettings(soft_width=0.0, **base), heights)
    assert target > 1.0 and resp > 1.0
    target, resp = _max_step(CorrectionSettings(soft_width=0.5, **base), heights)
    assert target < 0.4 and resp < 0.7


def test_a_partly_corrected_feature_gets_its_filter():
    """Without its own tolerance the fit would stop within 2 SD and place nothing."""
    c = correct_ear("L", _fit_with_bump(1.85), CorrectionSettings(reference="none", consistency=False, soft_width=0.5))
    f = max(c.features, key=lambda f: abs(f.peak_db))
    assert 0.2 < f.weight < 0.8
    assert abs(c.target).max() > 0.5 and np.abs(c.target - c.fit.response).max() < 0.5


def test_filter_fit_stops_when_only_the_fade_is_out_of_reach():
    """The target fades above f_hi, where no filter can be centred. That's not a
    reason to spend the whole budget."""
    f = log_grid(850, 15000, 1 / 96)
    fade = np.interp(np.log2(f), np.log2([12000, 13000, 14500]), [-8.0, -8.0, 0.0], left=0.0, right=0.0)
    fade[f < 12000] = -8.0 * np.exp(-0.5 * ((np.log2(f[f < 12000]) - np.log2(12000)) / 0.15) ** 2)
    ff = fit_filters(f, fade, np.ones(f.size), budget=20, bounds=FilterBounds(), tol_db=1.0)
    assert len(ff.filters) < 8 and not ff.budget_bound


def _many_features():
    """Broad offset in the narrow filters' average: many peaks all one way."""
    g = log_grid(850, 15000, 1 / 24)
    x = np.log2(g)
    L = sum(h * np.exp(-0.5 * ((x - np.log2(fc)) / (0.2 / 2.355)) ** 2)
            for fc, h in ((1400, 5), (1900, 4), (2600, 6), (3500, 5), (4700, 7), (6300, 6), (8400, 8), (11000, 3)))
    return ProbitFit(g, L, np.eye(g.size) * 0.3**2, 1.0, 0.5, 0.02, 100, 0.0, True)


@pytest.mark.parametrize("bands", [2, 4, 6, 8])
def test_tonality_survives_a_tight_budget(bands):
    fit = _many_features()
    c = correct_ear("L", fit, CorrectionSettings(reference="none", consistency=False, bands=bands))
    assert len(c.filters) <= bands
    assert c.compensation  # never given up for narrow corrections
    if bands <= 6:
        assert c.left_out  # and what's left out is reported


def test_a_tight_budget_keeps_the_largest_corrections():
    fit = _many_features()
    c = correct_ear("L", fit, CorrectionSettings(reference="none", consistency=False, bands=6))
    x = [f.fc for f in c.filters if f not in c.compensation]
    assert any(abs(np.log2(fc / 8400)) < 0.3 for fc in x)  # the tallest is corrected
    assert not any(abs(np.log2(fc / 11000)) < 0.1 for fc in x)  # the smallest isn't


def test_band_limit_follows_the_largest_exporter():
    assert CorrectionSettings(bands=128).bands == 128
    with pytest.raises(ValueError):
        CorrectionSettings(bands=129)


def test_soft_rejection_and_the_soft_veto_are_on_by_default():
    st = CorrectionSettings()
    assert st.soft_width == 0.5 and st.veto_softness == 0.1
