"""Consensus across profiles (ADDENDUM.md, Features 1a and 1b): the soft median,
disagreement with it, and the remove-only rule.

Statistics stay on each profile's own grid, because interpolating a fit onto a
finer one adds points but no measurements. The consensus m itself lives on a
common 1/48-octave grid and meets a profile through A_p, the interpolation from
the common grid to that profile's grid.
"""

from dataclasses import dataclass, field

import numpy as np
from scipy.stats import chi2

from flatiron.model.grid import common_grid, interp_matrix, second_difference
from flatiron.model.probit import Priors

MODEL_VERSION = "1"
TAU_HP_FLOOR_DB = 0.5
HETEROGENEITY_P = 0.01
MIN_WIDTH_OCT = 1 / 12
IRLS_ITERS = 100
IRLS_TOL_DB = 1e-4
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

def correction_rule(R, m, mode: str = "remove") -> np.ndarray:
    """The correction for residual R given the consensus m. remove: move R toward
    zero only by what it exceeds the consensus in the same direction, and never
    past zero, so nothing is imported from the others. match: all the way to m."""
    R, m = np.asarray(R, float), np.asarray(m, float)
    if mode == "match":
        return -(R - m)
    return -np.sign(R) * np.maximum(0.0, np.abs(R) - np.maximum(0.0, np.sign(R) * m))


@dataclass
class EarConsensus:
    """The consensus as one member's correction sees it, on its own grid."""
    members: list[str]
    key: str
    m: np.ndarray
    cov_m: np.ndarray
    G: np.ndarray  # this member's weights: Cov(R, m on the common grid) = Cov(R) @ G
    A: np.ndarray  # common grid to this member's grid
    tau_hp: float
    mode: str = "remove"

    def excess_cov(self, cov_R: np.ndarray) -> np.ndarray:
        """Cov(R − A m). This member votes, so R and m are correlated."""
        cross = cov_R @ self.G @ self.A.T
        return cov_R + self.cov_m - cross - cross.T


def for_member(sm: SoftMedian, index: int, grid: np.ndarray, members: list[str], key: str = "",
               mode: str = "remove") -> EarConsensus:
    A = interp_matrix(sm.grid, grid)
    return EarConsensus(members, key, A @ sm.m, A @ sm.cov @ A.T, sm.G[index], A, sm.tau_hp, mode)


# -- Feature 1a --------------------------------------------------------------

@dataclass
class Flag:
    label: str
    ear: str
    f_lo: float
    f_hi: float
    fc: float
    deviation_db: float
    sd_db: float

    @property
    def sign(self) -> str:
        return "peak only here" if self.deviation_db > 0 else "dip only here"

    @property
    def strength(self) -> float:
        return abs(self.deviation_db) / self.sd_db


@dataclass
class Disagreement:
    source: Source
    voting: bool
    deviation: np.ndarray  # from the consensus
    sd: np.ndarray
    flags: list[Flag] = field(default_factory=list)

    @property
    def z(self) -> np.ndarray:
        return self.deviation / self.sd


def _crossing(x, a, k, i, j) -> float:
    """Where |a| crosses k between neighbouring grid points i and j."""
    if a[i] == a[j]:
        return x[i]
    return x[i] + (k - a[i]) / (a[j] - a[i]) * (x[j] - x[i])


def flag_regions(source: Source, ear: str, deviation, sd, k: float = 2.0,
                 min_width_oct: float = MIN_WIDTH_OCT) -> list[Flag]:
    """Regions where |z| > k continuously over at least a width in octaves
    (a count of points would mean something different on each grid). Each edge
    is where |z| crosses k between grid points."""
    x = np.log2(source.grid)
    z = deviation / sd
    out: list[Flag] = []
    n = z.size
    i = 0
    while i < n:
        if abs(z[i]) <= k:
            i += 1
            continue
        j = i
        while j + 1 < n and abs(z[j + 1]) > k and np.sign(z[j + 1]) == np.sign(z[i]):
            j += 1
        a = np.abs(z)
        lo = _crossing(x, a, k, i - 1, i) if i > 0 and np.sign(z[i - 1]) == np.sign(z[i]) else x[i]
        hi = _crossing(x, a, k, j, j + 1) if j + 1 < n and np.sign(z[j + 1]) == np.sign(z[j]) else x[j]
        if hi - lo >= min_width_oct - 1e-9:
            p = i + int(np.argmax(np.abs(deviation[i:j + 1])))
            out.append(Flag(source.label, ear, float(2**lo), float(2**hi), float(source.grid[p]),
                            float(deviation[p]), float(sd[p])))
        i = j + 1
    return out


@dataclass
class EarComparison:
    ear: str
    consensus: SoftMedian
    entries: list[Disagreement]
    heterogeneous: np.ndarray  # on the common grid: Cochran's Q p < 0.01
    voters: int

    @property
    def flags(self) -> list[Flag]:
        return sorted((f for e in self.entries for f in e.flags), key=lambda f: -f.strength)


def compare_ear(ear: str, sources: list[Source], k: float = 2.0, min_width_oct: float = MIN_WIDTH_OCT,
                voting_kinds=KINDS_VOTING) -> EarComparison:
    """Every profile is judged against the whole consensus. A voter's own vote
    is in it, which its deviation's SD allows for, and the soft median keeps one
    profile's peak from moving it far. Leaving each voter out instead fails with
    three profiles: two voters have no majority, so one headphone's peak gets
    the other two flagged as dips."""
    voters = [s for s in sources if s.kind in voting_kinds]
    if len(voters) < 2:
        raise ValueError(f"comparing needs at least two profiles that vote; "
                         f"{len(voters)} of these do (see --include-kind)")
    common = common_grid([s.grid for s in sources])
    tau = estimate_tau_hp(voters, common)
    robust = len(voters) > 2
    full = soft_median(voters, common, tau, robust)
    entries = []
    for s in sources:
        voting = any(s is o for o in voters)
        m, cov_m = full.on(s.grid)
        dev = s.R - m
        if voting:
            i = next(n for n, o in enumerate(voters) if o is s)
            cross = s.cov @ full.G[i] @ interp_matrix(common, s.grid).T
            var = np.diag(s.cov + cov_m - cross - cross.T)
        else:
            var = np.diag(s.cov + cov_m)
        sd = np.sqrt(np.clip(var, 1e-12, None))
        entries.append(Disagreement(s, voting, dev, sd, flag_regions(s, ear, dev, sd, k, min_width_oct)))
    _, p = heterogeneity(voters, common)
    return EarComparison(ear, full, entries, p < HETEROGENEITY_P, len(voters))
