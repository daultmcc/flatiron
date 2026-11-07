import numpy as np
import pytest

from flatiron.fit.compare import Member, compare_ear
from flatiron.fit.consensus import Source, consensus_of, estimate_tau_hp, raw_consensus, soft_median
from flatiron.fit.filters import response
from flatiron.fit.features import find_features
from flatiron.fit.pipeline import CorrectionSettings, correct_ear, ear_reference
from flatiron.fit.reference import reference
from flatiron.fit.veto import (Candidate, correction, correction_rule, judge, judge_soft, moved_share, overlap_share,
                               overlaps, soft_correction)
from flatiron.model.grid import common_grid, interp_matrix, log_grid
from flatiron.model.probit import ProbitFit

G = log_grid()
X = np.log2(G)


def bump(fc, fwhm, h, grid=G):
    return h * np.exp(-0.5 * ((np.log2(grid) - np.log2(fc)) / (fwhm / 2.355)) ** 2)


def src(R, sd=0.05, name="p", grid=G):
    return Source(name, grid, np.asarray(R, float), np.eye(grid.size) * sd**2)


# -- the veto rule: the table in DESIGN.md, "Veto against other profiles" -----------------------

def _feature(height, fc=7000.0, width=0.2, label="x"):
    return Candidate(label, fc, width, float(height)) if height else None


def _judged(*heights, fc=float(G[np.argmin(np.abs(G - 7000))]), width=0.2):
    """Feature heights, this profile's first, at one place on the grid."""
    own = Candidate("own", fc, width, float(heights[0]))
    voters = {f"v{n}": [Candidate(f"v{n}", fc, width, float(h))] if h else [] for n, h in enumerate(heights[1:])}
    return own, judge(own, voters)


def _corrected_height(r, own):
    x = np.log2(G)
    k = np.argmin(np.abs(G - own.fc))
    width = own.width_oct / 2.355
    R = own.height_db * np.exp(-0.5 * ((x - np.log2(own.fc)) / width) ** 2)
    return correction(R, x, slice(0, G.size), r, np.sign(own.height_db), 1.0, 1.5)[k]


@pytest.mark.parametrize("heights,median,left", [
    ((8, 8, 8), "own", 0),  # in every headphone, the same height
    ((8, 0, 0), None, -8),  # only in this one
    ((8, 8, 0), "own", 0),  # in this one and one other
    ((8, 6, 6), 6, -2),  # taller here
    ((4, 8, 8), 8, 0),  # shorter here: no boost, at the same place
    ((-8, -6, -6), -6, 2),  # the mirror image for a dip
    ((-8, 0, 0), None, 8),
])
def test_veto_rule(heights, median, left):
    own, r = _judged(*heights)
    if median == "own":
        assert r.median is own
    elif median is None:
        assert r.median is None
    else:
        assert r.median.height_db == pytest.approx(median) and not r.moved
    if r.median is not own:
        assert _corrected_height(r, own) == pytest.approx(left, abs=1e-6)


def test_opposite_sign_in_the_others_counts_as_nothing():
    """A neighbouring dip isn't evidence against this peak."""
    own, r = _judged(5, -3, -3)
    assert r.median is None and all(not c.present for c in r.candidates[1:])


def test_two_profiles_have_no_majority_so_the_median_is_their_average():
    own, r = _judged(8, 0)
    assert r.median.height_db == pytest.approx(4) and _corrected_height(r, own) == pytest.approx(-4)
    own, r = _judged(8, 6)
    assert r.median.height_db == pytest.approx(7)


def test_overlap_is_of_half_height_widths():
    assert overlaps(7000, 0.2, 7000 * 2**0.19, 0.2)
    assert not overlaps(7000, 0.2, 7000 * 2**0.21, 0.2)
    # So a peak close to, but clear of, this one doesn't count for it.
    own = Candidate("own", 7000.0, 0.2, 6.0)
    assert judge(own, {"a": [Candidate("a", 7000 * 2**0.3, 0.2, 6.0)], "b": []}).median is None


def test_the_tallest_overlapping_same_sign_feature_is_the_voters():
    own = Candidate("own", 7000.0, 0.3, 6.0)
    a = [Candidate("a", 6800.0, 0.2, 2.0), Candidate("a", 7300.0, 0.2, 5.0), Candidate("a", 7100.0, 0.2, -9.0)]
    assert judge(own, {"a": a, "b": []}).candidates[1].height_db == 5.0


def test_a_peak_between_two_others_is_not_taken_for_their_median():
    """Independent medians of frequency would return this peak's own frequency."""
    own = Candidate("own", 8200.0, 0.32, 7.4)
    r = judge(own, {"a": [Candidate("a", 7060.0, 0.18, 5.4)], "b": [Candidate("b", 7160.0, 0.13, 3.7)]})
    assert r.median is not None and r.median.label in ("a", "b") and r.moved


def test_a_real_candidate_is_chosen_not_a_mix_of_them():
    own = Candidate("own", 7000.0, 0.3, 8.0)
    a, b = Candidate("a", 7300.0, 0.2, 5.0), Candidate("b", 6800.0, 0.25, 6.0)
    assert judge(own, {"a": [a], "b": [b]}).median in (own, a, b)


def test_a_move_imports_only_up_to_the_limit_and_a_same_place_median_imports_nothing():
    x = np.log2(G)
    own = Candidate("own", 8200.0, 0.2, 7.0)
    R = own.height_db * np.exp(-0.5 * ((x - np.log2(8200)) / (0.2 / 2.355)) ** 2)
    span = slice(0, G.size)
    moved = judge(own, {"a": [Candidate("a", 7000.0, 0.2, 6.0)], "b": [Candidate("b", 7000.0, 0.2, 6.0)]})
    moved.median = Candidate("a", 7000.0, 0.2, 6.0)
    moved.moved = True
    for limit in (0.0, 1.5, 3.0):
        c = correction(R, x, span, moved, 1.0, 1.0, limit)
        assert c.max() == pytest.approx(limit, abs=1e-6)
        assert c[np.argmin(np.abs(G - 8200))] < -5.0
    moved.moved = False
    assert correction(R, x, span, moved, 1.0, 1.0, 3.0).max() == pytest.approx(0.0, abs=1e-6)


def test_the_limit_is_on_the_net_boost_so_it_does_not_cut_what_the_headphone_has():
    """With energy of its own at the median feature's place, this headphone
    needs no boost there and shouldn't lose that energy."""
    x = np.log2(G)
    at_7k = 6.0 * np.exp(-0.5 * ((x - np.log2(7000)) / (0.2 / 2.355)) ** 2)
    R = at_7k + 7.0 * np.exp(-0.5 * ((x - np.log2(8200)) / (0.2 / 2.355)) ** 2)
    own = Candidate("own", 8200.0, 0.3, 7.0)
    r = judge(own, {"a": [], "b": []})
    r.median, r.moved = Candidate("a", 7000.0, 0.2, 6.0), True
    c = correction(R, x, slice(0, G.size), r, 1.0, 1.0, 1.5)
    assert abs(c[np.argmin(np.abs(G - 7000))]) < 0.5
    assert c[np.argmin(np.abs(G - 8200))] < -5.0


# -- the pointwise rule ----------------------------------------------------------

@pytest.mark.parametrize("R,m,want", [(6, 0, -6), (6, 6, 0), (9, 6, -3), (0, 6, 0), (-5, 0, 5), (6, -2, -6),
                                      (-5, -5, 0), (-7, -5, 2), (-5, 2, 5)])
def test_pointwise_rule(R, m, want):
    assert correction_rule(R, m) == pytest.approx(want)


# -- soft median -----------------------------------------------------------------

def test_the_worked_values_in_the_design():
    """A +6 dB peak on one of three gives m near +0.6; on two of three, near +5.4."""
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


# -- comparing -----------------------------------------------------------------------

def _member(L, name, kind="headphone", sd=0.3):
    return Member(name, kind, {"L": ProbitFit(G, L - L.mean(), np.eye(G.size) * sd**2, 1.0, 0.5, 0.02, 600, 0.0, True)})


NONE = CorrectionSettings(reference="none", consistency=False)


def test_one_headphone_standing_out_is_flagged_on_it_alone():
    c = compare_ear("L", [_member(bump(5800, 0.2, 5.0), "a"), _member(np.zeros(G.size), "b"),
                          _member(np.zeros(G.size), "c")], NONE)
    assert {s.label for s in c.flags} == {"a"}
    s = c.flags[0]
    assert abs(np.log2(s.fc / 5800)) < 1 / 24 and s.correction_db < -3.0 and s.reading == "peak only here"
    assert c.differ[np.argmin(np.abs(c.consensus.grid - 5800))]


def test_a_feature_every_headphone_shares_is_not_flagged():
    shared = bump(7000, 0.15, 6.0)
    c = compare_ear("L", [_member(shared, n) for n in "abc"], NONE)
    assert not c.flags
    assert len(c.groups) >= 1 and any(len(g) == 3 for g in c.groups)


def test_a_broad_difference_shows_in_the_strip_but_is_not_flagged():
    """The reason the comparison stopped using residuals."""
    broad = bump(3000, 1.5, 7.0)
    c = compare_ear("L", [_member(broad, "a"), _member(np.zeros(G.size), "b"), _member(np.zeros(G.size), "c")], NONE)
    assert not [s for s in c.flags if abs(np.log2(s.fc / 3000)) < 0.5]
    assert c.differ[np.argmin(np.abs(c.consensus.grid - 3000))]


def test_a_peak_elsewhere_on_the_others_says_so():
    here, there = bump(8000, 0.3, 7.0), bump(7000, 0.3, 6.0)
    c = compare_ear("L", [_member(here, "a"), _member(there, "b"), _member(there, "c")], NONE)
    assert next(s for s in c.flags if s.label == "a").reading == "peak elsewhere on the others"


def test_a_dip_is_described_as_a_dip():
    dip, less = bump(5000, 0.2, -6.0), bump(5000, 0.2, -3.0)
    c = compare_ear("L", [_member(dip, "a"), _member(less, "b"), _member(less, "c")], NONE)
    assert next(s for s in c.flags if s.label == "a").reading == "deeper here than on the others"


def test_rows_do_not_chain_features_far_apart():
    """Each overlaps the next, but the first and last are an octave apart."""
    from flatiron.fit.compare import Seen, _grouped

    seen = [Seen(n, True, fc, 0.6, 5.0, 0.0, None) for n, fc in (("a", 4000.0), ("b", 5600.0), ("c", 8000.0))]
    assert sorted(len(g) for g in _grouped(seen)) == [1, 2]
    assert len(_grouped([Seen(n, True, 7000.0, 0.2, 5.0, 0.0, None) for n in "abc"])) == 1
    # A profile only appears once per row.
    assert len(_grouped([Seen("a", True, fc, 0.6, 5.0, 0.0, None) for fc in (7000.0, 7100.0)])) == 2


def test_offsets_align_the_curves():
    L = bump(3000, 1.0, 5.0)
    fits = [_member(L, n) for n in "ab"]
    fits[0].fits["L"].L = fits[0].fits["L"].L + 6.0
    c = compare_ear("L", fits + [_member(L, "c", "iem")], NONE)
    assert c.offsets["a"] - c.offsets["b"] == pytest.approx(6.0, abs=0.3)
    assert "c" in c.offsets


def test_other_kinds_are_shown_but_do_not_vote():
    a, b = bump(7000, 0.15, 6.0), np.zeros(G.size)
    c = compare_ear("L", [_member(a, "x"), _member(a, "y"), _member(b, "iem", "iem")], NONE)
    assert c.voters == 2 and c.voting == {"x": True, "y": True, "iem": False}
    # It's measured against them without counting: its own peak isn't shared by voting for itself.
    c = compare_ear("L", [_member(b, "x"), _member(b, "y"), _member(a, "iem", "iem")], NONE)
    assert [s.label for s in c.flags] == ["iem"] and c.flags[0].correction_db < -3.0
    with pytest.raises(ValueError, match="include-kind"):
        compare_ear("L", [_member(a, "x"), _member(b, "iem", "iem")], NONE)
    assert compare_ear("L", [_member(a, "x"), _member(b, "iem", "iem")], NONE,
                       voting_kinds=("headphone", "iem")).voters == 2


# -- the raw consensus ---------------------------------------------------------

def test_offsets_align_the_curves_despite_a_treble_difference():
    """A mean-based alignment would shift the whole curve; these don't."""
    base = bump(3000, 1.0, 5.0)
    treble = 8.0 * ((G > 8000) * np.minimum((np.log2(G) - np.log2(8000)) * 8, 1.0))
    sm = raw_consensus([src(base + 4.0 + treble, 0.3, "a"), src(base - 1.0, 0.3, "b"), src(base - 3.0, 0.3, "c")])
    assert sm.offsets.sum() == pytest.approx(0.0, abs=1e-9)
    assert sm.offsets == pytest.approx([4.0, -1.0, -3.0], abs=0.5)
    k = (sm.grid > 1000) & (sm.grid < 6000)
    want = bump(3000, 1.0, 5.0, sm.grid)
    assert np.max(np.abs(sm.m[k] - want[k])) < 0.5


def test_a_shift_in_every_curve_shifts_the_consensus_and_nothing_else():
    """So the reference used to take a raw curve doesn't matter (ISO 226 has
    corners, which the curvature penalty sees, hence the tolerance)."""
    rng = np.random.default_rng(3)
    curves = [bump(5000, 0.3, 5.0) + rng.normal(0, 0.2, G.size), bump(7000, 0.3, 4.0) + 1.0, bump(9000, 0.3, 3.0) - 2.0]
    E = reference("iso226", G, 60.0)
    plain = raw_consensus([src(c, 0.5, str(n)) for n, c in enumerate(curves)])
    moved = raw_consensus([src(c - E, 0.5, str(n)) for n, c in enumerate(curves)])
    a = plain.on(G)[0]
    b = moved.on(G)[0] + E
    assert np.sqrt(np.mean((a - a.mean() - (b - b.mean())) ** 2)) < 0.5


def test_one_member_is_that_curve():
    L = bump(5000, 0.5, 5.0)
    sm = raw_consensus([src(L, 0.3)])
    assert sm.offsets == pytest.approx([0.0])
    assert np.max(np.abs(sm.on(G)[0] - L)) < 0.3


def test_smoothing_keeps_broad_shape_and_drops_narrow_peaks():
    broad, narrow = bump(3000, 1.5, 6.0), bump(7000, 0.15, 6.0)
    sm = raw_consensus([src(broad + narrow, 0.1, n) for n in "abc"])
    raw = ear_reference(sm, list("abc"), "", 0.0)
    soft = ear_reference(sm, list("abc"), "", 0.5)
    level = lambda e, f: e.m[np.argmin(np.abs(e.grid - f))]
    assert level(raw, 7000) > 5.0 and level(soft, 7000) < 3.0
    assert level(soft, 3000) == pytest.approx(level(raw, 3000), abs=0.5)
    assert np.all(np.diag(soft.cov)[20:-20] <= np.diag(raw.cov)[20:-20] + 1e-9)


def test_difference_covariance_includes_the_correlation_with_its_own_vote():
    """L − m shares a member's own noise, so its variance is less than if they
    were independent, and a non-member's isn't."""
    f = [_fit_of(np.zeros(G.size)) for _ in range(3)]
    srcs = [Source(n, G, np.zeros(G.size), x.cov) for n, x in zip("abc", f)]
    sm = raw_consensus(srcs)
    member = ear_reference(sm, list("abc"), "", 0.0, own=0)
    other = ear_reference(sm, list("abc"), "", 0.0)
    cov = member.difference_cov(srcs[0].cov, G)
    assert np.all(np.diag(cov)[5:-5] < np.diag(other.difference_cov(srcs[0].cov, G))[5:-5])
    # Monte Carlo agrees.
    rng = np.random.default_rng(2)
    A = interp_matrix(sm.grid, G)
    xs = []
    for _ in range(400):
        noisy = [Source(s.label, G, rng.multivariate_normal(np.zeros(G.size), s.cov), s.cov) for s in srcs]
        xs.append(noisy[0].R - A @ soft_median(noisy, sm.grid, sm.tau_hp, sm.robust).m)
    ratio = np.sqrt(np.diag(cov))[5:-5] / np.std(xs, axis=0)[5:-5]
    assert 0.85 < np.median(ratio) < 1.15


# -- the correction ------------------------------------------------------------------

def _fit_of(L, sd=0.3):
    return ProbitFit(G, L - L.mean(), np.eye(G.size) * sd**2, 1.0, 0.5, 0.02, 600, 0.0, True)


def _reference(fits, smooth=0.5, own=None):
    names = [f"h{n}" for n in range(len(fits))]
    sm = raw_consensus([Source(n, f.grid, f.L, f.cov) for n, f in zip(names, fits)])
    return ear_reference(sm, names, "", smooth, own)


def _corrected(curves, i=0, consensus=None, smooth=0.5, veto=False, **settings):
    """consensus: None for a fixed flat reference, "all" or "others"."""
    fits = [_fit_of(L) for L in curves]
    st = dict(reference="none", consistency=False, consensus_smooth_oct=smooth)
    ref = None
    if consensus:
        members = fits if consensus == "all" else [f for n, f in enumerate(fits) if n != i]
        ref = _reference(members, smooth, i if consensus == "all" else None)
        st["reference"] = "consensus:" + ",".join(f"h{n}" for n in range(len(members)))
    voters = None
    if veto:
        voters = [(f"h{n}", f) for n, f in enumerate(fits) if n != i]
        st["veto"] = tuple(v[0] for v in voters)
    return correct_ear("L", fits[i], CorrectionSettings(**{**st, **settings}), consensus=ref, voters=voters)


OWN, SHARED = bump(5800, 0.2, 5.0), bump(7500, 0.2, 6.0)


def _at(c, *freqs):
    return response(c.added, np.array(freqs, float))


@pytest.fixture(params=["feature", "pointwise"])
def mode(request):
    return request.param


def test_veto_removes_a_peak_only_this_headphone_has_and_keeps_a_shared_one(mode):
    h = _at(_corrected([OWN + SHARED, SHARED, SHARED], veto=True, veto_mode=mode), 5800, 7500)
    assert h[0] < -3.0 and abs(h[1]) < 1.0
    # Without the veto both go.
    h = _at(_corrected([OWN + SHARED, SHARED, SHARED]), 5800, 7500)
    assert h[0] < -3.0 and h[1] < -3.0


def test_veto_gives_a_headphone_without_the_peak_nothing_to_remove(mode):
    h = _at(_corrected([OWN + SHARED, SHARED, SHARED], i=1, veto=True, veto_mode=mode), 5800, 7500)
    assert abs(h[0]) < 1.0 and abs(h[1]) < 1.0


def test_veto_trims_a_taller_peak_to_the_shared_height(mode):
    c = _corrected([bump(7500, 0.2, 9.0), bump(7500, 0.2, 6.0), bump(7500, 0.2, 6.0)], veto=True, veto_mode=mode)
    assert _at(c, 7500)[0] == pytest.approx(-3.0, abs=1.0)


def test_a_lobe_merged_with_a_shared_peak_is_corrected_only_for_its_own_part(mode):
    """One number can't do this: the two parts need different shares."""
    near = bump(6000, 0.2, 6.0)
    shared = bump(7000, 0.2, 6.0)
    h = _at(_corrected([near + shared, shared, shared], veto=True, veto_mode=mode), 6000, 7000)
    assert h[0] < -3.0 and abs(h[1]) < 2.0


def test_veto_moves_a_peak_only_as_far_as_its_limit():
    """An 8.2 kHz peak where the others have one at 7 kHz, overlapping."""
    here, there = bump(8000, 0.3, 7.0), bump(7000, 0.3, 6.0)
    for limit in (0.0, 1.5):
        c = _corrected([here, there, there], veto=True, veto_import_db=limit)
        f = next(x for x in c.features if x.veto is not None)
        assert f.veto.moved and f.veto.median.fc == pytest.approx(7000, rel=0.03)
        assert _at(c, 8000)[0] < -3.0
    # Tonality compensation accounts for what's above zero here.
    assert _at(_corrected([here, there, there], veto=True, veto_import_db=0.0), 7000)[0] < 1.5


def test_voters_and_members_may_be_on_other_grids(mode):
    """Refined grids differ between profiles, with this one in the consensus."""
    fine = log_grid(step_oct=1 / 32)
    on = lambda g, L: ProbitFit(g, L, np.eye(g.size) * 0.09, 1.0, 0.5, 0.02, 600, 0.0, True)
    own = on(G, bump(5800, 0.2, 5.0) + bump(7500, 0.2, 6.0))
    a, b = on(fine, bump(7500, 0.2, 6.0, fine)), on(G, bump(7500, 0.2, 6.0))
    ref = ear_reference(raw_consensus([Source(n, f.grid, f.L, f.cov) for n, f in zip("oab", (own, a, b))]),
                        list("oab"), "", 0.5, own=0)
    st = CorrectionSettings(reference="consensus:o,a,b", consistency=False, veto=("a", "b"), veto_mode=mode)
    c = correct_ear("L", own, st, consensus=ref, voters=[("a", a), ("b", b)])
    h = _at(c, 5800, 7500)
    assert h[0] < -2.0 and abs(h[1]) < 1.5


def test_an_undetected_lobe_still_counts_for_a_voter():
    """A 2 dB bump that isn't significant at this noise isn't nothing."""
    from flatiron.fit.pipeline import voter_candidates

    f = _fit_of(bump(7000, 0.2, 2.0), sd=1.5)
    got = voter_candidates("v", f, CorrectionSettings(reference="none", consistency=False), None)
    assert any(abs(np.log2(c.fc / 7000)) < 0.1 and c.height_db > 1.0 for c in got)


def test_veto_applies_to_broad_features_with_broad_on_and_not_to_kept_tonality():
    a = bump(3500, 0.8, 6.0)
    kept = _corrected([a, a, a], veto=True)
    assert all(x.veto is None for x in kept.features if x.kind == "broad")
    broad = _corrected([a, a, a], veto=True, preserve_tonality=False)
    assert any(x.veto is not None for x in broad.features if x.kind == "broad")
    assert abs(_at(broad, 3500)[0]) < 1.0
    alone = _corrected([a, np.zeros(G.size), np.zeros(G.size)], veto=True, preserve_tonality=False)
    assert _at(alone, 3500)[0] < -3.0


def test_a_consensus_replaces_the_reference():
    shape = bump(3000, 1.2, 8.0)
    c = _corrected([shape, shape, shape], consensus="all", preserve_tonality=False)
    assert np.max(np.abs(c.E - (c.L - c.baseline - c.R))) < 1e-9
    assert all(abs(f.peak_db) < 1.0 for f in c.features if f.accepted)  # only what smoothing left behind
    # A fixed flat reference sees that whole shape as a feature.
    assert [f for f in _corrected([shape], preserve_tonality=False).features if f.accepted and f.kind == "broad"]


def test_unsmoothed_consensus_keeps_a_shared_peak_and_smoothed_leaves_it_to_the_veto():
    curves = [OWN + SHARED, SHARED, SHARED]
    assert abs(_at(_corrected(curves, consensus="all", smooth=0.0), 7500)[0]) < 1.0
    assert _at(_corrected(curves, consensus="all", smooth=0.5), 7500)[0] < -1.0
    assert abs(_at(_corrected(curves, consensus="all", smooth=0.5, veto=True), 7500)[0]) < 1.0


def test_a_profile_is_only_in_its_consensus_if_listed():
    shape = bump(5000, 0.2, 6.0)
    # With itself out, none of its peak is in the reference.
    out = _corrected([shape, np.zeros(G.size), np.zeros(G.size)], consensus="others", smooth=0.0)
    assert out.consensus.G is None and _at(out, 5000)[0] < -4.0
    assert _corrected([shape, np.zeros(G.size), np.zeros(G.size)], consensus="all", smooth=0.0).consensus.G is not None


def test_veto_and_consensus_work_with_broad_on(mode):
    a = bump(3500, 0.8, 5.0)
    c = _corrected([a + OWN, a, np.zeros(G.size)], consensus="all", veto=True, veto_mode=mode, preserve_tonality=False)
    assert _at(c, 5800)[0] < -2.0


def test_match_corrects_toward_the_reference_tonality_included():
    shape = bump(3000, 2.0, 5.0)
    c = _corrected([shape + OWN], method="match", bands=32)
    assert not c.compensation
    assert np.max(np.abs(c.baseline)) == 0.0
    h = _at(c, 1200, 3000, 5800)
    assert h[1] - h[0] < -3.0 and h[2] < h[0] - 2.0
    # The level is free: a shift in the curve changes nothing.
    k = np.argmin(np.abs(c.freqs - 3000))
    assert c.target[k] == pytest.approx(_corrected([shape + OWN + 5.0], method="match", bands=32).target[k], abs=1e-6)


def test_match_is_robust_to_where_its_level_comes_from():
    """A median alignment: a narrow excursion doesn't move the rest of the curve."""
    flat = _corrected([np.zeros(G.size)], method="match")
    assert np.max(np.abs(flat.target)) < 1e-6
    c = _corrected([bump(5000, 0.3, 8.0)], method="match")
    assert np.abs(c.target[np.argmin(np.abs(c.freqs - 1500))]) < 0.5


def test_match_to_a_consensus_goes_to_the_others():
    c = _corrected([np.zeros(G.size), bump(4000, 1.0, 5.0), bump(4000, 1.0, 5.0)], consensus="others", smooth=0.0,
                   method="match")
    assert c.target[np.argmin(np.abs(c.freqs - 4000))] > 2.0


@pytest.mark.parametrize("kw", [dict(veto=("a",), method="smooth"), dict(veto=("a",), method="match"),
                                dict(consensus_smooth_oct=3.0), dict(reference="consensus:"),
                                dict(veto_mode="nope"), dict(veto_import_db=-1.0)])
def test_settings_that_make_no_sense_are_refused(kw):
    with pytest.raises(ValueError):
        CorrectionSettings(**kw)


def test_smooth_method_takes_a_consensus_reference():
    c = _corrected([OWN + SHARED, SHARED, SHARED], consensus="all", smooth=0.0, method="smooth")
    assert c.consensus is not None


def test_a_correction_without_its_consensus_or_votes_is_refused():
    f = _fit_of(np.zeros(G.size))
    with pytest.raises(ValueError, match="consensus"):
        correct_ear("L", f, CorrectionSettings(reference="consensus:a"))
    with pytest.raises(ValueError, match="veto"):
        correct_ear("L", f, CorrectionSettings(reference="none", veto=("a",)))


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
    assert page.count("<picture>") == 1 and "Left ear" in page and "All features" in page
    assert main(["report", "--compare", "One", "Two"]) == 0
    assert "differences, not outliers" in capsys.readouterr().out

    csv = tmp_path / "out.csv"
    assert main(["report", "-p", "One", "--csv", str(csv)]) == 0
    lines = csv.read_text().splitlines()
    assert lines[0].startswith("ear,freq_hz,level_db,level_sd") and lines[1].startswith("L,") and lines[-1].startswith("R,")

    capsys.readouterr()
    assert main(["config", "-p", "One", "-r", "consensus:Two,Nope"]) == 1 and "settings" not in one.read()
    assert main(["config", "-p", "One", "-r", "consensus:Two,Three"]) == 0
    assert one.read()["settings"]["reference"] == "consensus:two,three"
    assert profile_settings(one).consensus == ("two", "three")
    assert main(["report", "-p", "One", "-H"]) == 0
    out = capsys.readouterr().out
    assert "raw curves of Three, Two, smoothed over 0.5 octave" in out
    assert "weighted mean" in out
    assert current_fit(one).correction.ears["L"].consensus.G is None  # it isn't a member
    entries = list(consensus_dir(tmp_path / "flatiron").glob("*.npz"))
    assert len(entries) == 2  # one per ear

    # The same list from another profile shares the entries; another list doesn't.
    assert main(["config", "-p", "Three", "-r", "consensus:Three,Two"]) == 0
    assert current_fit(three).correction.ears["L"].consensus.G is not None  # listed, so it's a member
    assert len(list(consensus_dir(tmp_path / "flatiron").glob("*.npz"))) == 2
    assert main(["config", "-p", "Three", "-r", "consensus:Two"]) == 0
    assert "single headphone" in capsys.readouterr().out
    current_fit(three)
    assert len(list(consensus_dir(tmp_path / "flatiron").glob("*.npz"))) == 4

    # Neither the width nor the smoothing is part of the raw entry.
    assert main(["config", "-p", "One", "-w", "0.5", "--consensus-smooth", "0.25"]) == 0
    assert current_fit(one).correction.ears["L"].consensus.smooth_oct == 0.25
    assert len(list(consensus_dir(tmp_path / "flatiron").glob("*.npz"))) == 4
    assert main(["config", "-p", "One", "--reset", "width", "consensus-smooth"]) == 0

    # The combinations that used to be refused aren't any more.
    assert main(["config", "-p", "One", "--broad"]) == 0
    current_fit(one)
    assert main(["config", "-p", "One", "--no-broad", "--method", "smooth"]) == 0
    current_fit(one)
    assert main(["config", "-p", "One", "--method", "match"]) == 0
    current_fit(one)
    assert main(["config", "-p", "One", "--veto", "Two"]) == 1  # veto judges features
    assert main(["config", "-p", "One", "--method", "features", "--veto", "Two,Three,One"]) == 0
    st = profile_settings(one)
    assert st.veto == ("two", "three", "one") and st.consensus == ("two", "three")
    capsys.readouterr()
    assert main(["report", "-p", "One", "-H"]) == 0
    out = capsys.readouterr().out
    assert "Veto: each feature" in out and "adding at most 1.5 dB" in out
    assert main(["config", "-p", "One", "--veto-mode", "pointwise"]) == 0
    assert main(["config", "-p", "One", "--veto-import", "3"]) == 1  # only for the feature mode
    capsys.readouterr()
    assert main(["report", "-p", "One", "-H"]) == 0
    assert "frequency by frequency" in capsys.readouterr().out
    assert main(["config", "-p", "One", "--veto-mode", "feature", "--veto-import", "3"]) == 0
    assert profile_settings(one).veto_import_db == 3.0
    assert main(["config", "-p", "One", "--reset", "veto-import"]) == 0
    assert main(["config", "-p", "One", "--veto", "One"]) == 1  # no one else to vote
    assert main(["config", "-p", "One", "--veto", "Nope"]) == 1
    assert main(["config", "-p", "One", "--veto", "none"]) == 0 and profile_settings(one).veto == ()

    assert main(["config", "-p", "Two", "--veto-mode", "pointwise"]) == 1  # needs --veto
    # --consensus-smooth only means something with a consensus.
    assert main(["config", "-p", "Two", "--consensus-smooth", "0.25"]) == 1

    # Pooling and a consensus or a veto don't mix.
    capsys.readouterr()
    assert main(["config", "-p", "Three", "--pool", "Two"]) == 0
    assert main(["config", "-p", "Three", "-r", "consensus:Two"]) == 1
    assert "share trials" in capsys.readouterr().out
    assert main(["config", "-p", "Three", "-r", "none", "--veto", "Two"]) == 1
    assert main(["config", "-p", "One", "-r", "consensus:Two,Three"]) == 1  # Three pools Two
    assert main(["config", "-p", "Three", "--pool", "none"]) == 0
    # A fixed reference ends it, and a comparison works with a consensus configured.
    assert main(["config", "-p", "One", "-r", "consensus:Two,Three"]) == 0
    capsys.readouterr()
    assert main(["report", "--compare", "One", "Two"]) == 0
    assert "isn't used for comparisons" in capsys.readouterr().out
    assert main(["config", "-p", "One", "-r", "none"]) == 0
    assert profile_settings(one).consensus == () and profile_settings(one).reference == "none"
    assert main(["config", "-p", "Three", "--reset", "reference"]) == 0
    assert profile_settings(Profile("three")).consensus == ()


def test_a_new_session_in_a_member_changes_the_key(three_profiles):
    from flatiron.fit.run import current_fit, set_profile_settings
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.sim.listener import design_example
    from flatiron.sim.profile import simulate_profile

    one, two, _ = three_profiles
    set_profile_settings(one, CorrectionSettings(reference="consensus:two,three"))
    before = current_fit(one).correction.ears["L"].consensus.key
    simulate_profile(two, {e: design_example(e) for e in "LR"}, sessions=1, trials_per_ear=10, seed=9,
                     config=SchedulerConfig(min_trials_noise_fit=10_000, estimate_tau=False))
    assert current_fit(one).correction.ears["L"].consensus.key != before


def test_a_missing_member_or_voter_says_how_to_fix_it(three_profiles):
    from flatiron.fit.run import current_fit, set_profile_settings

    one, _, _ = three_profiles
    set_profile_settings(one, CorrectionSettings(reference="consensus:gone"))
    with pytest.raises(ValueError, match="-r iso226"):
        current_fit(one)
    set_profile_settings(one, CorrectionSettings(veto=("gone",)))
    with pytest.raises(ValueError, match="--veto none"):
        current_fit(one)


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


def _soft_height(own, voters, softness=0.1):
    x = np.log2(G)
    k = np.argmin(np.abs(G - own.fc))
    R = own.height_db * np.exp(-0.5 * ((x - np.log2(own.fc)) / (own.width_oct / 2.355)) ** 2)
    r = judge_soft(own, voters, softness)
    return r, soft_correction(R, x, slice(0, G.size), r, own, 1.0, 1.5)[k]


def test_soft_veto_gives_the_hard_answer_for_a_clear_winner():
    own = Candidate("own", 7000, 0.2, 8.0)
    # In every headphone at the same height: this profile is central, nothing to correct.
    r, c = _soft_height(own, {"a": [Candidate("a", 7000, 0.2, 8.0)], "b": [Candidate("b", 7000, 0.2, 8.0)]})
    assert c == pytest.approx(0, abs=0.1)
    # Only here: nothing is the median, so all of it goes.
    r, c = _soft_height(own, {"a": [], "b": []})
    assert r.median is None and c == pytest.approx(-8, abs=0.1)
    # Taller here than the two others, which agree: trimmed to their height.
    r, c = _soft_height(own, {"a": [Candidate("a", 7000, 0.2, 6.0)], "b": [Candidate("b", 7000, 0.2, 6.0)]})
    assert c == pytest.approx(-2, abs=0.1)


def test_soft_veto_has_no_jump_at_a_tie():
    """Like the real case: a voter with a wider, lower feature nearby, and this
    profile's height passing through where the hard median flips."""
    voters = {"a": [], "b": [Candidate("b", 6800, 0.43, 2.6)]}
    x = np.log2(G)
    hard, soft = [], []
    for h in np.arange(2.0, 6.0, 0.01):
        own = Candidate("own", 7000, 0.24, float(h))
        R = h * np.exp(-0.5 * ((x - np.log2(7000)) / (0.24 / 2.355)) ** 2)
        r = judge(own, voters)
        hard.append(np.zeros_like(x) if r.median is own else correction(R, x, slice(0, G.size), r, 1.0, 1.0, 1.5))
        r = judge_soft(own, voters, 0.1)
        soft.append(soft_correction(R, x, slice(0, G.size), r, own, 1.0, 1.5))
    step = lambda c: np.abs(np.diff(np.array(c), axis=0)).max()
    assert step(hard) > 0.5
    assert step(soft) < 0.1


def test_soft_overlap_and_moved_are_gradual_around_the_hard_limits():
    assert overlap_share(7000, 0.2, 7000, 0.2) == 1
    edge = 7000 * 2**0.2
    assert overlap_share(7000, 0.2, edge, 0.2) == pytest.approx(0.5)
    assert overlap_share(7000, 0.2, 7000 * 2**0.4, 0.2) == 0
    assert moved_share(7000, 7000) == 0 and moved_share(7000, 7000 * 2**0.5) == 1
    assert moved_share(7000, 7000 * 2 ** (1 / 12)) == pytest.approx(0.5)


def test_a_voter_counts_less_as_its_feature_leaves():
    own = Candidate("own", 7000, 0.2, 8.0)
    near, far = (judge_soft(own, {"a": [Candidate("a", f, 0.2, 8.0)]}, 0.1).candidates[1] for f in (7000, 7000 * 2**0.25))
    assert near.height_db == pytest.approx(8) and 0 < far.height_db < 4


def test_soft_veto_runs_through_the_pipeline_and_a_centre_ignores_which_extreme_is_deepest():
    corrected = _corrected([OWN + SHARED, SHARED, SHARED], veto=True, veto_softness=0.1)
    x = np.log2(corrected.freqs)
    assert abs(np.interp(np.log2(5800), x, corrected.target) + 5) < 1  # only here: removed
    assert abs(np.interp(np.log2(7500), x, corrected.target)) < 1  # shared: kept
    two = bump(5000, 0.2, 4.0) + bump(5900, 0.2, 4.0)
    a, b = two + bump(5000, 0.05, 0.1), two + bump(5900, 0.05, 0.1)
    fa, fb = (max(find_features(G, c, np.eye(G.size) * 0.04), key=lambda f: abs(f.peak_db)) for c in (a, b))
    assert abs(np.log2(fa.fc / fb.fc)) > 0.15  # the deepest point jumps
    assert abs(np.log2(fa.centre_hz / fb.centre_hz)) < 0.02  # the centre doesn't
