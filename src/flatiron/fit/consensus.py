"""Consensus across profiles (DESIGN.md, "The soft median" and "Consensus reference"): the soft median,
how much profiles disagree, the consensus of raw curves, and the consensus
reference: each member split into broad and narrow parts, and the shared
narrow part kept only as far as it's believed to be the listener's.

Statistics stay on each profile's own grid, because interpolating a fit onto a
finer one adds points but no measurements. The consensus m itself lives on a
common 1/48-octave grid and meets a profile through A_p, the interpolation from
the common grid to that profile's grid.
"""

from dataclasses import dataclass, field, replace

import numpy as np
from scipy.stats import chi2

from flatiron.model.grid import common_grid, interp_matrix, local_linear_smoother, second_difference, smoother_matrix
from flatiron.model.probit import Priors

MODEL_VERSION = "3"  # 3: split into broad and narrow parts, speakers as gates; 2: raw curves with robust offsets
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
# Kinds that can be listed in a consensus without voting: they only decide how much of
# the headphones' shared narrow structure is kept.
KINDS_GATING = ("speaker",)


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


# -- the consensus reference ---------------------------------------------------

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


# -- the consensus reference: broad + k · narrow (DESIGN.md, "Consensus reference") --

SPLIT_OCT = 0.75  # each member's local-linear "broad" part (FWHM); the rest is "narrow"
# Prior SD of the listener's own narrow structure, a(f): flat below the first
# frequency and above the second, log-linear between. Narrow pinna structure
# lives above about 4 kHz; below, the ear's response is broad.
PRIOR_DB = ((3000.0, 0.5), (6000.0, 3.0))
DISAGREEMENT_SMOOTH_OCT = 1 / 4
COLOUR_WIDTH_OCT = 1 / 12  # a speaker's own narrow colouration: its correlation width (FWHM)
SPEAKER_COLOUR_DB = 1.0  # its size, unless set, or estimated from two or more speakers
COLOUR_RANGE_DB = (0.25, 6.0)  # an estimate is held within this
GAIN_SD_RANGE = (0.1, 1.0)  # the same for the estimated SD of the headphones-only share
GAIN_SD_DEFAULT = 0.3  # only used if it can't be estimated (no point inside the range)


def listener_prior_db(f) -> np.ndarray:
    (f0, a0), (f1, a1) = PRIOR_DB
    t = np.clip(np.log(np.asarray(f, float) / f0) / np.log(f1 / f0), 0, 1)
    return a0 * (a1 / a0) ** t


def split_parts(s: Source) -> tuple[np.ndarray, Source, Source]:
    """A source's smoother S, its broad part S R and its narrow part (I − S) R,
    each with its covariance, on its own grid."""
    S = local_linear_smoother(s.grid, SPLIT_OCT, np.gradient(np.log2(s.grid)))
    N = np.eye(s.grid.size) - S
    return S, replace(s, R=S @ s.R, cov=S @ s.cov @ S.T), replace(s, R=N @ s.R, cov=N @ s.cov @ N.T)


@dataclass
class SplitConsensus:
    """One ear's consensus before any setting applies (what's cached).
    broad: the soft median of the members' broad parts, with their offsets.
    narrow: the soft median of their narrow parts.
    cross: Cov(narrow, broad) on the common grid.
    excess: how much the narrow parts disagree beyond their own noise, as a
    variance of the narrow median (smoothed).
    speakers: each listed speaker's narrow part, on the common grid."""
    members: list[str]
    broad: SoftMedian
    narrow: SoftMedian
    cross: np.ndarray
    excess: np.ndarray
    speakers: list[Source] = field(default_factory=list)

    @property
    def grid(self) -> np.ndarray:
        return self.narrow.grid

    def arrays(self) -> dict[str, np.ndarray]:
        out = dict(members=np.array(self.members), speakers=np.array([s.label for s in self.speakers]),
                   cross=self.cross, excess=self.excess)
        for name, sm in (("broad", self.broad), ("narrow", self.narrow)):
            out.update({f"{name}_grid": sm.grid, f"{name}_m": sm.m, f"{name}_cov": sm.cov,
                        f"{name}_tau": sm.tau_hp, f"{name}_robust": sm.robust,
                        **{f"{name}_G{i}": g for i, g in enumerate(sm.G)}})
        out["broad_offsets"] = self.broad.offsets
        for j, s in enumerate(self.speakers):
            out[f"spk_R{j}"], out[f"spk_cov{j}"] = s.R, s.cov
        return out

    @classmethod
    def from_arrays(cls, z) -> "SplitConsensus":
        members = [str(x) for x in z["members"]]

        def sm(name):
            return SoftMedian(z[f"{name}_grid"], z[f"{name}_m"], z[f"{name}_cov"], float(z[f"{name}_tau"]),
                              [z[f"{name}_G{i}"] for i in range(len(members))], bool(z[f"{name}_robust"]),
                              z["broad_offsets"] if name == "broad" else None)

        narrow = sm("narrow")
        speakers = [Source(str(label), narrow.grid, z[f"spk_R{j}"], z[f"spk_cov{j}"], "speaker")
                    for j, label in enumerate(z["speakers"])]
        return cls(members, sm("broad"), narrow, z["cross"], z["excess"], speakers)


def _disagreement(narrow: list[Source], sm: SoftMedian) -> np.ndarray:
    """The narrow median's variance from the members' disagreement beyond their
    own noise: a sandwich estimate with the soft median's final weights, so a
    member far from the other two counts for little, as it does in the median.
    Smoothed, since three points per frequency say little on their own."""
    n = len(narrow)
    if n < 2:
        return np.zeros(sm.grid.size)
    R, V = [], []
    for s in narrow:
        A = interp_matrix(s.grid, sm.grid)
        R.append(A @ s.R)
        V.append(np.einsum("ij,jk,ik->i", A, s.cov, A))
    R, V = np.array(R), np.array(V)
    r = R - sm.m
    scale2 = V + sm.tau_hp**2
    w = 1 / scale2 / (np.sqrt(1 + r**2 / scale2) if sm.robust else 1)
    share = w / w.sum(axis=0)
    raw = (share**2 * np.maximum(r**2 - V, 0)).sum(axis=0) * n / (n - 1)
    return smoother_matrix(sm.grid, DISAGREEMENT_SMOOTH_OCT, np.gradient(np.log2(sm.grid))) @ raw


def split_consensus(headphones: list[Source], speakers: list[Source] = ()) -> SplitConsensus:
    """Split each headphone into broad and narrow parts, then take the soft
    median of each separately: broad tonal differences between headphones then
    can't decide which curve the narrow median follows, or read as narrow
    disagreement. Speakers don't vote; their narrow parts are kept for the share."""
    parts = [split_parts(s) for s in headphones]
    broad = raw_consensus([p[1] for p in parts])
    narrow = consensus_of([p[2] for p in parts])
    common = narrow.grid
    cross = np.zeros((common.size, common.size))
    for (S, _, _), s, gB, gN in zip(parts, headphones, broad.G, narrow.G):
        cross += gN.T @ ((np.eye(S.shape[0]) - S) @ s.cov @ S.T) @ gB
    spk = []
    for s in speakers:
        _, _, n = split_parts(s)
        A = interp_matrix(s.grid, common)
        spk.append(Source(s.label, common, A @ n.R, A @ n.cov @ A.T, "speaker"))
    return SplitConsensus([s.label for s in headphones], broad, narrow, cross, _disagreement([p[2] for p in parts], narrow),
                          spk)


def _colour_cov(grid: np.ndarray, colour_db: float) -> np.ndarray:
    x = np.log2(grid)
    return colour_db**2 * np.exp(-0.5 * ((x[:, None] - x[None, :]) / (COLOUR_WIDTH_OCT / 2.3548)) ** 2)


def speaker_mean(speakers: list[Source], colour_db: float) -> tuple[np.ndarray, np.ndarray]:
    """The speakers' narrow parts averaged point by point, each weighted by its
    noise plus its own colouration, with the covariance of that average. Two
    unrelated speakers halve the colouration's variance."""
    C = _colour_cov(speakers[0].grid, colour_db)
    covs = [s.cov + C for s in speakers]
    w = np.array([1 / np.clip(np.diag(c), 1e-12, None) for c in covs])
    a = w / w.sum(axis=0)
    return (a * np.array([s.R for s in speakers])).sum(axis=0), sum(ai[:, None] * c * ai[None, :] for ai, c in zip(a, covs))


def speaker_gain(n: np.ndarray, cov_n: np.ndarray, s: np.ndarray, cov_s: np.ndarray, grid: np.ndarray,
                 window_oct: float) -> tuple[np.ndarray, np.ndarray]:
    """g: the regression slope of the speaker's narrow part on the headphones',
    in a Gaussian window (FWHM window_oct) at each point, and its SD. If the
    speaker were the listener plus independent noise, g would be the share of
    the headphones' narrow structure that is the listener's, coupling included.
    The SD counts the speaker's error (colouration included in cov_s) and the
    headphones' own, to first order."""
    x = np.log2(grid)
    W = np.exp(-0.5 * ((x[:, None] - x[None, :]) / (window_oct / 2.3548)) ** 2)
    C = W * n[None, :]
    d = np.maximum(C @ n, 1e-12)
    g = (C @ s) / d
    var_s = np.einsum("ij,jk,ik->i", C, cov_s, C) / d**2
    Dn = W * (s[None, :] - 2 * g[:, None] * n[None, :]) / d[:, None]
    var_n = np.einsum("ij,jk,ik->i", Dn, cov_n, Dn)
    return g, np.sqrt(var_s + var_n)


def estimate_colour(splits: list[SplitConsensus], f_lo: float, f_hi: float) -> float | None:
    """A speaker's own narrow colouration, from how much two speakers' narrow
    parts differ beyond their noise (each pair's difference holds two
    colourations), pooled over pairs and ears. None with fewer than two speakers."""
    num = den = 0.0
    for sc in splits:
        inside = (sc.grid >= f_lo) & (sc.grid <= f_hi)
        w = np.gradient(np.log2(sc.grid))[inside]
        sp = sc.speakers
        for i in range(len(sp)):
            for j in range(i + 1, len(sp)):
                d2 = (sp[i].R - sp[j].R)[inside] ** 2
                v = np.diag(sp[i].cov)[inside] + np.diag(sp[j].cov)[inside]
                num += float(np.sum(w * (d2 - v))) / 2
                den += float(np.sum(w))
    if den == 0:
        return None
    return float(np.clip(np.sqrt(max(num / den, 0.0)), *COLOUR_RANGE_DB))


def estimate_gain_sd(points: list[tuple[float, float, float]]) -> float | None:
    """The SD of the headphones-only share as an estimate of the true one, from
    how far the speaker's gain strays from it beyond the gain's own SD:
    (g clipped to [0, 1], SD of g, k_hp) at points about a window apart, a
    moment estimate weighted iteratively (Paule–Mandel style)."""
    if not points:
        return None
    g, se, k = (np.array(v) for v in zip(*points))
    d2 = (np.clip(g, 0, 1) - k) ** 2
    s2 = 0.0
    for _ in range(200):
        w = 1 / (se**2 + s2)
        new = max(float(np.sum(w**2 * (d2 - se**2)) / np.sum(w**2)), 0.0)
        if abs(new - s2) < 1e-8:
            break
        s2 = new
    return float(np.clip(np.sqrt(s2), *GAIN_SD_RANGE))


@dataclass
class EarShare:
    """How much of the narrow median is kept, k(f), and what it came from."""
    k: np.ndarray
    k_hp: np.ndarray  # headphones only: a² / (a² + coupling² + v)
    v: np.ndarray  # the narrow median's error variance: its own noise plus the disagreement
    g: np.ndarray | None = None  # the speakers' gain, and its SD
    g_sd: np.ndarray | None = None
    speaker: np.ndarray | None = None  # the speakers' (averaged) narrow part


@dataclass
class Shares:
    ears: dict[str, EarShare]
    speakers: list[str]
    colour_db: float | None = None  # what the speakers' colouration was taken to be, and how
    colour_how: str = ""
    gain_sd: float | None = None
    gain_sd_how: str = ""


def shares(splits: dict[str, SplitConsensus], prior_scale: float = 1.0, coupling_db: float = 1.0,
           window_oct: float = 0.3, colour_db: float | None = None, gain_sd: float | None = None,
           f_lo: float = 1000.0, f_hi: float = 13000.0) -> Shares:
    """k(f) per ear. Headphones only: k_hp = a² / (a² + coupling² + v), the
    share of the narrow median that's the listener's if their own structure has
    SD a (prior_scale × a(f)), structure every cup adds has SD coupling_db, and
    the median's error has variance v. With speakers listed, k is the
    precision-weighted mean of k_hp (SD gain_sd) and the speakers' gain g
    clipped to [0, 1], which is an estimate of the same share. None for
    colour_db or gain_sd: estimated from the data where possible (both ears
    pooled), else the default."""
    names = sorted({s.label for sc in splits.values() for s in sc.speakers})
    out = Shares({}, names)
    if names:
        if colour_db is not None:
            out.colour_db, out.colour_how = colour_db, "set"
        else:
            est = estimate_colour(list(splits.values()), f_lo, f_hi)
            out.colour_db, out.colour_how = ((est, f"estimated from {len(names)} speakers") if est is not None
                                             else (SPEAKER_COLOUR_DB, "the default"))
    points = []
    for e, sc in splits.items():
        f = sc.grid
        v = np.diag(sc.narrow.cov) + sc.excess
        a2 = (prior_scale * listener_prior_db(f)) ** 2
        k_hp = a2 / np.maximum(a2 + coupling_db**2 + v, 1e-12)
        share = EarShare(k_hp, k_hp, v)
        if sc.speakers:
            s, cov_s = speaker_mean(sc.speakers, out.colour_db)
            g, g_sd = speaker_gain(sc.narrow.m, sc.narrow.cov, s, cov_s, f, window_oct)
            share.g, share.g_sd, share.speaker = g, g_sd, s
            x = np.log2(f)
            for p in np.arange(np.log2(f_lo), np.log2(f_hi) + 1e-9, window_oct):
                if x[0] <= p <= x[-1]:
                    i = int(np.argmin(np.abs(x - p)))
                    points.append((g[i], g_sd[i], k_hp[i]))
        out.ears[e] = share
    if names:
        if gain_sd is not None:
            out.gain_sd, out.gain_sd_how = gain_sd, "set"
        else:
            est = estimate_gain_sd(points)
            out.gain_sd, out.gain_sd_how = ((est, "estimated from the speakers") if est is not None
                                            else (GAIN_SD_DEFAULT, "the default"))
        for share in out.ears.values():
            wk, wg = 1 / max(out.gain_sd, 1e-6) ** 2, 1 / np.maximum(share.g_sd, 1e-6) ** 2
            share.k = (wk * share.k_hp + wg * np.clip(share.g, 0, 1)) / (wk + wg)
    return out


@dataclass
class EarConsensus:
    """The consensus as a reference for one profile, on the common grid:
    E = broad + k · narrow. G is that profile's weights if it's a member (None
    if not): Cov(L, E) = Cov(L) @ G, with the weights and k held fixed."""
    members: list[str]
    key: str
    grid: np.ndarray
    m: np.ndarray
    cov: np.ndarray
    tau_hp: float  # the narrow parts' between-headphone spread
    G: np.ndarray | None = None
    broad: np.ndarray | None = None
    narrow: np.ndarray | None = None
    share: EarShare | None = None
    shares: Shares | None = None

    def on(self, grid: np.ndarray) -> np.ndarray:
        """The reference on a grid, its mean removed like every other reference's."""
        e = interp_matrix(self.grid, grid) @ self.m
        return e - e.mean()

    def difference_cov(self, cov_L: np.ndarray, grid: np.ndarray) -> np.ndarray:
        """Cov(L − E). Unlike a fixed reference this one has its own error, and
        a member's own vote makes L and E correlated."""
        out = self.other_cov(cov_L, grid)
        if self.G is not None:
            A = interp_matrix(self.grid, grid)
            cross = cov_L @ self.G @ A.T
            out = out - cross - cross.T
        return out

    def other_cov(self, cov_L: np.ndarray, grid: np.ndarray) -> np.ndarray:
        """Cov(L − E) for another profile's curve, taken as independent of E."""
        A = interp_matrix(self.grid, grid)
        return cov_L + A @ self.cov @ A.T


def ear_reference(sc: SplitConsensus, share: EarShare, key: str = "", own_grid: np.ndarray | None = None,
                  own: int | None = None, all_shares: Shares | None = None,
                  broad: tuple[np.ndarray, np.ndarray] | None = None) -> EarConsensus:
    """E = broad + k · narrow for one profile, with its covariance, and that
    profile's weights if it's a member (own: its index among the headphones,
    own_grid: its grid). broad: (curve, covariance) on the consensus grid in
    place of the members' broad median (the threshold-anchored diffuse
    field's broad part; DESIGN.md, "Threshold-anchored reference")."""
    k = share.k
    if broad is None:
        b_m, b_cov, cross = sc.broad.m, sc.broad.cov, sc.cross
    else:
        (b_m, b_cov), cross = broad, np.zeros_like(sc.cross)
    cov = b_cov + k[:, None] * sc.narrow.cov * k[None, :] + k[:, None] * cross + cross.T * k[None, :]
    G = None
    if own is not None:
        S = local_linear_smoother(own_grid, SPLIT_OCT, np.gradient(np.log2(own_grid)))
        G = ((np.eye(S.shape[0]) - S).T @ sc.narrow.G[own]) * k[None, :]
        if broad is None:
            G = G + S.T @ sc.broad.G[own]
    return EarConsensus(sc.members, key, sc.grid, b_m + k * sc.narrow.m, 0.5 * (cov + cov.T),
                        sc.narrow.tau_hp, G, b_m, sc.narrow.m, share, all_shares)


def anchored_broad(grid: np.ndarray, anchor) -> tuple[np.ndarray, np.ndarray]:
    """The threshold-anchored diffuse field's broad part on the grid (split as
    the members are), and its covariance from the gap's."""
    from flatiron.fit.reference import anchored

    S = local_linear_smoother(grid, SPLIT_OCT, np.gradient(np.log2(grid)))
    return S @ anchored("diffuse", grid, anchor), S @ anchor.cov_on(grid) @ S.T
