"""The listener's loudness-threshold gap g (DESIGN.md, "Threshold-anchored
reference").

A headphone's response reaches both the loudness curve L and the threshold
curve T, and the listener's hearing adds to each in its own way. Per profile
p and ear e, over the listener's default measured range (within the
profile's measured range, and where both its curves were fitted):

    L_pe(f) + T_pe(f) = g(f) + δ_p(f) + k_pe + m_pe(f)

- g: how the listener's loudness above threshold departs from their
  threshold, the same for every profile: piecewise linear in log f, flat
  beyond the end knots. The knots are the ends of the listener's default
  measured range, and 1, 2, 4, 8 and 13 kHz where they're at least a quarter
  octave inside it (gap_knots). Its level is arbitrary (only its shape is
  used).
- δ_p: the profile's own low-frequency departure, hats at 1 and 2 kHz that
  are zero from 3 kHz up, prior SD 2.5 dB: the profiles agreed in the treble
  and not quite below 3 kHz.
- k_pe: a constant per profile and ear (L's level is arbitrary, T's in dBFS).
- m_pe: fine ripple of the threshold (and anything else narrow), Gaussian with
  SD ripple_sd correlated over RIPPLE_ELL_OCT; ripple_sd maximises the
  (restricted) likelihood.

L comes from each ear's own perceptual fit and T from the profile's threshold
fit, with their covariances. Like the cross-ear bias, g is fitted once to
every headphone or IEM profile with MIN_THRESHOLD_TRIALS threshold trials,
cached at the top of the data folder, and held fixed wherever it's used.
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from scipy.optimize import minimize_scalar

from flatiron import EARS
from flatiron.model.grid import MEASURE_HI, MEASURE_LO, interp_matrix, log_grid
from flatiron.session.store import Profile, measured_default

INNER_KNOTS_HZ = (1000.0, 2000.0, 4000.0, 8000.0, 13000.0)  # kept where a quarter octave inside the range
KNOT_CLEARANCE_OCT = 0.25
LOW_KNOTS_HZ = (1000.0, 2000.0, 3000.0)  # δ_p: hats at the first two, zero from the last
DELTA_SD_DB = 2.5
G_SD_DB = 30.0  # keeps the prior proper; no real pull
CONST_SD_DB = 1e4
RIPPLE_ELL_OCT = 0.1
RIPPLE_RANGE_DB = (0.05, 10.0)
MIN_THRESHOLD_TRIALS = 150  # a profile's threshold trials before it counts
GAP_CACHE_KEEP = 200
VERSION = "2"


def gap_knots(lo: float, hi: float) -> tuple:
    """g's knots for a listener whose default measured range is lo to hi."""
    inner = [k for k in INNER_KNOTS_HZ
             if np.log2(k / lo) >= KNOT_CLEARANCE_OCT and np.log2(hi / k) >= KNOT_CLEARANCE_OCT]
    return (float(lo), *inner, float(hi))


KNOTS_HZ = gap_knots(MEASURE_LO, MEASURE_HI)  # with the default range


def listener_range(root: Path) -> tuple[float, float]:
    """The listener's default measured range (root: the listener's folder)."""
    return measured_default(root / "listener.json")


def hats(freqs, knots) -> np.ndarray:
    """Piecewise-linear basis in log f (one column per knot), flat beyond the ends."""
    x = np.log2(np.atleast_1d(np.asarray(freqs, float)))
    xk = np.log2(np.asarray(knots, float))
    return np.column_stack([np.interp(x, xk, row) for row in np.eye(xk.size)])


def low_hats(freqs) -> np.ndarray:
    """δ_p's basis: hats at 1 and 2 kHz, zero from 3 kHz up (flat below 1 kHz)."""
    return hats(freqs, LOW_KNOTS_HZ)[:, :2]


@dataclass
class ListenerGap:
    knots_hz: list
    g: list  # at the knots, their mean removed
    cov: list  # of g (the same gauge)
    ripple_sd: float
    profiles: dict = field(default_factory=dict)  # slug: {delta, delta_sd, trials}
    key: str = ""

    def curve(self, freqs) -> np.ndarray:
        return hats(freqs, self.knots_hz) @ np.asarray(self.g)

    def curve_sd(self, freqs) -> np.ndarray:
        B = hats(freqs, self.knots_hz)
        return np.sqrt(np.clip(np.einsum("ij,jk,ik->i", B, np.asarray(self.cov), B), 0, None))

    def delta(self, slug: str, freqs) -> np.ndarray:
        """The profile's own low-frequency departure (0 for a profile that isn't a member)."""
        d = self.profiles.get(slug)
        return np.zeros(np.size(freqs)) if d is None else low_hats(freqs) @ np.asarray(d["delta"])

    def curve_for(self, slug: str, freqs) -> np.ndarray:
        return self.curve(freqs) + self.delta(slug, freqs)


# -- each profile's threshold fit -----------------------------------------------

_THRESHOLD_MEMO: dict = {}


def threshold_key(profile: Profile) -> str:
    """What a profile's threshold fit depends on (so it's only refitted when that changes)."""
    h = hashlib.sha256(VERSION.encode())
    for s in profile.threshold_sessions():
        h.update(json.dumps([s.id, s.task, s.stimulus_key(),
                             [(t.ear, t.freq, t.level_dbfs, t.eq_gain, t.kind, t.positive, t.response, t.undone)
                              for t in s.trials]]).encode())
    h.update(json.dumps(list(profile.measured)).encode())
    return h.hexdigest()[:20]


def threshold_fit(profile: Profile):
    """The profile's threshold fit (model/threshold.py) over all its threshold
    sessions, or None without any; memoised by threshold_key. Its grid spans
    the measured range and every session's frequencies."""
    from flatiron.model.threshold import ThresholdData, fit_thresholds

    key = (str(profile.dir), threshold_key(profile))
    if key in _THRESHOLD_MEMO:
        return _THRESHOLD_MEMO[key]
    ss = profile.threshold_sessions()
    fit = None
    if ss:
        data = ThresholdData.build([(s.stimulus_key(), s.trials) for s in ss])
        if data.freq.size:
            lo, hi = profile.measured
            grid = log_grid(min(lo, float(data.freq.min())), max(hi, float(data.freq.max())))
            fit = fit_thresholds(data, grid)
    _THRESHOLD_MEMO[key] = fit
    return fit


def gap_grid(profile: Profile, lo: float, hi: float, *grids) -> np.ndarray | None:
    """Where a profile enters g: the listener's range lo to hi, within the
    profile's measured range and every given grid (its fitted curves'); None
    if that's under a quarter octave."""
    a, b = profile.measured
    a, b = max(lo, a, *(float(g[0]) for g in grids)), min(hi, b, *(float(g[-1]) for g in grids))
    return log_grid(a, b) if b > a * 2 ** KNOT_CLEARANCE_OCT else None


# -- the fit ----------------------------------------------------------------------

@dataclass
class _Member:
    slug: str
    grid: np.ndarray
    d: dict  # ear: L + T on the grid
    V0: dict  # ear: Cov(L) + Cov(T)
    trials: int


def fit_gap(members: list[_Member], knots=KNOTS_HZ) -> ListenerGap:
    """g, each member's δ and the ripple SD (see the module docstring)."""
    knots = tuple(float(k) for k in knots)
    nk, P = len(knots), len(members)
    keys = [(i, e) for i in range(P) for e in EARS]
    n_par = nk + 2 * P + len(keys)  # g, δ per profile, a constant per profile and ear
    prior = np.concatenate([np.full(nk, G_SD_DB ** -2), np.full(2 * P, DELTA_SD_DB ** -2),
                            np.full(len(keys), CONST_SD_DB ** -2)])
    blocks = []
    for j, (i, e) in enumerate(keys):
        m = members[i]
        X = np.zeros((m.grid.size, n_par))
        X[:, :nk] = hats(m.grid, knots)
        X[:, nk + 2 * i:nk + 2 * i + 2] = low_hats(m.grid)
        X[:, nk + 2 * P + j] = 1.0
        x = np.log2(m.grid)
        K = np.exp(-0.5 * ((x[:, None] - x[None, :]) / RIPPLE_ELL_OCT) ** 2)
        blocks.append((X, m.d[e], m.V0[e], K))

    def solve(ripple_sd):
        A, b, ld = np.diag(prior), np.zeros(n_par), 0.0
        parts = []
        for X, y, V0, K in blocks:
            V = V0 + ripple_sd ** 2 * K + 1e-6 * np.eye(y.size)
            c = np.linalg.cholesky(V)
            Xw, yw = np.linalg.solve(c, X), np.linalg.solve(c, y)
            A += Xw.T @ Xw
            b += Xw.T @ yw
            ld += 2 * np.log(np.diag(c)).sum()
            parts.append((Xw, yw))
        cA = np.linalg.cholesky(A)
        w = np.linalg.solve(cA.T, np.linalg.solve(cA, b))
        quad = sum(float(np.sum((yw - Xw @ w) ** 2)) for Xw, yw in parts) + float(w @ (prior * w))
        nll = 0.5 * (quad + ld + 2 * np.log(np.diag(cA)).sum())
        return nll, w, A

    res = minimize_scalar(lambda ls: solve(np.exp(ls))[0], bounds=np.log(RIPPLE_RANGE_DB), method="bounded",
                          options={"xatol": 0.01})
    ripple = float(np.exp(res.x))
    _, w, A = solve(ripple)
    C = np.linalg.inv(A)
    D = np.eye(nk) - np.full((nk, nk), 1.0 / nk)  # the knots' mean removed
    g, cg = D @ w[:nk], D @ C[:nk, :nk] @ D.T
    profiles = {}
    for i, m in enumerate(members):
        sl = slice(nk + 2 * i, nk + 2 * i + 2)
        profiles[m.slug] = {"delta": w[sl].round(4).tolist(),
                            "delta_sd": np.sqrt(np.diag(C[sl, sl])).round(4).tolist(), "trials": m.trials}
    return ListenerGap(list(knots), g.round(4).tolist(), cg.round(6).tolist(), round(ripple, 4), profiles)


def gap_members(root: Path, on_fit: Callable[[], None] | None = None) -> tuple[list[_Member], list]:
    """Every headphone or IEM profile with enough threshold trials: its L + T
    per ear, and what they depend on (for the cache key)."""
    from flatiron.fit.run import NOISE_KINDS, cached_fit, fit_key, listener_noise, profile_priors

    members, keys, noise = [], [], None
    lo, hi = listener_range(root)
    for p in Profile.all(root):
        if p.kind not in NOISE_KINDS:
            continue
        n = sum(len(s.live()) for s in p.threshold_sessions())
        if n < MIN_THRESHOLD_TRIALS:
            continue
        trials, grids = p.history()
        if any(len(trials[e]) < 2 or e not in grids for e in EARS):
            continue
        tf = threshold_fit(p)
        if tf is None:
            continue
        noise = noise or listener_noise(root, on_fit)
        priors = profile_priors(p)
        fits = {e: cached_fit(p, trials[e], np.asarray(grids[e], float), noise[e], priors) for e in EARS}
        # Only where both curves were fitted: beyond a grid, interpolation would hold its end value.
        G = gap_grid(p, lo, hi, tf.grid, *(f.grid for f in fits.values()))
        if G is None:
            continue
        M = interp_matrix(tf.grid, G)
        nt = tf.grid.size
        d, V0 = {}, {}
        for i, e in enumerate(EARS):
            f = fits[e]
            A = interp_matrix(f.grid, G)
            cT = tf.cov[i * nt:(i + 1) * nt, i * nt:(i + 1) * nt]
            d[e] = A @ f.L + M @ tf.T[i]
            V0[e] = A @ f.cov @ A.T + M @ cT @ M.T
        members.append(_Member(p.slug, G, d, V0, n))
        keys.append([p.slug, threshold_key(p)] + [fit_key(trials[e], np.asarray(grids[e], float), noise[e], priors)
                                                   for e in EARS])
    return members, keys


def listener_gap(root: Path, on_fit: Callable[[], None] | None = None) -> ListenerGap | None:
    """The listener's g, cached in cache/gap/ keyed by everything it depends
    on. None: no profile has enough threshold trials."""
    members, keys = gap_members(root, on_fit)
    if not members:
        return None
    knots = gap_knots(*listener_range(root))
    key = hashlib.sha256(json.dumps([VERSION, knots, LOW_KNOTS_HZ, DELTA_SD_DB, RIPPLE_ELL_OCT]
                                    + sorted(keys)).encode()).hexdigest()[:24]
    folder = root / "cache" / "gap"
    path = folder / f"{key}.json"
    if path.exists():
        os.utime(path)
        return ListenerGap(**json.loads(path.read_text()))
    if on_fit is not None:
        on_fit()
    out = fit_gap(members, knots)
    out.key = key
    folder.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp")
    tmp.write_text(json.dumps(asdict(out)))
    tmp.replace(path)
    for old in sorted(folder.glob("*.json"), key=lambda q: q.stat().st_mtime)[:-GAP_CACHE_KEEP]:
        old.unlink(missing_ok=True)
    return out
