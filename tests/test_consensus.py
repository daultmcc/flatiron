from dataclasses import replace

import numpy as np
import pytest

from flatiron.fit.compare import Member, compare_ear
from flatiron.fit.consensus import (GAIN_SD_RANGE, SplitConsensus, Source, consensus_of, ear_reference,
                                    estimate_colour, estimate_tau_hp, raw_consensus, shares, soft_median,
                                    split_consensus, split_parts)
from flatiron.fit.filters import response
from flatiron.fit.features import find_features
from flatiron.fit.pipeline import CorrectionSettings, consensus_references, correct_ear
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


def test_one_headphone_standing_out_shows_in_the_narrow_strip():
    c = compare_ear("L", [_member(bump(5800, 0.2, 5.0), "a"), _member(np.zeros(G.size), "b"),
                          _member(np.zeros(G.size), "c")])
    assert c.differ_narrow[np.argmin(np.abs(c.grid - 5800))]
    assert not c.differ_narrow[np.argmin(np.abs(c.grid - 2000))]


def test_a_feature_every_headphone_shares_does_not_differ():
    shared = bump(7000, 0.15, 6.0)
    c = compare_ear("L", [_member(shared, n) for n in "abc"])
    assert not c.differ_narrow.any() and not c.differ_broad.any()


def test_a_broad_difference_shows_in_the_broad_strip_only():
    broad = bump(3000, 1.5, 7.0)
    c = compare_ear("L", [_member(broad, "a"), _member(np.zeros(G.size), "b"), _member(np.zeros(G.size), "c")])
    i = np.argmin(np.abs(c.grid - 3000))
    assert c.differ_broad[i] and not c.differ_narrow[i]


def test_the_broad_strip_marks_the_largest_tonal_differences_only():
    """Tonality always differs: a fixed span, not significance, decides what's shaded."""
    small = bump(3000, 1.5, 2.0)
    members = [_member(small, "a"), _member(np.zeros(G.size), "b"), _member(np.zeros(G.size), "c")]
    c = compare_ear("L", members)
    i = np.argmin(np.abs(c.grid - 3000))
    assert 1.0 < c.broad_spread[i] < 2.5 and not c.differ_broad.any()  # less what the alignment takes
    assert compare_ear("L", members, broad_spread_db=1.0).differ_broad[i]


def test_spans_reads_a_mask_as_ranges():
    from flatiron.fit.compare import spans

    g = np.array([1000.0, 2000, 3000, 4000, 5000])
    assert spans(g, np.array([True, True, False, False, True])) == [(1000.0, 2000.0), (5000.0, 5000.0)]
    assert spans(g, np.zeros(5, bool)) == []


def test_offsets_align_the_curves():
    L = bump(3000, 1.0, 5.0)
    fits = [_member(L, n) for n in "ab"]
    fits[0].fits["L"].L = fits[0].fits["L"].L + 6.0
    c = compare_ear("L", fits + [_member(L, "c", "iem")])
    assert c.offsets["a"] - c.offsets["b"] == pytest.approx(6.0, abs=0.3)
    assert c.offsets["c"] == pytest.approx(c.offsets["b"], abs=0.3)  # an IEM is aligned without counting


def test_offsets_ignore_one_headphones_treble_difference():
    """A mean-based alignment would shift the whole curve; the median one doesn't."""
    base = bump(3000, 1.0, 5.0)
    treble = 8.0 * ((G > 8000) * np.minimum((np.log2(G) - np.log2(8000)) * 8, 1.0))
    c = compare_ear("L", [_member(base + treble, "a"), _member(base, "b"), _member(base, "c")])
    k = (c.grid > 1000) & (c.grid < 6000)
    # Below 6 kHz the three aligned curves coincide.
    curves = {m: interp_matrix(G, c.grid) @ (L - L.mean()) - c.offsets[m] for m, L in
              (("a", base + treble), ("b", base), ("c", base))}
    assert np.max(np.abs(curves["a"] - curves["b"])[k]) < 0.3


def test_narrow_parts_are_overlaid_without_a_consensus():
    peak = bump(7000, 0.15, 6.0)
    c = compare_ear("L", [_member(peak + bump(3000, 1.5, 6.0), "a"), _member(peak, "b"), _member(np.zeros(G.size), "c")])
    n, sd = c.narrow["a"]
    i = np.argmin(np.abs(c.grid - 7000))
    assert n[i] > 3.5 and abs(n[np.argmin(np.abs(c.grid - 3000))]) < 1.0  # the broad bump isn't narrow
    assert abs(c.narrow["c"][0][i]) < 0.5 and np.all(sd >= 0)
    assert not hasattr(c, "consensus")


def test_other_kinds_are_shown_but_do_not_count():
    a, b = bump(7000, 0.15, 6.0), np.zeros(G.size)
    c = compare_ear("L", [_member(a, "x"), _member(a, "y"), _member(b, "iem", "iem")])
    assert c.n_counted == 2 and c.counted == {"x": True, "y": True, "iem": False} and not c.fallback
    assert not c.differ_narrow.any()  # the IEM's missing peak doesn't count
    # With fewer than two of a counted kind, all of them count.
    c = compare_ear("L", [_member(a, "x"), _member(b, "iem", "iem")])
    assert c.n_counted == 2 and c.fallback
    c = compare_ear("L", [_member(a, "x"), _member(b, "iem", "iem")], counted_kinds=("headphone", "iem"))
    assert c.n_counted == 2 and not c.fallback
    with pytest.raises(ValueError, match="two profiles"):
        compare_ear("L", [_member(a, "x")])


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


# -- the split consensus reference -----------------------------------------------------

def _split_ref(sources, speakers=(), own=None, **settings):
    sc = split_consensus(list(sources), list(speakers))
    st = CorrectionSettings(reference="consensus:" + ",".join(s.label for s in sources), **settings)
    return sc, consensus_references({"L": sc}, st, "", own, None if own is None else {"L": sources[own].grid})["L"]


def _level(e, f):
    return float(np.interp(np.log2(f), np.log2(e.grid), e.m - e.broad))


def test_shared_narrow_structure_is_kept_in_the_treble_and_mostly_not_low_down():
    broad, high, low = bump(3000, 1.5, 6.0), bump(7000, 0.15, 6.0), bump(1500, 0.15, 3.0)
    _, e = _split_ref([src(broad + high + low, 0.1, n) for n in "abc"])
    assert _level(e, 7000) > 3.5  # the narrow part of a 6 dB peak (the broad part takes ~1 dB), k near 0.85
    assert abs(_level(e, 1500)) < 1.0  # the prior there is 0.5 dB, against 1 dB of coupling
    assert np.interp(np.log2(3000), np.log2(e.grid), e.broad) > 4.0
    # More coupling keeps less; a prior scale of 0 keeps nothing narrow at all.
    _, more = _split_ref([src(broad + high + low, 0.1, n) for n in "abc"], consensus_coupling_db=3.0)
    assert _level(more, 7000) < _level(e, 7000) - 1.0
    _, none = _split_ref([src(broad + high + low, 0.1, n) for n in "abc"], consensus_prior=0.0)
    assert np.max(np.abs(none.m - none.broad)) < 1e-9


def test_a_broad_difference_between_headphones_doesnt_move_the_narrow_median():
    """Raw curves would have the median switch between headphones; split, the
    narrow parts agree."""
    peak = bump(7000, 0.15, 6.0)
    tilts = [2.0 * (X - X.mean()), -1.5 * (X - X.mean()), bump(5000, 1.2, 6.0)]
    sc = split_consensus([src(peak + t, 0.1, n) for n, t in zip("abc", tilts)])
    agree = split_consensus([src(peak, 0.1, n) for n in "abc"])
    k = (sc.grid > 2000) & (sc.grid < 11000)
    assert np.max(np.abs(sc.narrow.m - agree.narrow.m)[k]) < 0.7
    assert np.max(np.sqrt(sc.excess)[k]) < 0.5


def test_disagreement_keeps_less_where_the_headphones_differ():
    rng = np.random.default_rng(4)
    x = np.log2(G)
    ripple = lambda: np.convolve(rng.normal(0, 1, G.size), np.exp(-0.5 * (np.arange(-6, 7) / 2.0) ** 2), "same")
    peak = bump(7000, 0.15, 5.0) + bump(10000, 0.15, 5.0)
    calm = split_consensus([src(peak, 0.1, n) for n in "abc"])
    rough = split_consensus([src(peak + 2.0 * ripple() * (x > np.log2(9000)), 0.1, n) for n in "abc"])
    sh_calm, sh_rough = shares({"L": calm}).ears["L"], shares({"L": rough}).ears["L"]
    at = lambda sh, sc, f: float(np.interp(np.log2(f), np.log2(sc.grid), sh.k_hp))
    assert at(sh_rough, rough, 11000) < at(sh_calm, calm, 11000) - 0.05
    assert at(sh_rough, rough, 7000) == pytest.approx(at(sh_calm, calm, 7000), abs=0.05)


def test_a_split_consensus_survives_its_cache_round_trip():
    spk = src(bump(7000, 0.15, 6.0), 0.3, "s")
    sc = split_consensus([src(bump(7000, 0.15, 6.0) + bump(3000, 1.0, 3.0), 0.1, n) for n in "abc"], [spk])
    back = SplitConsensus.from_arrays(dict(sc.arrays()))
    assert back.members == sc.members and [s.label for s in back.speakers] == ["s"]
    for a, b in ((back.broad, sc.broad), (back.narrow, sc.narrow)):
        assert np.allclose(a.m, b.m) and np.allclose(a.cov, b.cov) and all(np.allclose(x, y) for x, y in zip(a.G, b.G))
    assert np.allclose(back.broad.offsets, sc.broad.offsets) and np.allclose(back.cross, sc.cross)
    assert np.allclose(back.speakers[0].R, sc.speakers[0].R)


def test_difference_covariance_includes_the_correlation_with_its_own_vote():
    """L − E shares a member's own noise, so its variance is less than if they
    were independent, and a non-member's isn't. With k and the weights fixed,
    Monte Carlo agrees."""
    curve = bump(7000, 0.15, 5.0) + bump(3000, 1.0, 3.0)
    f = [_fit_of(curve) for _ in range(3)]
    srcs = [Source(n, G, x.L, x.cov) for n, x in zip("abc", f)]
    sc, member = _split_ref(srcs, own=0)
    _, other = _split_ref(srcs)
    cov = member.difference_cov(srcs[0].cov, G)
    assert np.all(np.diag(cov)[5:-5] < np.diag(other.difference_cov(srcs[0].cov, G))[5:-5])
    rng = np.random.default_rng(2)
    A = interp_matrix(sc.grid, G)
    k = member.share.k
    xs = []
    for _ in range(300):
        noisy = [replace(s, R=s.R + rng.multivariate_normal(np.zeros(G.size), s.cov)) for s in srcs]
        parts = [split_parts(s) for s in noisy]
        mB = soft_median([replace(p[1], R=p[1].R - o) for p, o in zip(parts, sc.broad.offsets)], sc.grid,
                         sc.broad.tau_hp, sc.broad.robust).m
        mN = soft_median([p[2] for p in parts], sc.grid, sc.narrow.tau_hp, sc.narrow.robust).m
        E = A @ (mB + k * mN)
        xs.append(noisy[0].R - E - (noisy[0].R - E).mean())
    ref = np.array(xs)
    C = cov - cov.mean(axis=0, keepdims=True) - cov.mean(axis=1, keepdims=True) + cov.mean()
    ratio = np.sqrt(np.clip(np.diag(C), 0, None))[5:-5] / np.std(ref, axis=0)[5:-5]
    assert 0.8 < np.median(ratio) < 1.2


# -- speakers in the consensus ---------------------------------------------------------

SPK_GRID = log_grid(step_oct=1 / 24)


def _speaker(R, sd=0.3, name="s"):
    return Source(name, SPK_GRID, np.asarray(R, float), np.eye(SPK_GRID.size) * sd**2, "speaker")


def test_a_speaker_without_a_shared_peak_takes_it_out_of_the_reference():
    hp = [src(bump(7000, 0.15, 6.0) + bump(10000, 0.15, 5.0), 0.1, n) for n in "abc"]
    with_both = _speaker(bump(7000, 0.15, 6.0, SPK_GRID) + bump(10000, 0.15, 5.0, SPK_GRID))
    only_7k = _speaker(bump(7000, 0.15, 6.0, SPK_GRID))
    _, alone = _split_ref(hp)
    _, both = _split_ref(hp, [with_both])
    _, gated = _split_ref(hp, [only_7k])
    assert _level(both, 10000) == pytest.approx(_level(alone, 10000), abs=1.0)
    assert _level(gated, 10000) < 0.5 * _level(alone, 10000)
    assert _level(gated, 7000) > 4.0
    assert gated.shares.speakers == ["s"] and gated.shares.colour_how == "the default"


def test_a_speakers_own_peak_never_reaches_the_reference():
    hp = [src(bump(7000, 0.15, 6.0), 0.1, n) for n in "abc"]
    own = _speaker(bump(7000, 0.15, 6.0, SPK_GRID) + bump(4500, 0.15, 6.0, SPK_GRID))
    _, alone = _split_ref(hp)
    _, e = _split_ref(hp, [own])
    assert abs(_level(e, 4500) - _level(alone, 4500)) < 0.3


def test_a_correction_follows_the_speaker():
    """The headphones' shared 10 kHz peak is corrected once a speaker lacks it."""
    curves = [bump(7000, 0.15, 6.0) + bump(10000, 0.15, 5.0)] * 3
    fits = [_fit_of(c, sd=0.2) for c in curves]
    hp = [Source(f"h{n}", f.grid, f.L, f.cov) for n, f in enumerate(fits)]
    st = CorrectionSettings(reference="consensus:h0,h1,h2,s", consistency=False)
    at = {}
    for name, spk in (("none", []), ("gate", [_speaker(bump(7000, 0.15, 6.0, SPK_GRID), 0.2)])):
        sc = split_consensus(hp, spk)
        ref = consensus_references({"L": sc}, st, "", 0, {"L": G})["L"]
        at[name] = _at(correct_ear("L", fits[0], st, consensus=ref), 7000, 10000)
    assert abs(at["none"][1]) < 1.5 and at["gate"][1] < -2.5
    assert abs(at["gate"][0]) < 1.5


def test_two_speakers_estimate_their_colouration():
    rng = np.random.default_rng(11)
    x = np.log2(SPK_GRID)
    kernel = np.exp(-0.5 * (np.arange(-8, 9) / 2.0) ** 2)

    def ripple(sd):
        r = np.convolve(rng.normal(0, 1, SPK_GRID.size), kernel, "same")
        return sd * (r - r.mean()) / r.std()

    hp = [src(bump(7000, 0.15, 6.0), 0.1, n) for n in "abc"]
    peak = bump(7000, 0.15, 6.0, SPK_GRID)
    splits = {e: split_consensus(hp, [_speaker(peak + ripple(2.0), 0.1, "s1"), _speaker(peak + ripple(2.0), 0.1, "s2")])
              for e in "LR"}
    est = estimate_colour(list(splits.values()), 1000, 13000)
    assert 0.8 < est < 2.5  # 2 dB of ripple, less what the split's broad part takes
    sh = shares(splits)
    assert sh.colour_db == pytest.approx(est) and sh.colour_how == "estimated from 2 speakers"
    assert GAIN_SD_RANGE[0] <= sh.gain_sd <= GAIN_SD_RANGE[1] and sh.gain_sd_how == "estimated from the speakers"
    set_ = shares(splits, colour_db=0.5, gain_sd=0.2)
    assert (set_.colour_db, set_.colour_how, set_.gain_sd, set_.gain_sd_how) == (0.5, "set", 0.2, "set")
    # One speaker can't estimate it.
    assert estimate_colour([split_consensus(hp, [_speaker(peak)])], 1000, 13000) is None


def test_two_speakers_count_for_more_than_one():
    hp = [src(bump(7000, 0.15, 6.0) + bump(10000, 0.15, 5.0), 0.1, n) for n in "abc"]
    lack = lambda n: _speaker(bump(7000, 0.15, 6.0, SPK_GRID), 0.3, n)
    one = shares({"L": split_consensus(hp, [lack("a")])}, colour_db=1.0, gain_sd=0.3).ears["L"]
    two = shares({"L": split_consensus(hp, [lack("a"), lack("b")])}, colour_db=1.0, gain_sd=0.3).ears["L"]
    i = np.argmin(np.abs(split_consensus(hp).grid - 10000))
    assert two.g_sd[i] < one.g_sd[i] and two.k[i] < one.k[i]


# -- the correction ------------------------------------------------------------------

def _fit_of(L, sd=0.3):
    return ProbitFit(G, L - L.mean(), np.eye(G.size) * sd**2, 1.0, 0.5, 0.02, 600, 0.0, True)


def _reference(fits, own=None, st=CorrectionSettings()):
    names = [f"h{n}" for n in range(len(fits))]
    sc = split_consensus([Source(n, f.grid, f.L, f.cov) for n, f in zip(names, fits)])
    return consensus_references({"L": sc}, st, "", own, None if own is None else {"L": fits[own].grid})["L"]


def _corrected(curves, i=0, consensus=None, veto=False, **settings):
    """consensus: None for a fixed flat reference, "all" or "others"."""
    fits = [_fit_of(L) for L in curves]
    st = dict(reference="none", consistency=False)
    ref = None
    if consensus:
        members = fits if consensus == "all" else [f for n, f in enumerate(fits) if n != i]
        st["reference"] = "consensus:" + ",".join(f"h{n}" for n in range(len(members)))
        ref = _reference(members, i if consensus == "all" else None, CorrectionSettings(**{**st, **settings}))
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
    st = CorrectionSettings(reference="consensus:o,a,b", consistency=False, veto=("a", "b"), veto_mode=mode)
    sc = split_consensus([Source(n, f.grid, f.L, f.cov) for n, f in zip("oab", (own, a, b))])
    ref = consensus_references({"L": sc}, st, "", 0, {"L": own.grid})["L"]
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
    assert all(abs(f.peak_db) < 1.0 for f in c.features if f.accepted)
    # A fixed flat reference sees that whole shape as a feature.
    assert [f for f in _corrected([shape], preserve_tonality=False).features if f.accepted and f.kind == "broad"]


def test_a_consensus_keeps_a_shared_peak_and_corrects_one_only_this_headphone_has():
    h = _at(_corrected([OWN + SHARED, SHARED, SHARED], consensus="all"), 5800, 7500)
    assert h[0] < -3.0 and abs(h[1]) < 1.5
    # Without its narrow part (a prior of 0), the shared peak goes too.
    assert _at(_corrected([OWN + SHARED, SHARED, SHARED], consensus="all", consensus_prior=0.0), 7500)[0] < -3.0


def test_a_profile_is_only_in_its_consensus_if_listed():
    shape = bump(5000, 0.2, 6.0)
    # With itself out, none of its peak is in the reference.
    out = _corrected([shape, np.zeros(G.size), np.zeros(G.size)], consensus="others")
    assert out.consensus.G is None and _at(out, 5000)[0] < -4.0
    assert _corrected([shape, np.zeros(G.size), np.zeros(G.size)], consensus="all").consensus.G is not None


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
    c = _corrected([np.zeros(G.size), bump(4000, 1.0, 5.0), bump(4000, 1.0, 5.0)], consensus="others",
                   method="match")
    assert c.target[np.argmin(np.abs(c.freqs - 4000))] > 2.0


@pytest.mark.parametrize("kw", [dict(veto=("a",), method="smooth"), dict(veto=("a",), method="match"),
                                dict(consensus_prior=-1.0), dict(consensus_coupling_db=7.0),
                                dict(speaker_window_oct=0.05), dict(speaker_colour_db=-1.0),
                                dict(consensus_gain_sd=0.0), dict(reference="consensus:"),
                                dict(veto_mode="nope"), dict(veto_import_db=-1.0)])
def test_settings_that_make_no_sense_are_refused(kw):
    with pytest.raises(ValueError):
        CorrectionSettings(**kw)


def test_smooth_method_takes_a_consensus_reference():
    c = _corrected([OWN + SHARED, SHARED, SHARED], consensus="all", method="smooth")
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
    assert main(["compare", "One"]) == 1
    assert main(["compare", "One", "Nope"]) == 1
    capsys.readouterr()
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    assert main(["compare", "One", "Two", "Three", "-H", "--ear", "L"]) == 0
    out = capsys.readouterr().out
    assert "3 profiles counted" in out and "narrow parts differ:" in out and "Right ear" not in out
    page = (tmp_path / "flatiron" / "cache" / "comparison.html").read_text()
    assert page.count("<picture>") == 1 and "Left ear" in page and "All features" not in page
    assert "aligned curves, narrow parts" in page and not opened  # -H: written, not opened
    assert main(["compare", "One", "Two", "--broad-spread", "-1"]) == 1
    assert main(["compare", "One", "Two", "--broad-spread", "2"]) == 0
    assert len(opened) == 1
    page = (tmp_path / "flatiron" / "cache" / "comparison.html").read_text()
    assert page.count("<picture>") == 2 and page.index("Left ear") < page.index("Right ear")

    csv = tmp_path / "out.csv"
    assert main(["report", "One", "--csv", str(csv)]) == 0
    lines = csv.read_text().splitlines()
    assert lines[0].startswith("ear,freq_hz,level_db,level_sd") and lines[1].startswith("L,") and lines[-1].startswith("R,")

    capsys.readouterr()
    assert main(["config", "One", "-r", "consensus:Two,Nope"]) == 1 and "settings" not in one.read()
    assert main(["config", "One", "-r", "consensus:Two,Three"]) == 0
    assert one.read()["settings"]["reference"] == "consensus:two,three"
    assert profile_settings(one).consensus == ("two", "three")
    assert main(["report", "One", "-H"]) == 0
    out = capsys.readouterr().out
    assert "consensus of Three, Two" in out and "Narrow structure kept: L " in out
    report = (one.dir / "cache" / "report.html").read_text()
    assert report.count("ear: the reference") == 2
    assert "weighted mean" in out
    assert current_fit(one).correction.ears["L"].consensus.G is None  # it isn't a member
    entries = list(consensus_dir(tmp_path / "flatiron" / "listeners" / "default").glob("*.npz"))
    assert len(entries) == 2  # one per ear

    # The same list from another profile shares the entries; another list doesn't.
    assert main(["config", "Three", "-r", "consensus:Three,Two"]) == 0
    assert current_fit(three).correction.ears["L"].consensus.G is not None  # listed, so it's a member
    assert len(list(consensus_dir(tmp_path / "flatiron" / "listeners" / "default").glob("*.npz"))) == 2
    assert main(["config", "Three", "-r", "consensus:Two"]) == 0
    assert "single headphone" in capsys.readouterr().out
    current_fit(three)
    assert len(list(consensus_dir(tmp_path / "flatiron" / "listeners" / "default").glob("*.npz"))) == 4

    # Neither the width nor how much of the narrow part is kept is part of the cached entry.
    k_before = current_fit(one).correction.ears["L"].consensus.share.k
    assert main(["config", "One", "-w", "0.5", "--coupling", "2", "--consensus-prior", "1.5"]) == 0
    k_after = current_fit(one).correction.ears["L"].consensus.share.k
    assert not np.allclose(k_before, k_after)
    assert len(list(consensus_dir(tmp_path / "flatiron" / "listeners" / "default").glob("*.npz"))) == 4
    assert main(["config", "One", "--speaker-colouration", "0.5"]) == 1  # no speaker listed
    assert main(["config", "One", "--reset", "width,coupling,consensus-prior"]) == 0

    # The combinations that used to be refused aren't any more.
    assert main(["config", "One", "--broad-strength", "100"]) == 0
    current_fit(one)
    assert main(["config", "One", "--broad-strength", "0", "--method", "smooth"]) == 0
    current_fit(one)
    assert main(["config", "One", "--method", "match"]) == 0
    current_fit(one)
    assert main(["config", "One", "--veto", "Two"]) == 1  # veto judges features
    capsys.readouterr()
    assert main(["config", "One", "--method", "features", "--veto", "Two,Three,One"]) == 0
    assert "Warning: the veto is on as well as a consensus reference" in capsys.readouterr().out
    st = profile_settings(one)
    assert st.veto == ("two", "three", "one") and st.consensus == ("two", "three")
    capsys.readouterr()
    assert main(["report", "One", "-H"]) == 0
    out = capsys.readouterr().out
    assert "Veto: each feature" in out and "adding at most 1.5 dB" in out and "Warning: the veto" in out
    assert main(["config", "One", "--veto-mode", "pointwise"]) == 0
    assert main(["config", "One", "--veto-import", "3"]) == 1  # only for the feature mode
    capsys.readouterr()
    assert main(["report", "One", "-H"]) == 0
    assert "frequency by frequency" in capsys.readouterr().out
    assert main(["config", "One", "--veto-mode", "feature", "--veto-import", "3"]) == 0
    assert profile_settings(one).veto_import_db == 3.0
    assert main(["config", "One", "--reset", "veto-import"]) == 0
    # The joint baseline, with a consensus reference and both vetoes (each voter's baseline is its own).
    assert main(["config", "One", "--baseline", "joint"]) == 0
    capsys.readouterr()
    assert main(["report", "One", "-H"]) == 0
    assert "Baseline: joint" in capsys.readouterr().out
    assert current_fit(one).correction.ears["L"].baseline_sd is not None
    assert main(["config", "One", "--veto-mode", "pointwise"]) == 0
    current_fit(one)
    assert main(["config", "One", "--method", "smooth"]) == 1  # the joint baseline needs features
    assert main(["config", "One", "--reset", "baseline,veto-mode"]) == 0
    assert main(["config", "One", "--veto", "One"]) == 1  # no one else to vote
    assert main(["config", "One", "--veto", "Nope"]) == 1
    assert main(["config", "One", "--veto", "none"]) == 0 and profile_settings(one).veto == ()

    assert main(["config", "Two", "--veto-mode", "pointwise"]) == 1  # needs --veto
    # The consensus options only mean something with a consensus.
    assert main(["config", "Two", "--coupling", "2"]) == 1

    # Pooling and a consensus or a veto don't mix.
    capsys.readouterr()
    assert main(["config", "Three", "--sessions", "Three,Two"]) == 0
    assert main(["config", "Three", "-r", "consensus:Two"]) == 1
    assert "share trials" in capsys.readouterr().out
    assert main(["config", "Three", "-r", "none", "--veto", "Two"]) == 1
    assert main(["config", "One", "-r", "consensus:Two,Three"]) == 1  # Three pools Two
    assert main(["config", "Three", "--reset", "sessions"]) == 0
    # A fixed reference ends it, and a comparison works with a consensus configured.
    assert main(["config", "One", "-r", "consensus:Two,Three"]) == 0
    capsys.readouterr()
    assert main(["compare", "One", "Two"]) == 0
    assert main(["config", "One", "-r", "none"]) == 0
    assert profile_settings(one).consensus == () and profile_settings(one).reference == "none"
    assert main(["config", "Three", "--reset", "reference"]) == 0
    assert profile_settings(Profile("three")).consensus == ()


def test_speakers_in_a_consensus_cli(three_profiles, tmp_path, capsys):
    from flatiron.cli import main
    from flatiron.fit.run import consensus_dir, current_fit, profile_settings
    from flatiron.session.store import Profile
    from flatiron.sim.listener import design_example
    from flatiron.sim.speaker import simulate_speaker_profile

    one = three_profiles[0]
    assert main(["profiles", "new", "Spk", "--kind", "speaker", "--channel", "R"]) == 0
    simulate_speaker_profile(Profile("spk"), {e: design_example(e) for e in "LR"}, blocks=10, block=16, seed=1)
    capsys.readouterr()
    assert main(["config", "One", "-r", "consensus:Two,Three,Spk"]) == 0
    assert "with Spk deciding how much" in capsys.readouterr().out
    assert main(["config", "One", "--speaker-colouration", "0.5", "--gain-sd", "0.4", "--speaker-window", "0.25"]) == 0
    st = profile_settings(one)
    assert (st.speaker_colour_db, st.consensus_gain_sd, st.speaker_window_oct) == (0.5, 0.4, 0.25)
    sh = current_fit(one).correction.ears["L"].consensus.shares
    assert sh.speakers == ["Spk"] and (sh.colour_db, sh.colour_how, sh.gain_sd) == (0.5, "set", 0.4)
    assert main(["config", "One", "--speaker-colouration", "auto", "--gain-sd", "auto"]) == 0
    assert profile_settings(one).speaker_colour_db is None
    capsys.readouterr()
    assert main(["report", "One", "-H"]) == 0
    out = capsys.readouterr().out
    assert "Speakers (Spk)" in out and "1.00 dB (the default)" in out and "With one speaker" in out
    assert len(list(consensus_dir(tmp_path / "flatiron" / "listeners" / "default").glob("*.npz"))) == 2
    # Speakers only gate: a consensus of speakers alone is refused, and an IEM can't be listed.
    assert main(["config", "Two", "-r", "consensus:Spk"]) == 1
    assert main(["profiles", "new", "Bud", "--kind", "iem"]) == 0
    assert main(["config", "Two", "-r", "consensus:One,Bud"]) == 1


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
    with pytest.raises(ValueError, match="-r diffuse"):
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


def test_soft_veto_corrects_nothing_as_a_voter_reaches_this_features_shape():
    """The residual isn't exactly its Gaussian, which mustn't read as a difference."""
    x = np.log2(G)
    k = np.argmin(np.abs(G - 7000))
    own = Candidate("own", 7000, 0.2, 6.0)
    R = 6.0 * np.exp(-0.5 * ((x - np.log2(7000)) / (0.2 / 2.355)) ** 2) + 0.5 * np.exp(-0.5 * ((x - np.log2(7300)) / 0.03) ** 2)
    size = []
    for h in (6.0, 5.8, 5.0, 3.0):
        r = judge_soft(own, {"a": [Candidate("a", 7000, 0.2, h)], "b": [Candidate("b", 7000, 0.2, h)]}, 0.1)
        size.append(np.abs(soft_correction(R, x, slice(0, G.size), r, own, 1.0, 1.5)).max())
    assert size[0] == pytest.approx(0, abs=1e-6)
    assert all(a < b for a, b in zip(size, size[1:]))
