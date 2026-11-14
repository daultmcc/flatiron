"""From a perceptual fit to correction filters, per ear (DESIGN.md, "Pipeline").

Every setting here is downstream of the cached perceptual fit, so it can be
changed without new trials.
"""

import hashlib
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace

import numpy as np

from flatiron.audio.biquad import PeakingFilter
from flatiron.fit.baseline import fit_baseline, fit_shared_baseline, split
from flatiron.fit.decompose import fit_joint_baseline, fit_shared_joint_baseline
from flatiron.fit.consensus import EarConsensus, SoftMedian, Source, consensus_of
from flatiron.fit.features import BROAD, NARROW, Feature, GroupCurve, find_features, seating_step
from flatiron.fit.filters import FilterBounds, FilterFit, fit_filters, preamp_db, response
from flatiron.fit.features import weighted_median
from flatiron.fit.reference import CONSENSUS_PREFIX, reference
from flatiron.fit.veto import (LOBE_HI_DB, LOBE_LO_DB, MIN_LOBE_DB, OWN_LABEL, OWN_WINS, Candidate, correction,
                               correction_rule, judge, judge_soft, smoothstep, soft_correction)
from flatiron.model.grid import interp_matrix, log_grid
from flatiron.model.probit import ProbitFit

FINE_STEP_OCT = 1 / 96
MAX_BANDS = 128  # per channel; each exporter refuses more than it can hold
METHODS = ("features", "smooth", "match")
VETO_MODES = ("feature", "pointwise")
BASELINES = ("robust", "joint")
# Broad lobes that aren't corrected as a whole are searched for narrow
# features against a local smooth over this width (FWHM, octaves).
INNER_SMOOTH_OCT = 0.5


@dataclass(frozen=True)
class CorrectionSettings:
    max_width_oct: float = 1.0  # W: anything broader is left to the user's EQ
    reference: str = "diffuse"  # diffuse | iso226 | none | path to a CSV | consensus:SLUG,SLUG
    phon: float = 60.0
    baseline_penalty: float = 0.1  # baseline stiffness (tuned in the simulator; 1e-3 absorbed clustered features)
    # robust: the Tukey-weighted spline (pipeline step 2). joint: the posterior
    # mean of the tonality under an explicit model of D (fit/decompose.py),
    # which can't tip between two readings the way the robust fit can.
    baseline: str = "robust"
    # One baseline for both ears (DESIGN.md, "Shared baseline"): their broad
    # shape is taken to be the same, each with its own level. False: each ear
    # its own, which also takes up broad differences between the ears. Only
    # the features method has such a baseline.
    shared_baseline: bool = True
    k: float = 2.0
    k_broad: float = 2.5
    # Soft rejection: features within this many SDs of k or k_broad are partly
    # corrected instead of kept or dropped. 0: the plain test.
    soft_width: float = 0.5
    narrow_max_oct: float = 1 / 2  # 1/3 dropped a 0.4-octave, 7 dB resonance as broad
    broad_strength: float = 1.0  # 0..1, scales broad features only
    max_cut_db: float = 12.0
    max_boost_narrow_db: float = 4.0
    max_boost_broad_db: float = 6.0
    f_lo: float = 1000.0
    f_hi: float = 13000.0  # trials near the top of the range are hard to judge; don't go below 12 kHz
    taper_oct: float = 1 / 3  # fade beyond [f_lo, f_hi], limited by the measured margin
    bands: int = 24  # per channel; LSP x32 and EasyEffects hold 32, TinCan 128 (256 in all)
    q_min: float = 0.7
    q_max: float = 16.0
    max_gain_db: float = 12.0
    min_gain_db: float = 0.5
    fit_tol_db: float = 0.5  # the filter fit stops when it's this close to the target everywhere it can reach
    consistency: bool = True  # the seating step: correct each feature to the median seating's level
    # Keep the broad shape of the original response (the default): correct
    # narrow features only, then add broad compensation so the correction's own
    # average over about max_width_oct (W) is zero. Broad tonal changes are left
    # to the user. False: correct to the fixed target, broad features included
    # (scaled by broad_strength).
    preserve_tonality: bool = True
    # "features" (the default): detect significant features against the
    # baseline. "smooth" (experimental): the baseline is the curve smoothed
    # over smooth_oct (FWHM), and every residual is corrected, with no
    # feature detection; it corrects more, noise included. "match"
    # (experimental): no baseline at all; the whole curve is corrected toward
    # the reference, tonality included.
    method: str = "features"
    smooth_oct: float = 0.5
    # A consensus reference (DESIGN.md, "Consensus reference") is smoothed over this
    # (FWHM, octaves; 0: not at all). Unsmoothed it holds the members' narrow
    # features too, and a peak in the reference reads as a dip here.
    consensus_smooth_oct: float = 0.5
    # Other profiles whose features decide how each of this one's is corrected (fit/veto.py).
    veto: tuple[str, ...] = ()
    # feature: toward the median feature; pointwise: frequency by frequency, by
    # what exceeds the median of the residuals, and never past zero.
    veto_mode: str = "feature"
    # When the median feature is somewhere else, how far the correction may go
    # past removing this feature, in dB (0: it can't add anything).
    veto_import_db: float = 1.5
    # Above 0, the feature veto weighs its candidates instead of picking one, with
    # no sharp edges anywhere in it (fit/veto.py). How far the weights spread: a
    # candidate this share further from the others than the most central one
    # weighs 1/e of it.
    veto_softness: float = 0.1
    # The channel-balance prior's SDs (DESIGN.md, "Channel balance") as a
    # multiple of the defaults; 0: each ear fitted on its own. Unlike the rest,
    # this changes the perceptual fit (cached separately, and quick).
    balance: float = 1.0

    def __post_init__(self):
        object.__setattr__(self, "veto", tuple(self.veto))
        if self.reference.startswith(CONSENSUS_PREFIX) and not self.consensus:
            raise ValueError("a consensus reference needs at least one profile")
        if not 0 <= self.soft_width <= 2:
            raise ValueError("the soft width must be 0 to 2 SDs")
        if not 0 <= self.consensus_smooth_oct <= 2:
            raise ValueError("consensus smoothing must be 0 to 2 octaves")
        if self.veto and self.method != "features":
            raise ValueError("--veto judges features, so it needs --method features")
        if self.veto_mode not in VETO_MODES:
            raise ValueError(f"veto mode must be one of {', '.join(VETO_MODES)}")
        if self.veto_import_db < 0:
            raise ValueError("the veto's import limit can't be negative")
        if not 0 <= self.veto_softness <= 1:
            raise ValueError("the veto's softness must be 0 to 1")
        if not 0 <= self.balance <= 10:
            raise ValueError("the balance prior's scale must be 0 to 10 (0: off)")
        if not 1 / 3 - 1e-9 <= self.max_width_oct <= 2 + 1e-9:
            raise ValueError("max feature width must be 1/3 to 2 octaves")
        if not 0 <= self.broad_strength <= 1:
            raise ValueError("broad strength must be 0 to 1")
        if not 0 < self.bands <= MAX_BANDS:
            raise ValueError(f"band budget must be 1 to {MAX_BANDS}")
        if not 0.1 <= self.fit_tol_db <= 6:
            raise ValueError("the filter fit's tolerance must be 0.1 to 6 dB")
        if not self.f_lo < self.f_hi:
            raise ValueError("f_lo must be below f_hi")
        if self.baseline not in BASELINES:
            raise ValueError(f"baseline must be one of {', '.join(BASELINES)}")
        if self.baseline != "robust" and self.method != "features":
            raise ValueError("--baseline joint needs --method features (the other methods have no such baseline)")
        if self.method not in METHODS:
            raise ValueError(f"method must be one of {', '.join(METHODS)}")
        if not 1 / 8 - 1e-9 <= self.smooth_oct <= 1 + 1e-9:
            raise ValueError("smoothing width must be 1/8 to 1 octave")
        if self.method == "smooth" and not self.preserve_tonality:
            raise ValueError("the smooth method corrects narrow residuals only; it doesn't combine with --broad-strength above 0")

    @property
    def consensus(self) -> tuple[str, ...]:
        """The profiles a consensus reference is built from (empty for a fixed reference)."""
        if not self.reference.startswith(CONSENSUS_PREFIX):
            return ()
        return tuple(x for x in self.reference[len(CONSENSUS_PREFIX):].split(",") if x)


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
    sessions_checked: int = 0  # groups in the seating step
    session_R: list[np.ndarray] = field(default_factory=list)  # each group's residual, same baseline
    session_trials: list[int] = field(default_factory=list)  # trials behind each of session_R
    compensation: list[PeakingFilter] = field(default_factory=list)  # tonality-preserving filters (in fit.filters)
    fixed: list[PeakingFilter] = field(default_factory=list)  # filters kept from before (incremental refit)
    consensus: EarConsensus | None = None  # the reference, if it's a consensus
    baseline_sd: np.ndarray | None = None  # the joint baseline's posterior SD (None for the others)

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


def build_target(features, R, settings: CorrectionSettings, grid=None, median_R=None) -> np.ndarray:
    """grid: for the feature veto's corrections. median_R: the pointwise rule's
    median, on the same grid."""
    c = np.zeros_like(R)
    x = None if grid is None else np.log2(grid)
    for f in features:
        if not f.accepted or (f.kind == BROAD and settings.preserve_tonality):
            continue
        boost = settings.max_boost_broad_db if f.kind == BROAD else settings.max_boost_narrow_db
        strength = settings.broad_strength if f.kind == BROAD else 1.0
        if f.veto is not None:
            seg = R[f.span] if f.residual is None else f.residual
            soft = bool(f.veto.weighed)
            if soft:
                full = soft_correction(seg, x, f.span, f.veto, f.veto.candidates[0], f.scale * f.weight,
                                       settings.veto_import_db)
            else:
                full = correction(seg, x, f.span, f.veto, np.sign(f.peak_db), f.scale * f.weight,
                                  settings.veto_import_db)
            full = np.clip(full, -settings.max_cut_db, boost) * strength
            k = int(np.argmax(np.abs(full)))
            f.veto.correction_db = float(full[k])
            if abs(full[k]) < (1e-3 if soft else settings.min_gain_db):
                f.accepted, f.reason = False, "shared: nothing of it to remove"
                continue
            c += full
            continue
        if median_R is not None:
            own = R[f.span] if f.residual_R is None else f.residual_R
            # Inside a broad region, both against the same local smooth.
            m = median_R[f.span] if f.residual is None else own - f.residual
            rule = correction_rule(own, m)
            seg = rule * f.scale * f.weight
            k = int(np.argmax(np.abs(seg)))
            f.correction_db, f.shared_db = float(seg[k]), float(np.max(np.abs(own)) - abs(rule[k]))
            if abs(seg[k]) < settings.min_gain_db:
                f.accepted, f.reason = False, "shared: nothing of it to remove"
                continue
        else:
            seg = -(R[f.span] if f.residual is None else f.residual) * f.scale * f.weight
        c[f.span] += np.maximum(np.minimum(seg, boost) * strength, -settings.max_cut_db)
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


def inner_features(g, R, cov_R, lo: int, hi: int, settings: CorrectionSettings,
                   groups: list[GroupCurve], own_R=None) -> list[Feature]:
    """Narrow features inside a broad region [lo, hi] (grid indices) that
    isn't corrected as a whole (it's kept as tonality, or not significant):
    the residual against a local smooth over the region alone, with the
    narrow test on every part. groups: each group's curve, for the seating step.
    own_R: with the pointwise veto R is the excess, and this the profile's own residual."""
    sl = slice(lo, hi + 1)
    gi = g[sl]
    if gi.size < 3:
        return []
    w = np.gradient(np.log2(g))[sl] / np.clip(np.diag(cov_R)[sl], 1e-12, None)
    M = np.eye(gi.size) - local_linear_smoother(gi, INNER_SMOOTH_OCT, w)
    r = M @ R[sl]
    cov = M @ cov_R[sl, sl] @ M.T
    found = [f for f in find_features(gi, r, cov, settings.k, settings.k_broad, np.inf, settings.soft_width) if f.accepted]
    seating_step(found, gi, r, cov, [GroupCurve(M @ c.r[sl], M @ c.cov[sl, sl] @ M.T, M @ c.u_cov[sl, sl] @ M.T)
                                     for c in groups], settings.consistency)
    for f in found:
        f.kind, f.inside_broad, f.residual = NARROW, True, r[f.lo:f.hi + 1].copy()
        if own_R is not None:
            f.residual_R = (M @ own_R[sl])[f.lo:f.hi + 1]
        f.residual_sd = np.sqrt(np.clip(np.diag(cov)[f.lo:f.hi + 1], 1e-12, None))
        f.shifted(sl.start)
    return found


@dataclass
class _Base:
    values: np.ndarray  # the baseline of D
    J: np.ndarray  # its Jacobian in D: the residual is (I − J) D to first order (exactly, if it's linear)
    sd: np.ndarray | None = None  # posterior SD (joint only)
    # Shared with the other ear: its part of the residual's covariance, J_o Σ_o J_oᵀ
    # (the ears' errors taken as independent), and of each session's curve, by
    # session id: (shift of the residual, its covariance, its deviation's covariance).
    other_cov: np.ndarray | None = None
    other_groups: dict = field(default_factory=dict)

    @property
    def M(self) -> np.ndarray:
        return np.eye(self.J.shape[0]) - self.J

    def cov(self, cov_D: np.ndarray) -> np.ndarray:
        """The residual's covariance."""
        own = self.M @ cov_D @ self.M.T
        return own if self.other_cov is None else own + self.other_cov


def _baseline(g, D, sd_D, settings: CorrectionSettings) -> _Base:
    """D's baseline. The robust one and the smooth method's are linear smoothers
    S (J = S), fixed at their final weights; the joint one is nonlinear in D."""
    if settings.method == "match":
        return _Base(np.zeros(g.size), np.zeros((g.size, g.size)))
    if settings.method == "smooth":
        S = local_linear_smoother(g, settings.smooth_oct, np.gradient(np.log2(g)) / sd_D**2)
        return _Base(S @ D, S)
    if settings.baseline == "joint":
        j = fit_joint_baseline(g, D, sd_D, settings.max_width_oct, settings.baseline_penalty)
        return _Base(j.values, j.jacobian, j.sd)
    S = fit_baseline(g, D, sd_D, settings.max_width_oct, settings.baseline_penalty).smoother
    return _Base(S @ D, S)


_SHARED_CACHE: OrderedDict = OrderedDict()
SHARED_CACHE_SIZE = 32  # a comparison asks for each profile's pair once per ear


@dataclass
class EarInput:
    """What an ear's baseline is fitted to."""
    fit: ProbitFit
    D: np.ndarray
    cov_D: np.ndarray

    @property
    def sd(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.cov_D), 1e-12, None))


def ear_input(fit: ProbitFit, settings: CorrectionSettings, consensus: EarConsensus | None = None,
              fixed: list[PeakingFilter] | None = None, own: bool = True) -> EarInput:
    """D and its covariance for an ear (pipeline step 1). own: False for a voter,
    whose covariance with a consensus it's in is treated as the reference's."""
    g = fit.grid
    E = consensus.on(g) if consensus is not None else reference(settings.reference, g, settings.phon)
    cov = fit.cov if consensus is None else (consensus.difference_cov if own else consensus.other_cov)(fit.cov, g)
    return EarInput(fit, fit.L + response(list(fixed or []), g) - E, cov)


def shared_bases(inputs: dict[str, EarInput], settings: CorrectionSettings) -> dict[str, _Base]:
    """One baseline for the ears (DESIGN.md, "Shared baseline"), split into each
    ear's part. Each ear's residual also moves with the other ear's data, through
    the baseline; that's carried as the other ear's covariance and, session by
    session, its deviation. Cached on the inputs: a comparison asks for the
    same pair once per ear."""
    ears = list(inputs)
    grids = [inputs[e].fit.grid for e in ears]
    data, sds = [inputs[e].D for e in ears], [inputs[e].sd for e in ears]
    h = hashlib.sha1(repr((settings.baseline, settings.max_width_oct, settings.baseline_penalty)).encode())
    for a in grids + data + sds:
        h.update(np.ascontiguousarray(a).tobytes())
    key = h.hexdigest()
    if key in _SHARED_CACHE:
        _SHARED_CACHE.move_to_end(key)
        values, J, sd = _SHARED_CACHE[key]
    else:
        if settings.baseline == "joint":
            j = fit_shared_joint_baseline(grids, data, sds, settings.max_width_oct, settings.baseline_penalty)
            values, J, sd = j.values, j.jacobian, j.sd
        else:
            b = fit_shared_baseline(grids, data, sds, settings.max_width_oct, settings.baseline_penalty)
            values, J, sd = b.values, b.smoother, None
        _SHARED_CACHE[key] = values, J, sd
        while len(_SHARED_CACHE) > SHARED_CACHE_SIZE:
            _SHARED_CACHE.popitem(last=False)
    sl = dict(zip(ears, split(grids)))
    out = {}
    for e in ears:
        base = _Base(values[sl[e]], J[sl[e], sl[e]], None if sd is None else sd[sl[e]],
                     other_cov=np.zeros((grids[ears.index(e)].size,) * 2))
        for o in ears:
            if o == e:
                continue
            Jo, fo = J[sl[e], sl[o]], inputs[o].fit
            base.other_cov += Jo @ inputs[o].cov_D @ Jo.T
            if fo.group_trials is not None:
                for sid, k in zip(_group_ids(fo), np.flatnonzero(fo.group_trials)):
                    if sid is not None:
                        base.other_groups[sid] = (-Jo @ fo.u[k], Jo @ fo.group_cov[k] @ Jo.T,
                                                  Jo @ fo.group_u_cov[k] @ Jo.T)
        out[e] = base
    return out


def ear_bases(fits: dict[str, ProbitFit], settings: CorrectionSettings,
              consensus: dict[str, EarConsensus] | None = None,
              fixed: dict[str, list[PeakingFilter]] | None = None, own: bool = True) -> dict[str, _Base] | None:
    """The shared baselines of a profile's ears, or None if they aren't shared
    (the setting is off, the method has no such baseline, or there's one ear)."""
    if not settings.shared_baseline or settings.method != "features" or len(fits) < 2:
        return None
    return shared_bases({e: ear_input(f, settings, (consensus or {}).get(e), (fixed or {}).get(e), own)
                         for e, f in fits.items()}, settings)


def voter_bases(voters: dict[str, list[tuple[str, ProbitFit]]] | None, settings: CorrectionSettings,
                consensus: dict[str, EarConsensus] | None = None) -> dict[str, dict[str, _Base]]:
    """Per ear, each voter's shared baseline (by label), for those with every ear."""
    if not voters:
        return {}
    ears = list(voters)
    out = {e: {} for e in ears}
    for label in {l for e in ears for l, _ in voters[e]}:
        fits = {e: dict(voters[e]).get(label) for e in ears}
        if any(f is None for f in fits.values()):
            continue
        bases = ear_bases(fits, settings, consensus, own=False)
        if bases is not None:
            for e in ears:
                out[e][label] = bases[e]
    return out


def _found(g, T, cov_T, groups, settings: CorrectionSettings, own_R=None) -> list[Feature]:
    """Features of T (steps 4 and 5). With smooth or match they're only a report
    of what's significant; the target is the whole residual."""
    whole = settings.method != "features"
    sd = np.sqrt(np.clip(np.diag(cov_T), 1e-12, None))
    feats = find_features(g, T, cov_T, settings.k, settings.k_broad, np.inf if whole else settings.narrow_max_oct,
                          settings.soft_width)
    seating_step(feats, g, T, cov_T, groups, settings.consistency)
    if not whole:
        for region in broad_regions(feats, T, sd, settings):
            for f in region:
                if f.kind == NARROW and f.accepted:
                    f.accepted = False
                    f.reason = "part of a broad region (searched inside it instead)"
            feats += inner_features(g, T, cov_T, region[0].lo, region[-1].hi, settings, groups, own_R)
    return feats


def _group_ids(fit: ProbitFit) -> list:
    """The session id behind each group with trials, where it's known (None where not)."""
    ks = np.flatnonzero(fit.group_trials)
    if len(fit.groups) == len(fit.group_trials):
        return [fit.groups[k] for k in ks]
    if len(fit.groups) == len(ks):
        return list(fit.groups)
    return [None] * len(ks)


def _group_curves(fit: ProbitFit, R, base: _Base, shift=0.0) -> list[GroupCurve]:
    """Each group's residual against the same baseline, linearised: R + M u_k
    (exact when the baseline is linear). With a shared baseline, the other
    ear's deviation in the same session moves it too; for a session the other
    ear doesn't have (or can't be matched), the other ear's average stands in."""
    if fit.group_trials is None:
        return []
    M = base.M
    out = []
    for sid, k in zip(_group_ids(fit), np.flatnonzero(fit.group_trials)):
        r, cov, u_cov = R + M @ fit.u[k] - shift, M @ fit.group_cov[k] @ M.T, M @ fit.group_u_cov[k] @ M.T
        if base.other_cov is not None:
            other = base.other_groups.get(sid)
            if other is None:
                cov = cov + base.other_cov
            else:
                r, cov, u_cov = r + other[0], cov + other[1], u_cov + other[2]
        out.append(GroupCurve(r, cov, u_cov))
    return out


def voter_candidates(label: str, fit: ProbitFit, settings: CorrectionSettings,
                     consensus: EarConsensus | None, base: _Base | None = None) -> list[Candidate]:
    """What a voter has against this profile's reference: what it would correct,
    and any other narrow lobe tall enough to count. An undetected lobe isn't the
    same as nothing being there. Its correlation with a consensus it's in is
    ignored: this only affects which of its lobes pass as significant.
    base: its shared baseline, if it has one."""
    g = fit.grid
    x = ear_input(fit, settings, consensus, own=False)
    base = base or _baseline(g, x.D, x.sd, settings)
    R = x.D - base.values
    feats = _found(g, R, base.cov(x.cov_D), _group_curves(fit, R, base), settings)
    if settings.veto_softness > 0:
        # Each counts as far as it's significant or tall, so none appears or vanishes at an edge.
        def counted(f):
            own = f.weight if f.accepted else 0.0
            if f.kind == NARROW:
                return max(own, smoothstep((abs(f.peak_db) - LOBE_LO_DB) / (LOBE_HI_DB - LOBE_LO_DB)))
            return own if not settings.preserve_tonality else 0.0
        return [Candidate(label, f.centre_hz, f.eq_width_oct, f.peak_db * counted(f)) for f in feats if counted(f) > 0]
    keep = lambda f: (f.kind == NARROW and (f.accepted or abs(f.peak_db) >= MIN_LOBE_DB)
                      or f.kind == BROAD and f.accepted and not settings.preserve_tonality)
    return [Candidate(label, f.fc, f.width_oct, f.peak_db) for f in feats if keep(f)]


def apply_veto(feats: list[Feature], voters: dict[str, list[Candidate]], settings: CorrectionSettings,
               own_votes: bool = True) -> None:
    """Judge each feature that would be corrected against the others'."""
    for f in feats:
        if not f.accepted or (f.kind == BROAD and settings.preserve_tonality):
            continue
        if settings.veto_softness > 0:
            own = Candidate(OWN_LABEL, f.centre_hz, f.eq_width_oct, f.peak_db)
            f.veto = judge_soft(own, voters, settings.veto_softness, own_votes)
            if own_votes and f.veto.weights[0] >= OWN_WINS:
                f.accepted, f.reason = False, "shared: it's the median feature"
            continue
        own = Candidate(OWN_LABEL, f.fc, f.width_oct, f.peak_db)
        f.veto = judge(own, voters, own_votes)
        if f.veto.median is own:
            f.accepted, f.reason = False, "shared: it's the median feature"


def correct_ear(ear: str, fit: ProbitFit, settings: CorrectionSettings = CorrectionSettings(),
                fixed: list[PeakingFilter] | None = None, consensus: EarConsensus | None = None,
                voters: list[tuple[str, ProbitFit]] | None = None, own_votes: bool = True,
                base: _Base | None = None, voter_base: dict[str, _Base] | None = None) -> EarCorrection:
    """fixed: filters already in use (an incremental refit). The pipeline then
    runs on the corrected curve, L + fixed, and only adds filters for what's
    still significant, within the bands left over.
    consensus: the reference, in place of a fixed one (settings.reference names it).
    voters: the other profiles voting on each feature (settings.veto names them).
    own_votes: False for a comparison's non-voting profile.
    base: this ear's shared baseline (from ear_bases); voter_base: the voters'
    (from voter_bases). Without them each ear has its own."""
    g = fit.grid
    fixed = list(fixed or [])
    if settings.consensus and consensus is None:
        raise ValueError("this profile's consensus reference hasn't been computed")
    if settings.veto and voters is None:
        raise ValueError("this profile's veto votes haven't been supplied")

    E = consensus.on(g) if consensus is not None else reference(settings.reference, g, settings.phon)
    cov_D = fit.cov if consensus is None else consensus.difference_cov(fit.cov, g)
    sd_D = np.sqrt(np.clip(np.diag(cov_D), 1e-12, None))
    H_fixed = response(fixed, g)
    D = fit.L + H_fixed - E
    smooth, match = settings.method == "smooth", settings.method == "match"
    if match:
        # No baseline: only the level is free, aligned robustly over what's corrected.
        inside = (g >= settings.f_lo) & (g <= settings.f_hi)
        E = E + weighted_median(D[inside], (np.gradient(np.log2(g)) / sd_D**2)[inside])
        D = fit.L + H_fixed - E
    if base is None or settings.method != "features":
        base = _baseline(g, D, sd_D, settings)
    M, R = base.M, D - base.values
    cov_R = base.cov(cov_D)
    R_sd = np.sqrt(np.clip(np.diag(cov_R), 1e-12, None))

    pointwise = bool(settings.veto) and settings.veto_mode == "pointwise"
    T, cov_T, median_R = R, cov_R, None
    if pointwise:
        # Found on the excess over the median of the residuals. A checkpoint's
        # filters are part of R but not of the median: a peak they already took
        # out leaves nothing to correct.
        sources = [Source(OWN_LABEL, g, R - M @ H_fixed, cov_R)]
        for label, v in voters:
            xv = ear_input(v, settings, consensus, own=False)
            bv = (voter_base or {}).get(label) or _baseline(v.grid, xv.D, xv.sd, settings)
            sources.append(Source(label, v.grid, xv.D - bv.values, bv.cov(xv.cov_D)))
        sm = consensus_of(sources)
        A = interp_matrix(sm.grid, g)
        median_R = A @ sm.m
        cross = cov_R @ sm.G[0] @ A.T
        T, cov_T = R - median_R, cov_R + A @ sm.cov @ A.T - cross - cross.T
    groups = _group_curves(fit, R, base, 0.0 if median_R is None else median_R)
    session_R = [c.r if median_R is None else c.r + median_R for c in groups]
    checked = len(groups) if settings.consistency and len(groups) >= 2 else 0
    feats = _found(g, T, cov_T, groups, settings, R if pointwise else None)

    for f in feats:
        if f.accepted and not settings.f_lo <= f.fc <= settings.f_hi:
            f.accepted = False
            f.reason = "outside the correction range"
    if settings.veto and not pointwise:
        apply_veto(feats, {label: voter_candidates(label, v, settings, consensus, (voter_base or {}).get(label))
                           for label, v in voters}, settings, own_votes)
    if smooth:
        C = np.clip(-R, -settings.max_cut_db, settings.max_boost_narrow_db)
    elif match:
        C = np.clip(-R, -settings.max_cut_db, settings.max_boost_broad_db)
    else:
        C = build_target(feats, R, settings, g, median_R)
    freqs = log_grid(g[0], g[-1], FINE_STEP_OCT)
    m = interp_matrix(g, freqs)
    # Fade out beyond the range, within the measured margin (850 Hz-15 kHz by default).
    w_lo = min(settings.taper_oct, max(np.log2(settings.f_lo / g[0]), 0.0))
    w_hi = min(settings.taper_oct, max(np.log2(g[-1] / settings.f_hi), 0.0))
    target = (m @ C) * taper(freqs, settings.f_lo, settings.f_hi, w_lo, w_hi)
    sd = np.sqrt(np.einsum("ij,jk,ik->i", m, cov_T, m))
    inner = [f for f in feats if f.accepted and f.residual_sd is not None]
    if inner:
        # The target there is the residual against the local smooth, whose
        # error is smaller than R's (the smooth takes the slow part of it).
        sd_g = np.sqrt(np.clip(np.diag(cov_T), 1e-12, None))
        for f in inner:
            sd_g[f.span] = f.residual_sd
        sd = np.minimum(sd, m @ sd_g)
    bounds = FilterBounds(settings.f_lo, settings.f_hi, settings.q_min, settings.q_max, settings.max_gain_db)
    # The broad shape comes first: if the budget binds, the narrow corrections
    # give way, smallest first, and never the compensation's bands.
    budget = max(settings.bands - len(fixed), 0)
    compensating = settings.preserve_tonality and not match
    reserve = min(COMP_MAX_BANDS, budget // 2) if compensating else 0
    ff = fit_filters(freqs, target, sd, budget - reserve, bounds, settings.fit_tol_db, settings.min_gain_db)
    unfitted = target - ff.response
    comp: list[PeakingFilter] = []
    if compensating:
        for f in feats:
            if f.accepted and f.kind == BROAD:
                f.accepted = False
                f.reason = "broad: kept as tonality (correct it with --broad-strength)"
        ff, comp = add_tonality_compensation(ff, freqs, settings, w_lo, w_hi, len(fixed))
    if fixed:
        contrib = dict(zip(map(id, ff.filters), ff.contributions))
        allf = sorted(fixed + ff.filters, key=lambda f: f.fc)
        ff = replace(ff, filters=allf, response=ff.response + response(fixed, freqs),
                     contributions=[contrib.get(id(f), 0.0) for f in allf])

    left = []
    if ff.budget_bound:
        over = (np.abs(unfitted) > settings.fit_tol_db) & (freqs >= settings.f_lo) & (freqs <= settings.f_hi)
        i = 0
        while i < freqs.size:
            if over[i]:
                j = i
                while j + 1 < freqs.size and over[j + 1]:
                    j += 1
                left.append((float(freqs[i]), float(freqs[j]), float(np.abs(unfitted[i:j + 1]).max())))
                i = j + 1
            else:
                i += 1
    return EarCorrection(ear, g, fit.L, fit.sd, E, base.values, R, R_sd, feats, C, freqs, target, sd, ff,
                         left, checked, session_R,
                         [] if fit.group_trials is None else [int(n) for n in fit.group_trials if n],
                         comp, fixed, consensus, base.sd)


def ear_reference(sm: SoftMedian, members: list[str], key: str, smooth_oct: float,
                  own: int | None = None) -> EarConsensus:
    """A raw consensus as a reference for one profile: smoothed, and with that
    profile's weights if it's a member (own: its index among the sources)."""
    g = sm.grid
    S = np.eye(g.size) if smooth_oct == 0 else local_linear_smoother(g, smooth_oct, np.gradient(np.log2(g)))
    return EarConsensus(members, key, g, S @ sm.m, S @ sm.cov @ S.T, sm.tau_hp,
                        None if own is None else sm.G[own] @ S.T, smooth_oct)


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
    cf = fit_filters(freqs, goal, np.full(freqs.size, COMP_TOL_DB / 2), room, bounds, COMP_TOL_DB, 0.2)
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
                     "level_db": f.level_db, "level_sd": f.level_sd, "seat_spread": f.seat_spread,
                     "inside_broad": f.inside_broad, "scale": f.scale,
                     "veto": None if f.veto is None else asdict(f.veto)}
                    for f in c.features],
            }
        return out


def correct(fits: dict[str, ProbitFit], settings: CorrectionSettings = CorrectionSettings(),
            fixed: dict[str, list[PeakingFilter]] | None = None,
            consensus: dict[str, EarConsensus] | None = None,
            voters: dict[str, list[tuple[str, ProbitFit]]] | None = None) -> Correction:
    bases = ear_bases(fits, settings, consensus, fixed) or {}
    vb = voter_bases(voters, settings, consensus)
    ears = {e: correct_ear(e, fits[e], settings, (fixed or {}).get(e), (consensus or {}).get(e), (voters or {}).get(e),
                           base=bases.get(e), voter_base=vb.get(e))
            for e in fits}
    pre = min(preamp_db(c.filters) for c in ears.values())
    return Correction(settings, ears, pre)
