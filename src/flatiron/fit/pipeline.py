"""From a perceptual fit to correction filters, per ear (DESIGN.md, "Pipeline").

Every setting here is downstream of the cached perceptual fit, so it can be
changed without new trials.
"""

from dataclasses import asdict, dataclass, field, replace

import numpy as np

from flatiron.audio.biquad import PeakingFilter
from flatiron.fit.baseline import fit_baseline
from flatiron.fit.features import BROAD, NARROW, Feature, check_consistency, find_features
from flatiron.fit.filters import FilterBounds, FilterFit, fit_filters, preamp_db, response
from flatiron.fit.reference import reference
from flatiron.model.grid import interp_matrix, log_grid
from flatiron.model.probit import ProbitFit

FINE_STEP_OCT = 1 / 96
METHODS = ("features", "smooth")
# Broad lobes that aren't corrected as a whole are searched for narrow
# features against a local smooth over this width (FWHM, octaves).
INNER_SMOOTH_OCT = 0.5


@dataclass(frozen=True)
class CorrectionSettings:
    max_width_oct: float = 1.0  # W: anything broader is left to the user's EQ
    reference: str = "iso226"  # iso226 | diffuse | none | path to a CSV
    phon: float = 60.0
    baseline_penalty: float = 0.1  # baseline stiffness (tuned in the simulator; 1e-3 absorbed clustered features)
    k: float = 2.0
    k_broad: float = 2.5
    narrow_max_oct: float = 1 / 2  # 1/3 dropped a 0.4-octave, 7 dB resonance as broad
    broad_strength: float = 1.0  # 0..1, scales broad features only
    max_cut_db: float = 12.0
    max_boost_narrow_db: float = 4.0
    max_boost_broad_db: float = 6.0
    f_lo: float = 1000.0
    f_hi: float = 13000.0  # trials near the top of the range are hard to judge; don't go below 12 kHz
    taper_oct: float = 1 / 3  # fade beyond [f_lo, f_hi], limited by the measured margin
    bands: int = 20  # per channel; LSP x32 holds 32
    q_min: float = 0.7
    q_max: float = 16.0
    max_gain_db: float = 12.0
    min_gain_db: float = 0.5
    stop_z: float = 2.0  # filter fit stops when no point is off by more than this many SDs
    consistency: bool = True
    consistency_p: float = 0.01
    # Keep the broad shape of the original response (the default): correct
    # narrow features only, then add broad compensation so the correction's own
    # average over about max_width_oct (W) is zero. Broad tonal changes are left
    # to the user. False: correct to the fixed target, broad features included
    # (scaled by broad_strength).
    preserve_tonality: bool = True
    # "features" (the default): detect significant features against the
    # baseline. "smooth" (experimental): the baseline is the curve smoothed
    # over smooth_oct (FWHM), and every residual is corrected, with no
    # feature detection; it corrects more, noise included.
    method: str = "features"
    smooth_oct: float = 0.5

    def __post_init__(self):
        if not 1 / 3 - 1e-9 <= self.max_width_oct <= 2 + 1e-9:
            raise ValueError("max feature width must be 1/3 to 2 octaves")
        if not 0 <= self.broad_strength <= 1:
            raise ValueError("broad strength must be 0 to 1")
        if not 0 < self.bands <= 32:
            raise ValueError("band budget must be 1 to 32")
        if not self.f_lo < self.f_hi:
            raise ValueError("f_lo must be below f_hi")
        if self.method not in METHODS:
            raise ValueError(f"method must be one of {', '.join(METHODS)}")
        if not 1 / 8 - 1e-9 <= self.smooth_oct <= 1 + 1e-9:
            raise ValueError("smoothing width must be 1/8 to 1 octave")
        if self.method == "smooth" and not self.preserve_tonality:
            raise ValueError("the smooth method corrects narrow residuals only; it doesn't combine with --broad")


@dataclass
class EarCorrection:
    ear: str
    grid: np.ndarray
    L: np.ndarray
    L_sd: np.ndarray
    E: np.ndarray  # reference shape on the grid
    baseline: np.ndarray  # of D = L − E
    R: np.ndarray
    R_sd: np.ndarray
    features: list[Feature]
    target_grid: np.ndarray  # C on the grid, before tapering
    freqs: np.ndarray  # fine evaluation grid
    target: np.ndarray  # C on the fine grid, tapered
    sd: np.ndarray  # SD of the target on the fine grid
    fit: FilterFit
    left_out: list[tuple[float, float, float]] = field(default_factory=list)  # (f_lo, f_hi, max |err| dB)
    sessions_checked: int = 0
    session_R: list[np.ndarray] = field(default_factory=list)  # each session's residual, same baseline
    session_trials: list[int] = field(default_factory=list)  # trials behind each of session_R
    compensation: list[PeakingFilter] = field(default_factory=list)  # tonality-preserving filters (in fit.filters)
    fixed: list[PeakingFilter] = field(default_factory=list)  # filters kept from before (incremental refit)

    @property
    def filters(self) -> list[PeakingFilter]:
        return self.fit.filters

    @property
    def added(self) -> list[PeakingFilter]:
        """Filters this fit adds (all of them, unless it's incremental)."""
        keep = {id(f) for f in self.fixed}
        return [f for f in self.fit.filters if id(f) not in keep]


def taper(freqs, f_lo, f_hi, width_lo_oct, width_hi_oct) -> np.ndarray:
    """1 inside [f_lo, f_hi], raised-cosine to 0 over the given widths outside
    it (DESIGN.md step 7), 0 beyond. A zero width is a hard edge."""
    x = np.log2(freqs)
    a, b = np.log2(f_lo), np.log2(f_hi)
    up = np.clip((x - a) / width_lo_oct + 1, 0, 1) if width_lo_oct > 0 else (x >= a).astype(float)
    down = np.clip((b - x) / width_hi_oct + 1, 0, 1) if width_hi_oct > 0 else (x <= b).astype(float)
    return 0.5 * (1 - np.cos(np.pi * np.minimum(up, down)))


def build_target(features, R, settings: CorrectionSettings) -> np.ndarray:
    c = np.zeros_like(R)
    for f in features:
        if not f.accepted:
            continue
        seg = -(R[f.span] if f.residual is None else f.residual)
        if f.kind == BROAD:
            if settings.preserve_tonality:
                continue
            seg = np.minimum(seg, settings.max_boost_broad_db) * settings.broad_strength
        else:
            seg = np.minimum(seg, settings.max_boost_narrow_db)
        c[f.span] += np.maximum(seg, -settings.max_cut_db)
    return c


def broad_regions(feats: list[Feature], R, R_sd, settings: CorrectionSettings) -> list[list[Feature]]:
    """Broad lobes that aren't corrected as a whole (kept as tonality, or not
    significant), each with the same-sign neighbours it was split from at a
    valley that stays clear of zero. A narrow dip inside a broad excess splits
    it like that, and the flank beside the dip would otherwise pass for a
    narrow feature; a valley that comes back to zero (within k SD) separates
    real neighbours, which keep their own test."""
    out = []
    for run in sorted({f.run for f in feats}):
        parts = sorted((f for f in feats if f.run == run), key=lambda f: f.lo)
        groups = [[parts[0]]]
        for a, b in zip(parts, parts[1:]):
            if abs(R[a.hi]) <= settings.k * R_sd[a.hi]:
                groups.append([b])
            else:
                groups[-1].append(b)
        out += [grp for grp in groups
                if any(f.kind == BROAD and (settings.preserve_tonality or not f.accepted) for f in grp)]
    return out


def inner_features(g, R, cov_R, lo: int, hi: int, settings: CorrectionSettings, per) -> list[Feature]:
    """Narrow features inside a broad region [lo, hi] (grid indices) that
    isn't corrected as a whole (it's kept as tonality, or not significant):
    the residual against a local smooth over the region alone, with the
    narrow test on every part. per: each session's (R_s, cov_s), for the
    consistency check."""
    sl = slice(lo, hi + 1)
    gi = g[sl]
    if gi.size < 3:
        return []
    w = np.gradient(np.log2(g))[sl] / np.clip(np.diag(cov_R)[sl], 1e-12, None)
    M = np.eye(gi.size) - local_linear_smoother(gi, INNER_SMOOTH_OCT, w)
    r = M @ R[sl]
    cov = M @ cov_R[sl, sl] @ M.T
    found = [f for f in find_features(gi, r, cov, settings.k, settings.k_broad, np.inf) if f.accepted]
    check_consistency(found, gi, r, [(M @ rs[sl], M @ c[sl, sl] @ M.T) for rs, c in per],
                      settings.consistency_p)
    for f in found:
        f.kind, f.inside_broad, f.residual = NARROW, True, r[f.lo:f.hi + 1].copy()
        f.residual_sd = np.sqrt(np.clip(np.diag(cov)[f.lo:f.hi + 1], 1e-12, None))
        f.shifted(sl.start)
    return found


def correct_ear(ear: str, fit: ProbitFit, settings: CorrectionSettings = CorrectionSettings(),
                session_fits: list[ProbitFit] | None = None,
                fixed: list[PeakingFilter] | None = None) -> EarCorrection:
    """fixed: filters already in use (an incremental refit). The pipeline then
    runs on the corrected curve, L + fixed, and only adds filters for what's
    still significant, within the bands left over."""
    g = fit.grid
    fixed = list(fixed or [])
    E = reference(settings.reference, g, settings.phon)
    H_fixed = response(fixed, g)
    D = fit.L + H_fixed - E
    smooth = settings.method == "smooth"
    if smooth:
        S = local_linear_smoother(g, settings.smooth_oct, np.gradient(np.log2(g)) / fit.sd**2)
    else:
        S = fit_baseline(g, D, fit.sd, settings.max_width_oct, settings.baseline_penalty).smoother
    M = np.eye(g.size) - S
    R = M @ D
    cov_R = M @ fit.cov @ M.T
    R_sd = np.sqrt(np.clip(np.diag(cov_R), 1e-12, None))

    # With the smooth method the features are only a report of what's
    # significant (verify's leftovers); the target is the whole residual.
    feats = find_features(g, R, cov_R, settings.k, settings.k_broad, np.inf if smooth else settings.narrow_max_oct)
    per = [(M @ (s.L + H_fixed - E), M @ s.cov @ M.T) for s in session_fits or []]
    session_R = [r for r, _ in per]
    checked = len(per) if settings.consistency else 0
    if settings.consistency:
        check_consistency(feats, g, R, per, settings.consistency_p)
    if not smooth:
        for region in broad_regions(feats, R, R_sd, settings):
            for f in region:
                if f.kind == NARROW and f.accepted:
                    f.accepted = False
                    f.reason = "part of a broad region (searched inside it instead)"
            feats += inner_features(g, R, cov_R, region[0].lo, region[-1].hi, settings,
                                    per if settings.consistency else [])

    for f in feats:
        if f.accepted and not settings.f_lo <= f.fc <= settings.f_hi:
            f.accepted = False
            f.reason = "outside the correction range"
    if smooth:
        C = np.clip(-R, -settings.max_cut_db, settings.max_boost_narrow_db)
    else:
        C = build_target(feats, R, settings)
    freqs = log_grid(g[0], g[-1], FINE_STEP_OCT)
    m = interp_matrix(g, freqs)
    # Fade out beyond the range, within the measured margin (850 Hz-15 kHz by default).
    w_lo = min(settings.taper_oct, max(np.log2(settings.f_lo / g[0]), 0.0))
    w_hi = min(settings.taper_oct, max(np.log2(g[-1] / settings.f_hi), 0.0))
    target = (m @ C) * taper(freqs, settings.f_lo, settings.f_hi, w_lo, w_hi)
    sd = np.sqrt(np.einsum("ij,jk,ik->i", m, cov_R, m))
    inner = [f for f in feats if f.accepted and f.residual_sd is not None]
    if inner:
        # The target there is the residual against the local smooth, whose
        # error is smaller than R's (the smooth takes the slow part of it).
        sd_g = R_sd.copy()
        for f in inner:
            sd_g[f.span] = f.residual_sd
        sd = np.minimum(sd, m @ sd_g)
    bounds = FilterBounds(settings.f_lo, settings.f_hi, settings.q_min, settings.q_max, settings.max_gain_db)
    ff = fit_filters(freqs, target, sd, max(settings.bands - len(fixed), 0), bounds, settings.stop_z,
                     settings.min_gain_db)
    comp: list[PeakingFilter] = []
    if settings.preserve_tonality:
        for f in feats:
            if f.accepted and f.kind == BROAD:
                f.accepted = False
                f.reason = "broad: kept as tonality (correct it with --broad)"
        ff, comp = add_tonality_compensation(ff, freqs, settings, w_lo, w_hi, len(fixed))
    if fixed:
        contrib = dict(zip(map(id, ff.filters), ff.contributions))
        allf = sorted(fixed + ff.filters, key=lambda f: f.fc)
        ff = replace(ff, filters=allf, response=ff.response + response(fixed, freqs),
                     contributions=[contrib.get(id(f), 0.0) for f in allf])

    left = []
    if ff.budget_bound:
        err = target - ff.response
        over = np.abs(err) > settings.k * sd
        i = 0
        while i < freqs.size:
            if over[i]:
                j = i
                while j + 1 < freqs.size and over[j + 1]:
                    j += 1
                left.append((float(freqs[i]), float(freqs[j]), float(np.abs(err[i:j + 1]).max())))
                i = j + 1
            else:
                i += 1
    return EarCorrection(ear, g, fit.L, fit.sd, E, S @ D, R, R_sd, feats, C, freqs, target, sd, ff,
                         left, checked, session_R, [s.n_trials for s in session_fits or []], comp, fixed)


def smoother_matrix(freqs, fwhm_oct, weights) -> np.ndarray:
    """Gaussian smoothing in log2 f as a matrix, normalised by `weights` (so
    the edges average only over what's there)."""
    x = np.log2(freqs)
    k = np.exp(-0.5 * ((x[:, None] - x[None, :]) / (fwhm_oct / 2.355)) ** 2) * weights[None, :]
    return k / np.maximum(k.sum(axis=1, keepdims=True), 1e-12)


def smooth_log(freqs, values, fwhm_oct, weights) -> np.ndarray:
    return smoother_matrix(freqs, fwhm_oct, weights) @ values


def local_linear_smoother(freqs, fwhm_oct, weights) -> np.ndarray:
    """Gaussian-weighted local linear regression in log2 f, as a matrix. Unlike
    a plain weighted average it has no bias at the ends of the range (a
    one-sided average lags a slope there, which reads as a feature)."""
    x = np.log2(freqs)
    k = np.exp(-0.5 * ((x[:, None] - x[None, :]) / (fwhm_oct / 2.355)) ** 2) * weights[None, :]
    d = x[None, :] - x[:, None]  # (i, j): x_j − x_i
    s0, s1, s2 = k.sum(1), (k * d).sum(1), (k * d * d).sum(1)
    # Row i: weights of the local line's value at x_i.
    return k * (s2[:, None] - s1[:, None] * d) / np.maximum(s0 * s2 - s1**2, 1e-12)[:, None]


COMP_Q_MAX = 1.5  # compensation filters stay broad
COMP_TOL_DB = 0.5  # the broad shape is kept to within this
COMP_MAX_BANDS = 4


def _merge_close(filters: list[PeakingFilter], oct_tol: float = 1 / 24) -> list[PeakingFilter]:
    """Fold filters at (nearly) the same frequency and Q into one (gains add)."""
    out: list[PeakingFilter] = []
    for f in sorted(filters, key=lambda f: f.fc):
        if out and abs(np.log2(f.fc / out[-1].fc)) < oct_tol and abs(np.log2(f.q / out[-1].q)) < 0.1:
            p = out.pop()
            f = PeakingFilter(p.fc, p.gain_db + f.gain_db, p.q)
        out.append(f)
    return out


def add_tonality_compensation(ff: FilterFit, freqs, settings: CorrectionSettings, w_lo, w_hi, used: int = 0):
    """Broad filters that cancel the narrow filters' average over ~W, so the
    corrected response keeps the original's broad shape. Fitted to what the
    narrow filters actually do (a noise-free curve), with the bands left over."""
    w = taper(freqs, settings.f_lo, settings.f_hi, w_lo, w_hi)
    h = response(ff.filters, freqs)
    goal = -smooth_log(freqs, h, settings.max_width_oct, w) * w
    room = min(settings.bands - used - len(ff.filters), COMP_MAX_BANDS)
    if room <= 0 or np.max(np.abs(goal)) < COMP_TOL_DB:
        return ff, []
    # Gains no bigger than the curve itself: otherwise the fit chases the
    # range-edge fade with large opposing pairs (seen: −12 dB against +11 dB).
    limit = min(settings.max_gain_db, 1.5 * float(np.max(np.abs(goal))) + 0.5)
    bounds = FilterBounds(settings.f_lo, settings.f_hi, settings.q_min, COMP_Q_MAX, limit)
    cf = fit_filters(freqs, goal, np.full(freqs.size, COMP_TOL_DB / 2), room, bounds, 2.0, 0.2)
    comp = _merge_close(cf.filters)
    contrib = dict(zip(map(id, ff.filters), ff.contributions))
    filters = sorted(ff.filters + comp, key=lambda f: f.fc)
    return replace(ff, filters=filters, response=ff.response + cf.response,
                   contributions=[contrib.get(id(f), 0.0) for f in filters]), comp


@dataclass
class Correction:
    settings: CorrectionSettings
    ears: dict[str, EarCorrection]
    preamp_db: float  # common to both channels, so the L/R balance is kept

    def summary(self) -> dict:
        """JSON-able summary for profile.json."""
        out = {"settings": asdict(self.settings), "preamp_db": self.preamp_db, "ears": {}}
        for e, c in self.ears.items():
            out["ears"][e] = {
                "filters": [asdict(f) for f in c.filters],
                "compensation": [asdict(f) for f in c.compensation],
                "chi2": c.fit.chi2,
                "budget_bound": c.fit.budget_bound,
                "left_out": c.left_out,
                "curve": {"freq": c.grid.tolist(), "L": c.L.tolist(), "sd": c.L_sd.tolist()},
                "features": [
                    {"fc": f.fc, "kind": f.kind, "peak_db": f.peak_db, "width_oct": f.width_oct,
                     "z": f.z, "accepted": f.accepted, "reason": f.reason,
                     "consistency_p": f.consistency_p, "inside_broad": f.inside_broad}
                    for f in c.features],
            }
        return out


def correct(fits: dict[str, ProbitFit], settings: CorrectionSettings = CorrectionSettings(),
            session_fits: dict[str, list[ProbitFit]] | None = None,
            fixed: dict[str, list[PeakingFilter]] | None = None) -> Correction:
    ears = {e: correct_ear(e, fits[e], settings, (session_fits or {}).get(e), (fixed or {}).get(e))
            for e in fits}
    pre = min(preamp_db(c.filters) for c in ears.values())
    return Correction(settings, ears, pre)
