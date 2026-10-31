"""From a perceptual fit to correction filters, per ear (DESIGN.md, "Pipeline").

Every setting here is downstream of the cached perceptual fit, so it can be
changed without new trials.
"""

from dataclasses import asdict, dataclass, field

import numpy as np

from flatiron.audio.biquad import PeakingFilter
from flatiron.fit.baseline import fit_baseline
from flatiron.fit.features import BROAD, Feature, check_consistency, find_features
from flatiron.fit.filters import FilterBounds, FilterFit, fit_filters, preamp_db
from flatiron.fit.reference import reference
from flatiron.model.grid import interp_matrix, log_grid
from flatiron.model.probit import ProbitFit

FINE_STEP_OCT = 1 / 96


@dataclass(frozen=True)
class CorrectionSettings:
    max_width_oct: float = 1.0  # W: anything broader is left to the user's EQ
    reference: str = "iso226"  # iso226 | diffuse | none | path to a CSV
    phon: float = 60.0
    baseline_penalty: float = 0.1  # baseline stiffness (tuned in the simulator; 1e-3 absorbed clustered features)
    k: float = 2.0
    k_broad: float = 2.5
    narrow_max_oct: float = 1 / 3
    broad_strength: float = 1.0  # 0..1, scales broad features only
    max_cut_db: float = 12.0
    max_boost_narrow_db: float = 4.0
    max_boost_broad_db: float = 6.0
    f_lo: float = 1000.0
    f_hi: float = 13000.0  # trials near the top of the range are hard to judge; don't go below 12 kHz
    taper_oct: float = 1 / 3  # fade beyond [f_lo, f_hi], limited by the measured margin
    bands: int = 10
    q_min: float = 0.7
    q_max: float = 16.0
    max_gain_db: float = 12.0
    min_gain_db: float = 0.5
    stop_z: float = 2.0  # filter fit stops when no point is off by more than this many SDs
    consistency: bool = True
    consistency_p: float = 0.01

    def __post_init__(self):
        if not 1 / 3 - 1e-9 <= self.max_width_oct <= 2 + 1e-9:
            raise ValueError("max feature width must be 1/3 to 2 octaves")
        if not 0 <= self.broad_strength <= 1:
            raise ValueError("broad strength must be 0 to 1")
        if not 0 < self.bands <= 32:
            raise ValueError("band budget must be 1 to 32")
        if not self.f_lo < self.f_hi:
            raise ValueError("f_lo must be below f_hi")


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

    @property
    def filters(self) -> list[PeakingFilter]:
        return self.fit.filters


def taper(freqs, f_lo, f_hi, width_lo_oct, width_hi_oct) -> np.ndarray:
    """1 inside [f_lo, f_hi], raised-cosine to 0 over the given widths outside
    it (DESIGN.md step 6), 0 beyond. A zero width is a hard edge."""
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
        seg = -R[f.span]
        if f.kind == BROAD:
            seg = np.minimum(seg, settings.max_boost_broad_db) * settings.broad_strength
        else:
            seg = np.minimum(seg, settings.max_boost_narrow_db)
        c[f.span] += np.maximum(seg, -settings.max_cut_db)
    return c


def correct_ear(ear: str, fit: ProbitFit, settings: CorrectionSettings = CorrectionSettings(),
                session_fits: list[ProbitFit] | None = None) -> EarCorrection:
    g = fit.grid
    E = reference(settings.reference, g, settings.phon)
    D = fit.L - E
    base = fit_baseline(g, D, fit.sd, settings.max_width_oct, settings.baseline_penalty)
    M = np.eye(g.size) - base.smoother
    R = M @ D
    cov_R = M @ fit.cov @ M.T
    R_sd = np.sqrt(np.clip(np.diag(cov_R), 1e-12, None))

    feats = find_features(g, R, cov_R, settings.k, settings.k_broad, settings.narrow_max_oct)
    checked, session_R = 0, []
    if session_fits:
        per = [(M @ (s.L - E), M @ s.cov @ M.T) for s in session_fits]
        session_R = [r for r, _ in per]
        if settings.consistency:
            checked = len(per)
            check_consistency(feats, g, R, per, settings.consistency_p)

    for f in feats:
        if f.accepted and not settings.f_lo <= f.fc <= settings.f_hi:
            f.accepted = False
            f.reason = "outside the correction range"
    C = build_target(feats, R, settings)
    freqs = log_grid(g[0], g[-1], FINE_STEP_OCT)
    m = interp_matrix(g, freqs)
    # Fade out beyond the range, within the measured margin (850 Hz-15 kHz by default).
    w_lo = min(settings.taper_oct, max(np.log2(settings.f_lo / g[0]), 0.0))
    w_hi = min(settings.taper_oct, max(np.log2(g[-1] / settings.f_hi), 0.0))
    target = (m @ C) * taper(freqs, settings.f_lo, settings.f_hi, w_lo, w_hi)
    sd = np.sqrt(np.einsum("ij,jk,ik->i", m, cov_R, m))
    bounds = FilterBounds(settings.f_lo, settings.f_hi, settings.q_min, settings.q_max, settings.max_gain_db)
    ff = fit_filters(freqs, target, sd, settings.bands, bounds, settings.stop_z, settings.min_gain_db)

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
    return EarCorrection(ear, g, fit.L, fit.sd, E, base.values, R, R_sd, feats, C, freqs, target, sd, ff,
                         left, checked, session_R)


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
                "chi2": c.fit.chi2,
                "budget_bound": c.fit.budget_bound,
                "left_out": c.left_out,
                "curve": {"freq": c.grid.tolist(), "L": c.L.tolist(), "sd": c.L_sd.tolist()},
                "features": [
                    {"fc": f.fc, "kind": f.kind, "peak_db": f.peak_db, "width_oct": f.width_oct,
                     "z": f.z, "accepted": f.accepted, "reason": f.reason,
                     "consistency_p": f.consistency_p}
                    for f in c.features],
            }
        return out


def correct(fits: dict[str, ProbitFit], settings: CorrectionSettings = CorrectionSettings(),
            session_fits: dict[str, list[ProbitFit]] | None = None) -> Correction:
    ears = {e: correct_ear(e, fits[e], settings, (session_fits or {}).get(e)) for e in fits}
    pre = min(preamp_db(c.filters) for c in ears.values())
    return Correction(settings, ears, pre)
