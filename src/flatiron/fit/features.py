"""Residual features, significance and the seating step (DESIGN.md, pipeline
steps 3-5).

The residual R = D − baseline is split into lobes between sign changes, and a
lobe is split again at any valley no deeper than half the smaller peak on
either side (two same-sign features rarely cross zero between them).
A lobe narrower than `narrow_max_oct` (at half height) is narrow: it is kept
if at least two adjacent points exceed k·SD. A wider lobe is broad: it is kept
if its mean exceeds k_broad times the SD of that mean. (A broad lobe is never
re-tested point by point: its errors are correlated, so a pointwise test would
pass the top of a hump whose mean isn't significant.)

Kept features then go through the seating step. Each one's level is taken in
every group's curve (L_avg + u_g, posterior mean) over the feature's half-height
core. The feature is corrected to the median group's level (weighted by
precision), as a share of its level in L_avg, and dropped if that's under
MIN_SCALE (most seatings show little of it). A resonance only some seatings have
sits lower in L_avg than in those seatings, so the share can be above 1. The
model has already shrunk the groups together as far as the data allow, so
there's no significance gate: the share just stays near 1 when the seatings agree.
"""

from dataclasses import dataclass, field

import numpy as np

NARROW, BROAD = "narrow", "broad"
MIN_SCALE = 0.2
NOTE_SCALE = 0.1  # a share further than this from 1 is worth telling the listener about


def weighted_median(values, weights) -> float:
    """Midpoint rule: each value's weight is centred on it."""
    v, w = np.asarray(values, float), np.asarray(weights, float)
    order = np.argsort(v)
    v, w = v[order], w[order]
    cum = (np.cumsum(w) - 0.5 * w) / w.sum()
    return float(np.interp(0.5, cum, v))


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
    level_db: float | None = None  # its mean over the half-height core, in L_avg
    level_sd: float | None = None  # the SD of that: whether it's real
    seat_spread: float | None = None  # how far a typical seating's level is from it (dB)
    group_levels: list = field(default_factory=list)  # (level, SD) in each group's curve
    # Found inside a broad lobe, against a local smooth: its residual over
    # the span (the pipeline's R includes the lobe's broad offset there).
    residual: np.ndarray | None = None
    residual_sd: np.ndarray | None = None
    veto: object = None  # with the feature veto: its candidates and median feature (fit/veto.py)
    residual_R: np.ndarray | None = None  # pointwise veto: `residual` is the excess, this the profile's own
    correction_db: float | None = None  # pointwise veto: what's corrected at the feature's largest correction
    shared_db: float | None = None  # pointwise veto: how much of the residual there is kept as shared
    inside_broad: bool = False
    scale: float = 1.0  # share of the residual corrected: the median seating's level over L_avg's
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


@dataclass
class GroupCurve:
    """A group's curve through the same steps as the residual being tested."""
    r: np.ndarray  # its residual (posterior mean)
    cov: np.ndarray  # covariance of that
    u_cov: np.ndarray  # covariance of its deviation from the average


def seating_step(features: list[Feature], grid, r, cov, groups: list[GroupCurve], apply: bool = True) -> None:
    """r, cov: the residual of L_avg. Each feature is measured over its core
    (where it's above half height), not the whole lobe: a lobe's mean barely
    changes when the feature inside it moves. The report's two numbers are set
    whenever there are groups. apply: also scale or drop what the seatings say
    to (needs two groups: one says nothing about the others)."""
    x = np.log2(grid)
    for f in features:
        if not f.accepted:
            continue
        seg = np.abs(r[f.span])
        core = np.flatnonzero(seg >= 0.5 * seg.max()) + f.lo
        lo, hi = int(core.min()), int(core.max())
        f.level_db, f.level_sd = _mean_stats(x, r, cov, lo, hi)
        if not groups:
            continue
        ms = [_mean_stats(x, g.r, g.cov, lo, hi) for g in groups]
        f.group_levels = [(float(a), float(b)) for a, b in ms]
        m = np.array([a for a, _ in ms])
        sd = np.array([b for _, b in ms])
        # Expected squared deviation of each seating, with its posterior
        # uncertainty: the posterior means alone are shrunk together.
        var_u = np.array([_mean_stats(x, g.r, g.u_cov, lo, hi)[1] ** 2 for g in groups])
        f.seat_spread = float(np.sqrt(np.mean((m - f.level_db) ** 2 + var_u)))
        if not apply or len(groups) < 2:
            continue
        f.scale = max(weighted_median(m, 1 / sd**2) / f.level_db, 0.0) if f.level_db else 0.0
        if f.scale < MIN_SCALE:
            f.accepted, f.scale = False, 0.0
            f.reason = "most seatings show little of it"
        elif abs(f.scale - 1) > NOTE_SCALE:
            f.reason = f"corrected to the median seating's level, {f.scale:.0%} of the average's"
