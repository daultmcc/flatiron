"""Residual features, significance and consistency (DESIGN.md, pipeline
steps 3-5).

The residual R = D − baseline is split into lobes between sign changes, and a
lobe is split again at any valley no deeper than half the smaller peak on
either side (two same-sign features rarely cross zero between them).
A lobe narrower than `narrow_max_oct` (at half height) is narrow: it is kept
if at least two adjacent points exceed k·SD. A wider lobe is broad: it is kept
if its mean exceeds k_broad times the SD of that mean. (A broad lobe is never
re-tested point by point: its errors are correlated, so a pointwise test would
pass the top of a hump whose mean isn't significant.)

Kept features can then be checked across reseat sessions: a feature whose
per-session means disagree by more than their noise allows (Cochran's Q) is
dropped as fit-dependent.
"""

from dataclasses import dataclass, field

import numpy as np
from scipy.stats import chi2

NARROW, BROAD = "narrow", "broad"


@dataclass
class Feature:
    lo: int  # first grid index
    hi: int  # last grid index (inclusive)
    fc: float  # frequency of the extreme (Hz)
    peak_db: float  # residual at the extreme
    width_oct: float  # full width at half height
    kind: str
    z: float  # narrow: largest run minimum |R|/SD; broad: mean / SD(mean)
    mean_db: float
    mean_sd: float
    accepted: bool = False
    reason: str = ""
    consistency_p: float | None = None
    session_means: list = field(default_factory=list)
    # Found inside a broad lobe, against a local smooth: its residual over
    # the span (the pipeline's R includes the lobe's broad offset there).
    residual: np.ndarray | None = None
    residual_sd: np.ndarray | None = None
    inside_broad: bool = False
    run: int = -1  # which same-sign stretch of the residual it was split from

    def shifted(self, n: int) -> "Feature":
        self.lo += n
        self.hi += n
        return self

    @property
    def span(self) -> slice:
        return slice(self.lo, self.hi + 1)


def _half_width(x, r, lo, hi, k):
    """FWHM (octaves) of |r| around index k within [lo, hi]."""
    a = np.abs(r)
    half = a[k] / 2

    def edge(step):
        i = k
        while lo <= i + step <= hi and a[i + step] > half:
            i += step
        j = i + step
        if not lo <= j <= hi:
            return x[i]
        t = (a[i] - half) / (a[i] - a[j]) if a[i] != a[j] else 0.5
        return x[i] + t * (x[j] - x[i])

    return edge(1) - edge(-1)


def _runs(mask):
    """(start, end) inclusive index runs where mask is true."""
    out, start = [], None
    for i, m in enumerate(np.append(mask, False)):
        if m and start is None:
            start = i
        elif not m and start is not None:
            out.append((start, i - 1))
            start = None
    return out


def _mean_stats(x, r, cov, lo, hi):
    w = np.gradient(x)[lo:hi + 1]
    w = w / w.sum()
    seg = r[lo:hi + 1]
    v = w @ cov[lo:hi + 1, lo:hi + 1] @ w
    return float(w @ seg), float(np.sqrt(max(v, 1e-12)))


def find_features(grid, r, cov, k=2.0, k_broad=2.5, narrow_max_oct=1 / 3) -> list[Feature]:
    x = np.log2(grid)
    sd = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    sign = np.sign(r)
    feats = []
    for n, lo, hi in ((n, *part) for n, run in enumerate(_sign_runs(sign)) for part in _split_valleys(r, *run)):
        k_pk = lo + int(np.argmax(np.abs(r[lo:hi + 1])))
        width = _half_width(x, r, lo, hi, k_pk)
        mean, msd = _mean_stats(x, r, cov, lo, hi)
        over = _runs(np.abs(r[lo:hi + 1]) > k * sd[lo:hi + 1])
        long_runs = [(a + lo, b + lo) for a, b in over if b > a]
        run_z = max((np.min(np.abs(r[a:b + 1]) / sd[a:b + 1]) for a, b in long_runs), default=0.0)
        kind = NARROW if width < narrow_max_oct else BROAD
        f = Feature(lo, hi, float(grid[k_pk]), float(r[k_pk]), float(width), kind,
                    float(run_z if kind == NARROW else abs(mean) / msd), mean, msd, run=n)
        if kind == NARROW:
            f.accepted = bool(long_runs)
            f.reason = "" if f.accepted else f"no two adjacent points beyond {k:g} SD"
        else:
            f.accepted = abs(mean) > k_broad * msd
            f.reason = "" if f.accepted else f"mean {mean:+.2f} dB is within {k_broad:g} SD ({msd:.2f} dB)"
        feats.append(f)
    return feats


def _sign_runs(sign):
    out, start = [], 0
    for i in range(1, sign.size + 1):
        if i == sign.size or sign[i] != sign[start]:
            if sign[start] != 0:
                out.append((start, i - 1))
            start = i
    return out


def _split_valleys(r, lo, hi, ratio=0.5):
    """Split [lo, hi] at the deepest qualifying valley of |r|, recursively."""
    a = np.abs(r)
    best = None
    for v in range(lo + 1, hi):
        if a[v] <= a[v - 1] and a[v] <= a[v + 1]:
            left, right = a[lo:v].max(), a[v + 1:hi + 1].max()
            if a[v] <= ratio * min(left, right) and (best is None or a[v] < a[best]):
                best = v
    if best is None:
        return [(lo, hi)]
    return _split_valleys(r, lo, best, ratio) + _split_valleys(r, best + 1, hi, ratio)


def check_consistency(features: list[Feature], grid, r, session_residuals, p_min=0.01) -> None:
    """session_residuals: list of (R_s, cov_s), one per session with enough
    trials; r: the pooled residual. Drops accepted features whose per-session
    levels disagree. Each session is measured over the feature's core (where
    the pooled feature is above half height), not the whole lobe: a lobe's
    mean barely changes when the feature inside it moves."""
    if len(session_residuals) < 2:
        return
    x = np.log2(grid)
    for f in features:
        if not f.accepted:
            continue
        seg = np.abs(r[f.span])
        core = np.flatnonzero(seg >= 0.5 * seg.max()) + f.lo
        lo, hi = int(core.min()), int(core.max())
        ms = [_mean_stats(x, rs, c, lo, hi) for rs, c in session_residuals]
        m = np.array([a for a, _ in ms])
        v = np.array([b for _, b in ms]) ** 2
        pooled = np.sum(m / v) / np.sum(1 / v)
        q = float(np.sum((m - pooled) ** 2 / v))
        f.consistency_p = float(chi2.sf(q, len(ms) - 1))
        f.session_means = [(float(a), float(b)) for a, b in ms]
        if f.consistency_p < p_min:
            f.accepted = False
            f.reason = f"inconsistent across reseats (p = {f.consistency_p:.3g})"
