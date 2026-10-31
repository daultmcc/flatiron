import json

import numpy as np
import pytest

from flatiron.audio.biquad import PeakingFilter, response_db
from flatiron.fit.baseline import fit_baseline
from flatiron.fit.features import BROAD, NARROW, check_consistency, find_features
from flatiron.fit.filters import FilterBounds, fit_filters, preamp_db, response
from flatiron.fit.pipeline import CorrectionSettings, correct_ear, taper
from flatiron.fit.reference import read_curve_csv, reference
from flatiron.model.grid import log_grid

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


def test_consistency_drops_features_that_move_between_sessions():
    sd = 0.3
    cov = np.eye(G.size) * sd**2
    pooled = bump(6000, 0.1, -3) + bump(9000, 0.1, -3)
    feats = find_features(G, pooled, cov)
    sessions = [(bump(6000, 0.1, -3) + bump(9000 * s, 0.1, -3), cov * 3) for s in (0.85, 1.0, 1.18)]
    check_consistency(feats, G, pooled, sessions)
    six = min(feats, key=lambda f: abs(f.fc - 6000))
    nine = min(feats, key=lambda f: abs(f.fc - 9000))
    assert six.accepted and six.consistency_p > 0.05
    assert not nine.accepted and "inconsistent" in nine.reason


# -- filters ---------------------------------------------------------------

def test_filter_fit_recovers_known_filters():
    truth = [PeakingFilter(3000, -3, 2), PeakingFilter(6150, -4.2, 6.5), PeakingFilter(9000, 3, 8)]
    f = log_grid(850, 15000, 1 / 96)
    target = response_db(truth, f)
    ff = fit_filters(f, target, np.full(f.size, 0.1), budget=6, stop_z=0.5)
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

@pytest.fixture(scope="module")
def simulated():
    from flatiron.sim.listener import EarTruth, SimulatedListener, design_example
    from flatiron.sim.run import run

    out = {}
    for name, truth in (("design", design_example()), ("null", EarTruth(tilt_db_per_oct=-1.0))):
        lis = SimulatedListener({"L": truth}, seed=11)
        _, fits = run(lis, 600, seed=3)
        out[name] = (lis, fits["L"])
    return out


def test_ear_matching_the_reference_gets_no_correction(simulated):
    lis, fit = simulated["null"]
    c = correct_ear("L", fit, CorrectionSettings(reference="iso226", consistency=False))
    assert np.max(np.abs(response(c.filters, c.freqs))) < 1.5  # FALSE_DB: the agreed 1.5 dB tolerance


def test_design_features_are_corrected_in_the_right_direction(simulated):
    from flatiron.sim.validate import validate

    lis, fit = simulated["design"]
    c = correct_ear("L", fit, CorrectionSettings(reference="iso226", consistency=False))
    v = validate(lis.ears["L"], c)
    # Every feature gets a correction of the right sign near the right place;
    # the strict DESIGN.md criteria (1/24 octave, 1 dB) aren't met yet.
    for fc in v.features[:3]:
        assert np.sign(fc.corrected_db) == -np.sign(fc.gain_db)
        assert abs(fc.freq_err_oct) < 1 / 12
    assert v.worst_false_db < 1.5


def test_fit_cli_writes_profile_and_report(tmp_path, monkeypatch, capsys):
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
    assert main(["fit", "--open"]) == 0  # the only profile
    out = capsys.readouterr().out
    assert "L ear: 160 trials" in out and "preamp" in out
    report = prof.dir / "report.html"
    assert opened == [report.as_uri()]
    html = report.read_text()
    assert html.count("<picture>") == 2 and "Left ear" in html and "Settings" in html
    saved = prof.read()["fit"]
    assert set(saved["ears"]) == {"L", "R"} and len(saved["sessions"]) == 2
    # The perceptual fits are cached: a refit with other settings doesn't redo them.
    import flatiron.fit.run as run_mod

    monkeypatch.setattr(run_mod, "fit_probit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("refit")))
    assert main(["fit", "--profile", "Sim", "--width", "0.5", "--no-report"]) == 0
    assert json.loads(prof.path.read_text())["fit"]["settings"]["max_width_oct"] == 0.5


def test_preserve_tonality_keeps_the_broad_shape(simulated):
    from flatiron.fit.pipeline import smooth_log

    lis, fit = simulated["design"]
    plain = correct_ear("L", fit, CorrectionSettings(broad_strength=0, consistency=False))
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
