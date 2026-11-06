"""Consensus across profiles (ADDENDUM.md, Features 1a and 1b): the soft median,
how much profiles disagree, and the consensus of raw curves.

Statistics stay on each profile's own grid, because interpolating a fit onto a
finer one adds points but no measurements. The consensus m itself lives on a
common 1/48-octave grid and meets a profile through A_p, the interpolation from
the common grid to that profile's grid.
"""

from dataclasses import dataclass, replace

import numpy as np
from scipy.stats import chi2

from flatiron.model.grid import common_grid, interp_matrix, second_difference
from flatiron.model.probit import Priors

MODEL_VERSION = "2"  # 2: raw curves with robust offsets, listed members only
TAU_HP_FLOOR_DB = 0.5
IRLS_ITERS = 100
IRLS_TOL_DB = 1e-4
OFFSET_ITERS = 100
OFFSET_TOL_DB = 2e-3
# Octave widths are relative to this, so a point at the usual spacing counts as
# one measurement. It's what the curvature penalty is scaled to: the same penalty
# against raw octave widths (1/24 per point) would smooth about 24 times too hard.
WEIGHT_STEP_OCT = 1 / 24
KINDS_VOTING = ("headphone",)


@dataclass
class Source:
    """One profile's residual in one ear, on its own grid."""
    label: str
    grid: np.ndarray
    R: np.ndarray
    cov: np.ndarray
    kind: str = "headphone"

    @property
    def sd(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.cov), 1e-12, None))


@dataclass
class SoftMedian:
    grid: np.ndarray  # the common grid
    m: np.ndarray
    cov: np.ndarray
    tau_hp: float
    G: list[np.ndarray]  # per source: m = sum(G_p.T @ R_p), weights fixed at their final values
    robust: bool
    offsets: np.ndarray | None = None  # raw consensus: each source's level against m (they sum to zero)

    @property
    def sd(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.cov), 0, None))

    def on(self, grid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """m and its covariance on a profile's own grid."""
        A = interp_matrix(self.grid, grid)
        return A @ self.m, A @ self.cov @ A.T


def _to_common(s: Source, common: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    up = interp_matrix(s.grid, common)
    return up @ s.R, np.einsum("ij,jk,ik->i", up, s.cov, up)


def heterogeneity(sources: list[Source], common: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per common-grid point: the DerSimonian–Laird between-source variance, and
    Cochran's Q p-value."""
    pts = [_to_common(s, common) for s in sources]
    R = np.array([p[0] for p in pts])
    w = 1.0 / np.clip(np.array([p[1] for p in pts]), 1e-12, None)
    sw = w.sum(axis=0)
    mean = (w * R).sum(axis=0) / sw
    q = (w * (R - mean) ** 2).sum(axis=0)
    df = len(sources) - 1
    denom = np.maximum(sw - (w**2).sum(axis=0) / sw, 1e-12)
    return np.maximum((q - df) / denom, 0.0), chi2.sf(q, df)


def estimate_tau_hp(sources: list[Source], common: np.ndarray) -> float:
    """One value per ear and profile set: a separate one at each frequency would
    let a headphone's own peak inflate the tolerance at that peak, and the soft
    median would act like a mean exactly where it most needs to be robust."""
    t2, _ = heterogeneity(sources, common)
    return max(float(np.median(np.sqrt(t2))), TAU_HP_FLOOR_DB)


def _curvature_penalty(common: np.ndarray, priors: Priors = Priors()) -> np.ndarray:
    d2, w = second_difference(common)
    scale = priors.smooth_sd_db * 24**1.5
    return d2.T @ (w[:, None] * d2) / scale**2 + 1e-9 * np.eye(common.size)


def soft_median(sources: list[Source], common: np.ndarray, tau_hp: float, robust: bool = True) -> SoftMedian:
    """Minimises sum_p sum_i w_pi rho((R_pi − (A_p m)_i) / scale_pi) + curvature
    penalty, by iteratively reweighted least squares. rho is the pseudo-Huber
    loss with delta = 1: close to a precision-weighted mean when the sources
    agree within their error bars, close to a median when one is far off.
    robust=False: the precision-weighted mean (what's left with two sources)."""
    K = _curvature_penalty(common)
    A = [interp_matrix(common, s.grid) for s in sources]
    scale2 = [s.sd**2 + tau_hp**2 for s in sources]
    w0 = [np.gradient(np.log2(s.grid)) / WEIGHT_STEP_OCT / v for s, v in zip(sources, scale2)]

    def solve(weights):
        H = K.copy()
        b = np.zeros(common.size)
        for Ap, wp, s in zip(A, weights, sources):
            AW = Ap.T * wp
            H += AW @ Ap
            b += AW @ s.R
        return H, np.linalg.solve(H, b)

    weights = w0
    H, m = solve(weights)
    if robust:
        for _ in range(IRLS_ITERS):
            weights = [w / np.sqrt(1.0 + (s.R - Ap @ m) ** 2 / v)
                       for w, s, Ap, v in zip(w0, sources, A, scale2)]
            H, new = solve(weights)
            done = np.max(np.abs(new - m)) < IRLS_TOL_DB
            m = new
            if done:
                break
    Hinv = np.linalg.inv(H)
    G = [(Ap.T * wp).T @ Hinv for Ap, wp in zip(A, weights)]  # n_p x N
    cov = sum(g.T @ s.cov @ g for g, s in zip(G, sources))
    return SoftMedian(common, m, 0.5 * (cov + cov.T), tau_hp, G, robust)


def consensus_of(sources: list[Source]) -> SoftMedian:
    common = common_grid([s.grid for s in sources])
    return soft_median(sources, common, estimate_tau_hp(sources, common), robust=len(sources) > 2)


# -- Feature 1b --------------------------------------------------------------

def raw_consensus(sources: list[Source]) -> SoftMedian:
    """The soft median of raw curves, each with a constant offset fitted by the
    same robust loss: a treble difference in one headphone mustn't shift its
    whole curve, as a mean-based alignment would. The offsets sum to zero, so m
    keeps the group's level. The loss is convex in m and the offsets together,
    so alternating between them converges to the same answer from any start.
    G and cov treat the offsets as fixed; each is averaged over the whole range,
    so its own error is small next to m's at any one frequency."""
    if len(sources) == 1:
        return replace(consensus_of(sources), offsets=np.zeros(1))
    offsets = np.array([s.R.mean() for s in sources])
    offsets -= offsets.mean()
    for _ in range(OFFSET_ITERS):
        sm = consensus_of([replace(s, R=s.R - o) for s, o in zip(sources, offsets)])
        new = np.empty(len(sources))
        for i, s in enumerate(sources):
            r = s.R - sm.on(s.grid)[0]
            v = s.sd**2 + sm.tau_hp**2
            w = np.gradient(np.log2(s.grid)) / v
            if sm.robust:
                w = w / np.sqrt(1.0 + (r - offsets[i]) ** 2 / v)
            new[i] = (w * r).sum() / w.sum()
        new -= new.mean()
        done = np.max(np.abs(new - offsets)) < OFFSET_TOL_DB
        offsets = new
        if done:
            break
    return replace(sm, offsets=offsets)


@dataclass
class EarConsensus:
    """The consensus as a reference for one profile, after any smoothing, on the
    common grid. G is that profile's weights if it's a member (None if not):
    Cov(L, m) = Cov(L) @ G."""
    members: list[str]
    key: str
    grid: np.ndarray
    m: np.ndarray
    cov: np.ndarray
    tau_hp: float
    G: np.ndarray | None = None
    smooth_oct: float = 0.0

    def on(self, grid: np.ndarray) -> np.ndarray:
        """The reference on a grid, its mean removed like every other reference's."""
        e = interp_matrix(self.grid, grid) @ self.m
        return e - e.mean()

    def difference_cov(self, cov_L: np.ndarray, grid: np.ndarray) -> np.ndarray:
        """Cov(L − m). Unlike a fixed reference this one has its own error, and
        a member's own vote makes L and m correlated."""
        out = self.other_cov(cov_L, grid)
        if self.G is not None:
            A = interp_matrix(self.grid, grid)
            cross = cov_L @ self.G @ A.T
            out = out - cross - cross.T
        return out

    def other_cov(self, cov_L: np.ndarray, grid: np.ndarray) -> np.ndarray:
        """Cov(L − m) for another profile's curve, taken as independent of m."""
        A = interp_matrix(self.grid, grid)
        return cov_L + A @ self.cov @ A.T
