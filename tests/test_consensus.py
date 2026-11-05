import numpy as np
import pytest

from flatiron.fit.consensus import (Source, compare_ear, consensus_of, correction_rule, estimate_tau_hp, flag_regions,
                                    for_member, soft_median)
from flatiron.fit.filters import response
from flatiron.fit.pipeline import CorrectionSettings, correct_ear
from flatiron.model.grid import common_grid, interp_matrix, log_grid
from flatiron.model.probit import ProbitFit

G = log_grid()
X = np.log2(G)


def bump(fc, fwhm, h, grid=G):
    return h * np.exp(-0.5 * ((np.log2(grid) - np.log2(fc)) / (fwhm / 2.355)) ** 2)


def src(R, sd=0.05, name="p", grid=G):
    return Source(name, grid, np.asarray(R, float), np.eye(grid.size) * sd**2)


# -- the remove-only rule: the table in ADDENDUM.md, Feature 1b ----------------

@pytest.mark.parametrize("R,m,want", [(6, 0, -6), (6, 6, 0), (9, 6, -3), (0, 6, 0), (-5, 0, 5), (6, -2, -6),
                                      (-5, -5, 0), (-7, -5, 2), (-5, 2, 5)])
def test_remove_only_rule(R, m, want):
    assert correction_rule(R, m) == pytest.approx(want)


def test_match_mode_goes_all_the_way_to_the_consensus():
    assert correction_rule(0.0, 6.0, "match") == pytest.approx(6.0)
    assert correction_rule(9.0, 6.0, "match") == pytest.approx(-3.0)


# -- soft median -----------------------------------------------------------------

def test_the_worked_values_in_the_addendum():
    """A +6 dB peak on one of three gives m near +0.6 and a correction near −5.4;
    on two of three, m near +5.4 and a correction near −0.6 on each."""
    common = common_grid([G])
    k = np.argmin(np.abs(G - 5000))
    peak = bump(5000, 0.5, 6.0)
    flat = np.zeros(G.size)
    one = soft_median([src(peak), src(flat), src(flat)], common, 1.0).on(G)[0][k]
    two = soft_median([src(peak), src(peak), src(flat)], common, 1.0).on(G)[0][k]
    assert one == pytest.approx(0.6, abs=0.25) and two == pytest.approx(5.4, abs=0.25)
    assert correction_rule(6.0, one) == pytest.approx(-5.4, abs=0.25)
    assert correction_rule(6.0, two) == pytest.approx(-0.6, abs=0.25)


def test_two_sources_give_their_precision_weighted_mean():
    a, b = bump(5000, 0.5, 4.0), np.zeros(G.size)
    sm = consensus_of([src(a, 0.5), src(b, 0.5)])
    assert not sm.robust
    k = np.argmin(np.abs(sm.grid - 5000))
    assert sm.m[k] == pytest.approx(2.0, abs=0.1)


def test_tau_hp_ignores_a_single_headphones_peak_and_has_a_floor():
    common = common_grid([G])
    rng = np.random.default_rng(0)
    quiet = [src(rng.normal(0, 0.05, G.size), 0.05) for _ in range(3)]
    assert estimate_tau_hp(quiet, common) == 0.5
    peak = [src(bump(5000, 0.2, 9.0), 0.05), src(np.zeros(G.size), 0.05), src(np.zeros(G.size), 0.05)]
    assert estimate_tau_hp(peak, common) == 0.5  # one value: the median across frequency


def test_each_source_counts_by_octaves_covered_not_by_points():
    """A refined grid doesn't outvote a coarse one over the same range."""
    fine = log_grid(step_oct=1 / 48)
    common = common_grid([G, fine])
    coarse = src(np.zeros(G.size))
    dense = src(np.full(fine.size, 2.0), grid=fine)
    sm = soft_median([coarse, dense], common, 1.0, robust=False)
    assert np.median(sm.m) == pytest.approx(1.0, abs=0.02)


def test_covariance_formula_matches_monte_carlo_with_correlated_noise():
    """Per-frequency weights ignore the correlation between neighbours and come
    out too small; the sandwich doesn't."""
    rng = np.random.default_rng(1)
    C = 0.8**2 * np.exp(-0.5 * ((X[:, None] - X[None, :]) / 0.1) ** 2) + 1e-6 * np.eye(G.size)
    make = lambda: [Source("p", G, np.zeros(G.size), C) for _ in range(3)]
    common = common_grid([G])
    sm = soft_median(make(), common, 1.0, robust=False)
    draws = []
    for _ in range(500):
        srcs = [Source("p", G, rng.multivariate_normal(np.zeros(G.size), C), C) for _ in range(3)]
        draws.append(soft_median(srcs, common, 1.0, robust=False).m)
    ratio = np.median(sm.sd[10:-10] / np.std(draws, axis=0)[10:-10])
    assert 0.9 < ratio < 1.1


# -- flags -------------------------------------------------------------------------

def test_a_region_must_be_wide_enough_in_octaves():
    s = src(np.zeros(G.size))
    dev = bump(5000, 0.25, 6.0)
    assert len(flag_regions(s, "L", dev, np.ones(G.size), 2.0, 1 / 12)) == 1
    assert not flag_regions(s, "L", bump(5000, 0.05, 2.6), np.ones(G.size), 2.0, 1 / 12)


def test_one_headphone_standing_out_is_flagged_on_it_alone():
    sources = [src(bump(5800, 0.2, 5.0), 0.4, "a"), src(np.zeros(G.size), 0.4, "b"), src(np.zeros(G.size), 0.4, "c")]
    c = compare_ear("L", sources)
    who = {e.source.label: e.flags for e in c.entries}
    assert who["a"] and not who["b"] and not who["c"]
    assert abs(np.log2(who["a"][0].fc / 5800)) < 1 / 24 and who["a"][0].sign == "peak only here"
    assert who["a"][0].deviation_db > 3.5
    assert c.heterogeneous[np.argmin(np.abs(c.consensus.grid - 5800))]


def test_a_feature_every_headphone_shares_is_not_flagged():
    shared = bump(7000, 0.15, 6.0)
    c = compare_ear("L", [src(shared, 0.4, n) for n in "abc"])
    assert not c.flags
    assert c.consensus.m[np.argmin(np.abs(c.consensus.grid - 7000))] == pytest.approx(6.0, abs=0.5)


def test_other_kinds_are_shown_but_do_not_vote():
    a, b = bump(7000, 0.15, 6.0), np.zeros(G.size)
    c = compare_ear("L", [src(a, 0.4, "x"), src(a, 0.4, "y"), Source("iem", G, b, np.eye(G.size) * 0.16, "iem")])
    assert c.voters == 2
    assert [e.voting for e in c.entries] == [True, True, False]
    with pytest.raises(ValueError, match="include-kind"):
        compare_ear("L", [src(a, 0.4, "x"), Source("iem", G, b, np.eye(G.size) * 0.16, "iem")])
    assert compare_ear("L", [src(a, 0.4, "x"), Source("iem", G, b, np.eye(G.size) * 0.16, "iem")],
                       voting_kinds=("headphone", "iem")).voters == 2


# -- the correction ------------------------------------------------------------------

def _fit_of(L, sd=0.3):
    return ProbitFit(G, L - L.mean(), np.eye(G.size) * sd**2, 1.0, 0.5, 0.02, 600, 0.0, True)


def _corrected(curves, i=0, mode="remove"):
    from flatiron.fit.pipeline import member_source

    st = CorrectionSettings(reference="none", consistency=False)
    fits = [_fit_of(L) for L in curves]
    sm = consensus_of([member_source(f"h{n}", "headphone", f, st) for n, f in enumerate(fits)])
    return correct_ear("L", fits[i], st, consensus=for_member(sm, i, G, [f"h{n}" for n in range(len(fits))], "", mode))


def test_a_peak_only_this_headphone_has_is_removed_and_a_shared_one_kept():
    own, shared = bump(5800, 0.2, 5.0), bump(7500, 0.2, 6.0)
    c = _corrected([own + shared, shared, shared])
    h = response(c.added, np.array([5800.0, 7500.0]))
    assert h[0] < -3.0
    assert abs(h[1]) < 1.0
    # A headphone without that peak has nothing to remove.
    c = _corrected([own + shared, shared, shared], i=1)
    h = response(c.added, np.array([5800.0, 7500.0]))
    assert abs(h[0]) < 1.0 and abs(h[1]) < 1.0


def test_excess_covariance_includes_the_correlation_with_its_own_vote():
    """X = R − A m shares this profile's own noise, so its variance is less than
    if they were independent."""
    f = [_fit_of(np.zeros(G.size)) for _ in range(3)]
    srcs = [Source(n, G, np.zeros(G.size), x.cov) for n, x in zip("abc", f)]
    sm = consensus_of(srcs)
    ec = for_member(sm, 0, G, ["a", "b", "c"])
    cov = ec.excess_cov(srcs[0].cov)
    assert np.all(np.diag(cov)[5:-5] < np.diag(srcs[0].cov)[5:-5] + np.diag(ec.cov_m)[5:-5])
    # Monte Carlo agrees.
    rng = np.random.default_rng(2)
    xs = []
    common = sm.grid
    A = interp_matrix(common, G)
    for _ in range(400):
        noisy = [Source(s.label, G, rng.multivariate_normal(np.zeros(G.size), s.cov), s.cov) for s in srcs]
        xs.append(noisy[0].R - A @ soft_median(noisy, common, sm.tau_hp, sm.robust).m)
    ratio = np.sqrt(np.diag(cov))[5:-5] / np.std(xs, axis=0)[5:-5]
    assert 0.85 < np.median(ratio) < 1.15


# -- the CLI and cache ----------------------------------------------------------------

@pytest.fixture
def three_profiles(tmp_path, monkeypatch):
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.session.store import Profile
    from flatiron.sim.listener import design_example
    from flatiron.sim.profile import simulate_profile

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    out = []
    for n, name in enumerate(("One", "Two", "Three")):
        p = Profile.open(name)
        simulate_profile(p, {e: design_example(e) for e in "LR"}, sessions=1, trials_per_ear=50, seed=n + 5,
                         config=SchedulerConfig(min_trials_noise_fit=10_000, estimate_tau=False))
        out.append(p)
    return out


def test_compare_csv_and_consensus_cli(three_profiles, tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.fit.run import consensus_dir, current_fit, profile_settings
    from flatiron.session.store import Profile

    one, two, three = three_profiles
    monkeypatch.setattr("webbrowser.open", lambda *_: None)
    assert main(["report", "--compare", "One"]) == 1
    assert main(["report", "--compare", "One", "Nope"]) == 1
    capsys.readouterr()
    assert main(["report", "--compare", "One", "Two", "Three", "-H", "--ear", "L", "-w", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "soft median of 3 profiles" in out and "Right ear" not in out
    page = (tmp_path / "flatiron" / "cache" / "comparison.html").read_text()
    assert page.count("<picture>") == 1 and "Left ear" in page
    assert main(["report", "--compare", "One", "Two"]) == 0
    assert "differences, not outliers" in capsys.readouterr().out

    csv = tmp_path / "out.csv"
    assert main(["report", "-p", "One", "--csv", str(csv)]) == 0
    lines = csv.read_text().splitlines()
    assert lines[0].startswith("ear,freq_hz,level_db,level_sd") and lines[1].startswith("L,") and lines[-1].startswith("R,")

    capsys.readouterr()
    assert main(["config", "-p", "One", "-r", "consensus:Two,Nope"]) == 1 and "settings" not in one.read()
    assert main(["config", "-p", "One", "-r", "consensus:One"]) == 1
    assert main(["config", "-p", "One", "-r", "consensus:Two,Three"]) == 0
    assert one.read()["settings"]["reference"]["type"] == "consensus"
    assert one.read()["settings"]["reference"]["profiles"] == ["two", "three"]
    assert profile_settings(one).reference == "iso226"
    assert main(["report", "-p", "One", "-H"]) == 0
    out = capsys.readouterr().out
    assert "Consensus of One, Three, Two" in out
    entries = list(consensus_dir(tmp_path / "flatiron").glob("*.npz"))
    assert len(entries) == 2  # one per ear

    # A second member of the same set shares the entries.
    assert main(["config", "-p", "Two", "-r", "consensus:One,Three"]) == 0
    assert current_fit(two).correction.ears["L"].consensus.key in {p.stem for p in entries}
    assert len(list(consensus_dir(tmp_path / "flatiron").glob("*.npz"))) == 2

    # Another W means another entry; a new mode doesn't.
    assert main(["config", "-p", "One", "--consensus-mode", "match"]) == 0
    assert profile_settings(one).consensus_mode == "match"
    assert len(list(consensus_dir(tmp_path / "flatiron").glob("*.npz"))) == 2
    assert main(["config", "-p", "One", "-w", "0.5"]) == 0
    current_fit(one)
    assert len(list(consensus_dir(tmp_path / "flatiron").glob("*.npz"))) == 4

    # Combinations the rule doesn't cover yet are refused.
    capsys.readouterr()
    assert main(["config", "-p", "One", "--broad"]) == 1
    assert main(["config", "-p", "One", "--method", "smooth"]) == 1
    # Pooling and a consensus don't mix.
    assert main(["config", "-p", "Three", "--pool", "Two"]) == 0
    assert main(["config", "-p", "Three", "-r", "consensus:Two"]) == 1
    assert "share trials" in capsys.readouterr().out
    # One other profile: allowed, with a warning.
    assert main(["config", "-p", "Three", "--pool", "none"]) == 0
    assert main(["config", "-p", "Three", "-r", "consensus:Two"]) == 0
    assert "single other headphone" in capsys.readouterr().out
    # A fixed reference ends it.
    assert main(["config", "-p", "Three", "-r", "none"]) == 0
    assert profile_settings(three).consensus == () and profile_settings(three).reference == "none"
    assert main(["config", "-p", "Two", "--reset", "reference"]) == 0
    assert profile_settings(Profile("two")).consensus == ()


def test_a_new_session_in_a_member_changes_the_key(three_profiles):
    from flatiron.fit.run import current_fit, set_profile_settings
    from flatiron.fit.pipeline import CorrectionSettings
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.sim.listener import design_example
    from flatiron.sim.profile import simulate_profile

    one, two, _ = three_profiles
    set_profile_settings(one, CorrectionSettings(consensus=("two", "three")))
    before = current_fit(one).correction.ears["L"].consensus.key
    simulate_profile(two, {e: design_example(e) for e in "LR"}, sessions=1, trials_per_ear=10, seed=9,
                     config=SchedulerConfig(min_trials_noise_fit=10_000, estimate_tau=False))
    assert current_fit(one).correction.ears["L"].consensus.key != before


def test_consensus_cache_is_pruned_oldest_first(tmp_path):
    from flatiron.fit.run import _prune_consensus, consensus_dir
    import os

    d = consensus_dir(tmp_path)
    d.mkdir(parents=True)
    for n, age in (("old", 300), ("mid", 200), ("new", 100)):
        p = d / f"{n}.npz"
        p.write_bytes(b"x" * 1000)
        os.utime(p, (1_000_000 - age, 1_000_000 - age))
    _prune_consensus(tmp_path, cap=2000)
    assert sorted(p.stem for p in d.glob("*.npz")) == ["mid", "new"]


def test_profile_kinds(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.session.store import Profile

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    assert main(["profiles", "new", "Etude", "--kind", "iem"]) == 0
    assert main(["profiles", "new", "Etude"]) == 1
    p = Profile("etude")
    assert p.kind == "iem"
    assert main(["profiles", "set", "Etude", "--kind", "headphone", "--label", "Etude 2"]) == 0
    assert p.kind == "headphone" and p.read()["label"] == "Etude 2"
    assert main(["profiles", "set", "Nope", "--kind", "iem"]) == 1
    capsys.readouterr()
    assert main(["profiles"]) in (0, None)
    assert "Etude 2" in capsys.readouterr().out
