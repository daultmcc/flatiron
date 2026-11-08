"""What the perceptual model is about: trials, priors, the fit, and each trial's
row of the design matrix (DESIGN.md, "Perceptual model"). The fit itself is in
model/hierarchical.py.
"""

from dataclasses import dataclass, field

import numpy as np
from scipy import sparse

from flatiron.audio.stimulus import POINT, spectrum_offsets_oct
from flatiron.model.grid import interp_matrix, octaves

# Listener noise rises in the treble: a ramp in log frequency between these,
# from sigma0 below to sigma0 * treble_noise above (DESIGN.md, "Perceptual model").
TREBLE_NOISE_LO_HZ, TREBLE_NOISE_HI_HZ = 6000.0, 10000.0


def noise_sd(f_a, f_b, sigma0: float, d0: float, treble_noise: float = 1.0) -> np.ndarray:
    """The trial's noise: each tone brings its own variance, and the pair's gap adds to it."""
    def ramp(f):
        return np.clip(np.log2(np.asarray(f, float) / TREBLE_NOISE_LO_HZ) / np.log2(TREBLE_NOISE_HI_HZ / TREBLE_NOISE_LO_HZ), 0.0, 1.0)
    tones = np.sqrt(0.5 * (treble_noise ** (2 * ramp(f_a)) + treble_noise ** (2 * ramp(f_b))))
    return sigma0 * tones * np.sqrt(1.0 + (octaves(f_a, f_b) / d0) ** 2)


@dataclass
class Trials:
    """Within-ear judgments. `a_louder` is the listener's answer for stimulus A;
    `order` is +1 if A played first, −1 if B did, 0 if unknown. `group` says
    which seating each trial belongs to (0, 1, ...: one per session)."""
    f_a: np.ndarray
    f_b: np.ndarray
    offset: np.ndarray
    a_louder: np.ndarray
    order: np.ndarray | None = None
    stimulus: np.ndarray | None = None  # per-trial stimulus key (stimulus.stimulus_key); None: points
    group: np.ndarray | None = None

    def __post_init__(self):
        self.f_a = np.asarray(self.f_a, float)
        self.f_b = np.asarray(self.f_b, float)
        self.offset = np.asarray(self.offset, float)
        self.a_louder = np.asarray(self.a_louder, bool)
        self.order = (np.zeros(self.f_a.size) if self.order is None
                      else np.asarray(self.order, float))
        self.stimulus = (np.full(self.f_a.size, POINT, dtype=object) if self.stimulus is None
                         else np.asarray(self.stimulus, dtype=object))
        self.group = (np.zeros(self.f_a.size, dtype=int) if self.group is None
                      else np.asarray(self.group, dtype=int))

    def __len__(self):
        return self.f_a.size

    @classmethod
    def concat(cls, parts: list["Trials"]) -> "Trials":
        return cls(*(np.concatenate([getattr(p, k) for p in parts])
                     for k in ("f_a", "f_b", "offset", "a_louder", "order", "stimulus", "group")))

    @classmethod
    def grouped(cls, parts: list["Trials"]) -> "Trials":
        """One seating per part, in order."""
        for g, p in enumerate(parts):
            p.group = np.full(len(p), g, dtype=int)
        return cls.concat(parts)


@dataclass(frozen=True)
class Priors:
    # SD of the second difference of L at 1/24-octave spacing (dB). Expressed
    # internally as a grid-independent curvature penalty.
    smooth_sd_db: float = 1.0
    # Very weak prior on each level, so the fit is well-posed with few or no
    # trials (the smoothness prior alone leaves a linear trend unconstrained).
    level_sd_db: float = 100.0
    sigma0: float = 1.0
    sigma0_log_sd: float = 0.5
    d0: float = 0.5
    d0_log_sd: float = 0.5
    lapse: float = 0.02
    lapse_logit_sd: float = 1.0
    treble_noise_log_sd: float = 0.5  # noise above 10 kHz relative to below 6 kHz, centred on no difference
    bias_sd_db: float = 3.0  # time-order error prior, centred on none
    # Seating spread (DESIGN.md, "Perceptual model"): medians below 5 kHz and above 8 kHz.
    tau_lo: float = 0.5
    tau_hi: float = 1.5
    tau_log_sd: float = 0.7
    ell_oct: float = 1 / 12  # correlation width of a seating's deviation


@dataclass(frozen=True)
class BalancePrior:
    """The difference between the ears' curves (DESIGN.md, "Channel balance"):
    SD below 5 kHz and above 8 kHz (log-interpolated between), and its
    correlation width. Sized from the real profiles: a rough empirical-Bayes
    estimate over them gave about 1 to 1.5 and 2 to 4 dB."""
    lo_db: float = 1.5
    hi_db: float = 3.0
    ell_oct: float = 1 / 8


@dataclass
class ProbitFit:
    """L is the average over seatings (L_avg); each group's curve is L + u[g]."""
    grid: np.ndarray
    L: np.ndarray
    cov: np.ndarray  # covariance of L under the mean(L) = 0 gauge
    sigma0: float
    d0: float
    lapse: float
    n_trials: int
    neg_log_post: float
    converged: bool
    bias_db: float = 0.0  # time-order error: the first interval sounds this much louder
    bias_sd: float = 0.0
    treble_noise: float = 1.0  # noise above 10 kHz as a multiple of sigma0
    tau_lo: float = 0.5  # seating spread below 5 kHz / above 8 kHz (dB)
    tau_hi: float = 1.5
    tau_fitted: bool = False  # False: at the prior or held fixed
    tau: np.ndarray | None = None  # the spread at each grid point
    u: np.ndarray | None = None  # groups x points: each group's deviation (posterior mean)
    v: np.ndarray | None = None  # the same in the whitened basis (a warm start for the next fit)
    group_trials: np.ndarray | None = None  # trials per group
    group_cov: np.ndarray | None = None  # groups x points x points: Cov(L + u_g)
    group_u_cov: np.ndarray | None = None  # groups x points x points: Cov(u_g)
    focus_cov: np.ndarray | None = None  # Cov(L + u_f) for the group the fit was focused on (the last)
    focus_cross: np.ndarray | None = None  # Cov(L, L + u_f)
    groups: list = field(default_factory=list)  # what each group is (session ids), when known

    @property
    def sd(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.cov), 0, None))

    def var_diff(self, i, j):
        c = self.cov
        return c[i, i] + c[j, j] - 2 * c[i, j]

    def at(self, freqs) -> tuple[np.ndarray, np.ndarray]:
        """Interpolated L and its SD at arbitrary frequencies."""
        m = interp_matrix(self.grid, freqs)
        return m @ self.L, np.sqrt(np.einsum("ij,jk,ik->i", m, self.cov, m))

    @property
    def n_groups(self) -> int:
        """Groups with trials in them."""
        return 0 if self.group_trials is None else int(np.count_nonzero(self.group_trials))


MODEL_SPECTRUM_POINTS = 11  # samples across each stimulus band (grid steps are wider)


def band_rows(grid: np.ndarray, freqs, stimulus) -> sparse.csr_matrix:
    """Sparse rows mapping L to each trial's level as heard through its
    stimulus: interpolation averaged over the stimulus spectrum (in dB, a linear
    approximation to the power average; close for features of a few dB)."""
    f = np.asarray(freqs, float)
    x = np.log2(grid)
    keys = np.asarray(stimulus, dtype=object)
    rows, cols, vals = [], [], []
    for key in set(keys):
        idx = np.flatnonzero(keys == key)
        offs = spectrum_offsets_oct(key, MODEL_SPECTRUM_POINTS)
        q = np.clip(np.log2(f[idx])[:, None] + np.asarray(offs)[None, :], x[0], x[-1])
        hi = np.clip(np.searchsorted(x, q, side="right"), 1, x.size - 1)
        lo = hi - 1
        w = (q - x[lo]) / (x[hi] - x[lo])
        r = np.repeat(idx, len(offs))
        rows += [r, r]
        cols += [lo.ravel(), hi.ravel()]
        vals += [((1.0 - w) / len(offs)).ravel(), (w / len(offs)).ravel()]
    if not rows:
        return sparse.csr_matrix((f.size, grid.size))
    return sparse.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                             shape=(f.size, grid.size))


def band_matrix(grid: np.ndarray, freqs, stimulus) -> np.ndarray:
    return band_rows(grid, freqs, stimulus).toarray()
