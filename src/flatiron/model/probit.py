"""Probit MAP fit of perceived level per ear (DESIGN.md, "Perceptual model").

P(A) = lapse/2 + (1 - lapse) * Phi((L_a - L_b + off + bias * order) / sigma(d)),
sigma(d) = sigma0 * sqrt(1 + (d/d0)^2), d = |log2(f_a/f_b)|,
where `off` is every known level difference between A and B (Δ_a − Δ_b plus
any active or preview EQ gain), L_a and L_b are L averaged (in dB) over each
trial's stimulus spectrum (a point for tones; the band for noise), order is +1
when A played first and −1 when B did, and bias is the time-order error: how many dB louder the first interval
sounds. The bias is fitted with L (a fixed effect), so its uncertainty is part
of L's error bars.
"""

from dataclasses import dataclass, field

import numpy as np
from scipy import optimize
from scipy.special import expit, logit, ndtr

from flatiron.audio.stimulus import POINT, spectrum_offsets_oct
from flatiron.model.grid import interp_matrix, octaves, second_difference

SQRT_2PI = np.sqrt(2 * np.pi)


@dataclass
class Trials:
    """Within-ear judgments. `a_louder` is the listener's answer for stimulus A;
    `order` is +1 if A played first, −1 if B did, 0 if unknown."""
    f_a: np.ndarray
    f_b: np.ndarray
    offset: np.ndarray
    a_louder: np.ndarray
    order: np.ndarray | None = None
    stimulus: np.ndarray | None = None  # per-trial stimulus key (stimulus.stimulus_key); None: points

    def __post_init__(self):
        self.f_a = np.asarray(self.f_a, float)
        self.f_b = np.asarray(self.f_b, float)
        self.offset = np.asarray(self.offset, float)
        self.a_louder = np.asarray(self.a_louder, bool)
        self.order = (np.zeros(self.f_a.size) if self.order is None
                      else np.asarray(self.order, float))
        self.stimulus = (np.full(self.f_a.size, POINT, dtype=object) if self.stimulus is None
                         else np.asarray(self.stimulus, dtype=object))

    def __len__(self):
        return self.f_a.size

    @classmethod
    def concat(cls, parts: list["Trials"]) -> "Trials":
        return cls(*(np.concatenate([getattr(p, k) for p in parts])
                     for k in ("f_a", "f_b", "offset", "a_louder", "order", "stimulus")))


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
    bias_sd_db: float = 3.0  # time-order error prior, centred on none


# Box bounds on the hyperparameters, in their unconstrained coordinates.
LOG_SIGMA0_BOUNDS = (np.log(0.1), np.log(10.0))
LOG_D0_BOUNDS = (np.log(0.05), np.log(4.0))
LOGIT_LAPSE_BOUNDS = (logit(1e-4), logit(0.3))
GAUGE_SD = 0.01  # dB, soft gauge mean(L) = 0 used during optimisation
# With fewer trials than this the evidence's spurious small-sigma0 basin wins,
# so the noise parameters stay at the prior (or the warm start).
MIN_TRIALS_NOISE_FIT = 200


@dataclass
class ProbitFit:
    grid: np.ndarray
    L: np.ndarray
    cov: np.ndarray  # covariance of L under the mean(L) = 0 gauge
    sigma0: float
    d0: float
    lapse: float
    n_trials: int
    neg_log_post: float
    converged: bool
    hyper_sd: dict = field(default_factory=dict)
    bias_db: float = 0.0  # time-order error: the first interval sounds this much louder
    bias_sd: float = 0.0

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


MODEL_SPECTRUM_POINTS = 11  # samples across each stimulus band (grid steps are wider)


def band_matrix(grid: np.ndarray, freqs, stimulus) -> np.ndarray:
    """Rows mapping L to each trial's level as heard through its stimulus:
    interpolation averaged over the stimulus spectrum (in dB, a linear
    approximation to the power average; close for features of a few dB)."""
    f = np.asarray(freqs, float)
    out = np.zeros((f.size, grid.size))
    keys = np.asarray(stimulus, dtype=object)
    for key in set(keys):
        rows = keys == key
        offs = spectrum_offsets_oct(key, MODEL_SPECTRUM_POINTS)
        out[rows] = np.mean([interp_matrix(grid, f[rows] * 2.0**o) for o in offs], axis=0)
    return out


class _Problem:
    def __init__(self, trials: Trials, grid: np.ndarray, priors: Priors):
        self.grid = grid
        self.n = grid.size
        self.A = (band_matrix(grid, trials.f_a, trials.stimulus)
                  - band_matrix(grid, trials.f_b, trials.stimulus))
        self.off = trials.offset
        self.order = trials.order
        self.m = self.n + 1  # inner parameters: L and the bias
        self.y = trials.a_louder.astype(float)
        self.d = octaves(trials.f_a, trials.f_b)
        self.p = priors
        d2, w = second_difference(grid)
        tau = priors.smooth_sd_db * 24**1.5
        self.K = d2.T @ (w[:, None] * d2) / tau**2 + np.eye(self.n) / priors.level_sd_db**2
        self.mu = np.array([np.log(priors.sigma0), np.log(priors.d0), logit(priors.lapse)])
        self.prec = 1.0 / np.array([priors.sigma0_log_sd, priors.d0_log_sd,
                                    priors.lapse_logit_sd]) ** 2

    def unpack(self, theta):
        n, m = self.n, self.m
        return theta[:n], theta[n], np.exp(theta[m]), np.exp(theta[m + 1]), expit(theta[m + 2])

    def _forward(self, theta):
        L, bias, s0, d0, lam = self.unpack(theta)
        n, m = self.n, self.m
        s = np.sqrt(1.0 + (self.d / d0) ** 2)
        sigma = s0 * s
        z = (self.A @ L + bias * self.order + self.off) / sigma
        phi = np.exp(-0.5 * z * z) / SQRT_2PI
        p = lam / 2 + (1 - lam) * ndtr(z)
        # dp/dtheta, one row per trial
        dpdz = (1 - lam) * phi
        J = np.empty((z.size, m + 3))
        J[:, :n] = (dpdz / sigma)[:, None] * self.A
        J[:, n] = dpdz / sigma * self.order
        J[:, m] = -dpdz * z
        J[:, m + 1] = dpdz * z * (self.d / d0) ** 2 / s**2
        J[:, m + 2] = (0.5 - ndtr(z)) * lam * (1 - lam)
        return L, p, J

    def prior_hessian(self):
        n, m = self.n, self.m
        H = np.zeros((m + 3, m + 3))
        H[:n, :n] = self.K + 1.0 / (n**2 * GAUGE_SD**2)
        H[n, n] = 1.0 / self.p.bias_sd_db**2
        H[m:, m:] = np.diag(self.prec)
        return H

    def objective(self, theta):
        L, p, J = self._forward(theta)
        n, m = self.n, self.m
        bias = theta[n]
        y = self.y
        nll = -np.sum(y * np.log(p) + (1 - y) * np.log1p(-p))
        g = J.T @ (-(y / p - (1 - y) / (1 - p)))
        h = theta[m:] - self.mu
        mean = L.mean()
        prior = (0.5 * L @ self.K @ L + 0.5 * mean**2 / GAUGE_SD**2
                 + 0.5 * bias**2 / self.p.bias_sd_db**2 + 0.5 * np.sum(self.prec * h * h))
        g[:n] += self.K @ L + mean / (n * GAUGE_SD**2)
        g[n] += bias / self.p.bias_sd_db**2
        g[m:] += self.prec * h
        return nll + prior, g

    def fisher(self, theta):
        _, p, J = self._forward(theta)
        return J.T @ (J / (p * (1 - p))[:, None]) + self.prior_hessian()


def _fit_L(prob: _Problem, h: np.ndarray, u0: np.ndarray):
    """MAP of the inner parameters u = (L, bias) with hyperparameters h fixed.
    Returns (u, neg log posterior, success)."""
    m = prob.m

    def f(u):
        val, g = prob.objective(np.concatenate([u, h]))
        return val, g[:m]

    res = optimize.minimize(f, u0, jac=True, method="L-BFGS-B",
                            options={"maxiter": 5000, "maxfun": 20000, "ftol": 1e-12, "gtol": 1e-7})
    return res.x, res.fun, res.success


def _neg_log_evidence(prob: _Problem, h: np.ndarray, L0: np.ndarray):
    """Laplace approximation to -log p(trials | h) - log p(h), up to a constant."""
    u, val, ok = _fit_L(prob, h, L0)
    H = prob.fisher(np.concatenate([u, h]))[:prob.m, :prob.m]
    _, logdet = np.linalg.slogdet(H)
    return val + 0.5 * logdet, u, ok


def _simplex(start, lo, hi, step=0.3):
    pts = [start]
    for k in range(start.size):
        p = start.copy()
        p[k] = p[k] + step if p[k] + step <= hi[k] else p[k] - step
        pts.append(p)
    return np.array(pts)


def fit_probit(trials: Trials, grid: np.ndarray, priors: Priors = Priors(),
               init: ProbitFit | None = None, fit_hyper: bool = True) -> ProbitFit:
    """Fit perceived level L with Laplace error bars.

    The noise hyperparameters (sigma0, d0, lapse) are set by maximising the
    Laplace-approximated marginal likelihood with L integrated out. A joint MAP
    over L and the hyperparameters overfits: with a few trials per grid point L
    can separate the answers and sigma0 collapses. L is then the MAP given those
    hyperparameters. With fit_hyper=False the hyperparameters stay at `init`
    (or the prior means), which is the fast path for trial-by-trial refits.
    """
    prob = _Problem(trials, np.asarray(grid, float), priors)
    n = prob.n
    lo = np.array([LOG_SIGMA0_BOUNDS[0], LOG_D0_BOUNDS[0], LOGIT_LAPSE_BOUNDS[0]])
    hi = np.array([LOG_SIGMA0_BOUNDS[1], LOG_D0_BOUNDS[1], LOGIT_LAPSE_BOUNDS[1]])
    L0 = np.zeros(prob.m)  # (L, bias)
    h0 = prob.mu.copy()
    if init is not None:
        L0 = np.append(init.at(prob.grid)[0], init.bias_db)
        h0 = np.array([np.log(init.sigma0), np.log(init.d0), logit(init.lapse)])
    h0 = np.clip(h0, lo, hi)

    converged = True
    if fit_hyper and len(trials) < MIN_TRIALS_NOISE_FIT:
        fit_hyper = False  # too few trials: the evidence underestimates sigma0
    if fit_hyper:
        # Search from the prior means and from the warm start, and keep the
        # better optimum: the Laplace evidence has a spurious basin at small
        # sigma0 / large lapse (few trials near threshold, so the information
        # term vanishes), and a warm start from an early, noisy estimate can
        # stay trapped in it.
        best = None
        for start in {tuple(prob.mu.clip(lo, hi)), tuple(h0)}:
            start = np.array(start)
            # Every evaluation starts its inner fit from the same point (the
            # fit at the search's start), so the evidence is a deterministic
            # function of h; warm-starting from the previous call made it
            # jitter by more than fatol. Starting near the answer keeps it fast.
            u_start = _fit_L(prob, start, L0)[0]
            cache = {"L": u_start}

            def outer(h):
                v, u, _ = _neg_log_evidence(prob, h, u_start)
                cache["L"] = u
                return v

            res = optimize.minimize(outer, start, method="Nelder-Mead", bounds=list(zip(lo, hi)),
                                    options={"xatol": 1e-2, "fatol": 1e-2, "maxiter": 1000,
                                             "initial_simplex": _simplex(start, lo, hi)})
            if best is None or res.fun < best[0].fun:
                best = (res, cache["L"])
        res, L0 = best
        h = res.x
        converged = bool(res.success)
    else:
        h = h0
    u, val, ok = _fit_L(prob, h, L0)
    theta = np.concatenate([u, h])
    L, bias, s0, d0, lam = prob.unpack(theta)

    full_cov = np.linalg.inv(prob.fisher(theta))
    # Exact gauge: centre L and project its covariance onto mean(L) = 0.
    P = np.eye(n) - 1.0 / n
    hsd = np.sqrt(np.diag(full_cov)[prob.m:])
    return ProbitFit(
        grid=prob.grid, L=L - L.mean(), cov=P @ full_cov[:n, :n] @ P,
        sigma0=float(s0), d0=float(d0), lapse=float(lam), n_trials=len(trials),
        neg_log_post=float(val), converged=converged and bool(ok),
        hyper_sd={"log_sigma0": hsd[0], "log_d0": hsd[1], "logit_lapse": hsd[2]},
        bias_db=float(bias), bias_sd=float(np.sqrt(full_cov[n, n])),
    )
