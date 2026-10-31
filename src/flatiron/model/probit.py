"""Probit MAP fit of perceived level per ear (DESIGN.md, "Perceptual model").

P(A) = lapse/2 + (1 - lapse) * Phi((L_a - L_b + off) / sigma(d)),
sigma(d) = sigma0 * sqrt(1 + (d/d0)^2), d = |log2(f_a/f_b)|,
where `off` is every known level difference between A and B (Δ_a − Δ_b plus
any active or preview EQ gain).
"""

from dataclasses import dataclass, field

import numpy as np
from scipy import optimize
from scipy.special import expit, logit, ndtr

from flatiron.model.grid import interp_matrix, octaves, second_difference

SQRT_2PI = np.sqrt(2 * np.pi)


@dataclass
class Trials:
    """Within-ear judgments. `a_louder` is the listener's answer for stimulus A."""
    f_a: np.ndarray
    f_b: np.ndarray
    offset: np.ndarray
    a_louder: np.ndarray

    def __post_init__(self):
        self.f_a = np.asarray(self.f_a, float)
        self.f_b = np.asarray(self.f_b, float)
        self.offset = np.asarray(self.offset, float)
        self.a_louder = np.asarray(self.a_louder, bool)

    def __len__(self):
        return self.f_a.size

    @classmethod
    def concat(cls, parts: list["Trials"]) -> "Trials":
        return cls(*(np.concatenate([getattr(p, k) for p in parts])
                     for k in ("f_a", "f_b", "offset", "a_louder")))


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


# Box bounds on the hyperparameters, in their unconstrained coordinates.
LOG_SIGMA0_BOUNDS = (np.log(0.1), np.log(10.0))
LOG_D0_BOUNDS = (np.log(0.05), np.log(4.0))
LOGIT_LAPSE_BOUNDS = (logit(1e-4), logit(0.3))
GAUGE_SD = 0.01  # dB, soft gauge mean(L) = 0 used during optimisation


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


def band_matrix(grid: np.ndarray, freqs, offsets_oct: np.ndarray | None) -> np.ndarray:
    """Interpolation rows averaged over the stimulus spectrum (log2 offsets
    from each centre), so L is compared as heard through the stimulus band.
    None: point probes at the centre frequencies."""
    if offsets_oct is None:
        return interp_matrix(grid, freqs)
    f = np.asarray(freqs, float)
    return np.mean([interp_matrix(grid, f * 2.0**o) for o in offsets_oct], axis=0)


class _Problem:
    def __init__(self, trials: Trials, grid: np.ndarray, priors: Priors,
                 band_offsets_oct: np.ndarray | None = None):
        self.grid = grid
        self.n = grid.size
        self.A = (band_matrix(grid, trials.f_a, band_offsets_oct)
                  - band_matrix(grid, trials.f_b, band_offsets_oct))
        self.off = trials.offset
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
        return theta[:self.n], np.exp(theta[self.n]), np.exp(theta[self.n + 1]), expit(theta[self.n + 2])

    def _forward(self, theta):
        L, s0, d0, lam = self.unpack(theta)
        s = np.sqrt(1.0 + (self.d / d0) ** 2)
        sigma = s0 * s
        z = (self.A @ L + self.off) / sigma
        phi = np.exp(-0.5 * z * z) / SQRT_2PI
        p = lam / 2 + (1 - lam) * ndtr(z)
        # dp/dtheta, one row per trial
        dpdz = (1 - lam) * phi
        J = np.empty((z.size, self.n + 3))
        J[:, :self.n] = (dpdz / sigma)[:, None] * self.A
        J[:, self.n] = -dpdz * z
        J[:, self.n + 1] = dpdz * z * (self.d / d0) ** 2 / s**2
        J[:, self.n + 2] = (0.5 - ndtr(z)) * lam * (1 - lam)
        return L, p, J

    def prior_hessian(self):
        H = np.zeros((self.n + 3, self.n + 3))
        H[:self.n, :self.n] = self.K + 1.0 / (self.n**2 * GAUGE_SD**2)
        H[self.n:, self.n:] = np.diag(self.prec)
        return H

    def objective(self, theta):
        L, p, J = self._forward(theta)
        y = self.y
        nll = -np.sum(y * np.log(p) + (1 - y) * np.log1p(-p))
        g = J.T @ (-(y / p - (1 - y) / (1 - p)))
        h = theta[self.n:] - self.mu
        mean = L.mean()
        prior = (0.5 * L @ self.K @ L + 0.5 * mean**2 / GAUGE_SD**2
                 + 0.5 * np.sum(self.prec * h * h))
        g[:self.n] += self.K @ L + mean / (self.n * GAUGE_SD**2)
        g[self.n:] += self.prec * h
        return nll + prior, g

    def fisher(self, theta):
        _, p, J = self._forward(theta)
        return J.T @ (J / (p * (1 - p))[:, None]) + self.prior_hessian()


def _fit_L(prob: _Problem, h: np.ndarray, L0: np.ndarray):
    """MAP of L with hyperparameters h fixed. Returns (L, neg log posterior)."""
    n = prob.n

    def f(L):
        val, g = prob.objective(np.concatenate([L, h]))
        return val, g[:n]

    res = optimize.minimize(f, L0, jac=True, method="L-BFGS-B",
                            options={"maxiter": 5000, "maxfun": 20000, "ftol": 1e-12, "gtol": 1e-7})
    return res.x, res.fun, res.success


def _neg_log_evidence(prob: _Problem, h: np.ndarray, L0: np.ndarray):
    """Laplace approximation to -log p(trials | h) - log p(h), up to a constant."""
    L, val, ok = _fit_L(prob, h, L0)
    H = prob.fisher(np.concatenate([L, h]))[:prob.n, :prob.n]
    _, logdet = np.linalg.slogdet(H)
    return val + 0.5 * logdet, L, ok


def _simplex(start, lo, hi, step=0.3):
    pts = [start]
    for k in range(start.size):
        p = start.copy()
        p[k] = p[k] + step if p[k] + step <= hi[k] else p[k] - step
        pts.append(p)
    return np.array(pts)


def fit_probit(trials: Trials, grid: np.ndarray, priors: Priors = Priors(),
               init: ProbitFit | None = None, fit_hyper: bool = True,
               band_offsets_oct: np.ndarray | None = None) -> ProbitFit:
    """Fit perceived level L with Laplace error bars.

    The noise hyperparameters (sigma0, d0, lapse) are set by maximising the
    Laplace-approximated marginal likelihood with L integrated out. A joint MAP
    over L and the hyperparameters overfits: with a few trials per grid point L
    can separate the answers and sigma0 collapses. L is then the MAP given those
    hyperparameters. With fit_hyper=False the hyperparameters stay at `init`
    (or the prior means), which is the fast path for trial-by-trial refits.
    """
    prob = _Problem(trials, np.asarray(grid, float), priors, band_offsets_oct)
    n = prob.n
    lo = np.array([LOG_SIGMA0_BOUNDS[0], LOG_D0_BOUNDS[0], LOGIT_LAPSE_BOUNDS[0]])
    hi = np.array([LOG_SIGMA0_BOUNDS[1], LOG_D0_BOUNDS[1], LOGIT_LAPSE_BOUNDS[1]])
    L0 = np.zeros(n)
    h0 = prob.mu.copy()
    if init is not None:
        L0 = init.at(prob.grid)[0]
        h0 = np.array([np.log(init.sigma0), np.log(init.d0), logit(init.lapse)])
    h0 = np.clip(h0, lo, hi)

    converged = True
    if fit_hyper:
        # Search from the prior means and from the warm start, and keep the
        # better optimum: the Laplace evidence has a spurious basin at small
        # sigma0 / large lapse (few trials near threshold, so the information
        # term vanishes), and a warm start from an early, noisy estimate can
        # stay trapped in it.
        best = None
        for start in {tuple(prob.mu.clip(lo, hi)), tuple(h0)}:
            cache = {"L": L0}

            def outer(h):
                # Inner fits always start from the same point, so the
                # evidence is a deterministic function of h; warm-starting
                # from the last call made it jitter by more than fatol.
                v, L, _ = _neg_log_evidence(prob, h, L0)
                cache["L"] = L
                return v

            start = np.array(start)
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
    L, val, ok = _fit_L(prob, h, L0)
    theta = np.concatenate([L, h])
    _, s0, d0, lam = prob.unpack(theta)

    full_cov = np.linalg.inv(prob.fisher(theta))
    # Exact gauge: centre L and project its covariance onto mean(L) = 0.
    P = np.eye(n) - 1.0 / n
    hsd = np.sqrt(np.diag(full_cov)[n:])
    return ProbitFit(
        grid=prob.grid, L=L - L.mean(), cov=P @ full_cov[:n, :n] @ P,
        sigma0=float(s0), d0=float(d0), lapse=float(lam), n_trials=len(trials),
        neg_log_post=float(val), converged=converged and bool(ok),
        hyper_sd={"log_sigma0": hsd[0], "log_d0": hsd[1], "logit_lapse": hsd[2]},
    )
