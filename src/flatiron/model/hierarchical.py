"""The perceptual fit: seatings as random effects (DESIGN.md, "Perceptual model").

Per ear, a trial in group g (a session) depends on L_g = L + u_g:

    P(A) = lapse/2 + (1 - lapse) * Phi((Lg_a - Lg_b + off + bias * order) / sigma),
    sigma = sigma0 * sqrt((s(f_a)^2 + s(f_b)^2) / 2) * sqrt(1 + (d/d0)^2), d = |log2(f_a/f_b)|,
    s(f) = 1 below 6 kHz, treble_noise above 10 kHz, log-interpolated between,

where `off` is every known level difference between A and B (Δ_a − Δ_b plus any
active or preview EQ gain), Lg_a and Lg_b are L_g averaged (in dB) over each
trial's stimulus spectrum, order is +1 when A played first and −1 when B did,
and bias is the time-order error, fitted with L (a fixed effect), so its
uncertainty is part of L's error bars.

u_g is a Gaussian deviation with amplitude tau(f) and correlation width ell,
centred across frequency (a constant offset cancels within a group). tau(f) has
two values, below 5 kHz and above 8 kHz, log-interpolated between them. It's
held in a whitened basis, u_g = C D Phi v_g with v_g ~ N(0, I), where
Phi Phi' = the correlation (truncated to its significant eigenvectors, which
also deals with its ill-conditioning), D = diag(tau) and C centres. So a group
costs only a few dozen unknowns, and the same basis serves every value of tau.

The solve is Fisher scoring. Groups connect only through (L, bias), so the
Fisher matrix is block-arrow: eliminating each v_g in turn leaves a Schur
complement on (L, bias), and the work grows linearly with the number of groups.

The noise (sigma0, d0, lapse, treble_noise) and then tau are set by maximising the
Laplace-approximated marginal likelihood with L and the u_g integrated out. A
joint MAP overfits: with a few trials per grid point the curve can separate the
answers and sigma0 collapses. With few trials the noise stays at the prior; with
fewer than two groups of MIN_GROUP_TRIALS, tau does.
"""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy import optimize
from scipy.linalg import cho_factor, cho_solve, solve_triangular
from scipy.special import expit, logit, ndtr

from flatiron.model.grid import interp_matrix, log_grid, second_difference
from flatiron.model.probit import BalancePrior, Priors, ProbitFit, Trials, band_rows, noise_sd

SQRT_2PI = np.sqrt(2 * np.pi)
# Box bounds on the hyperparameters, in their unconstrained coordinates.
LOG_SIGMA0_BOUNDS = (np.log(0.1), np.log(10.0))
LOG_D0_BOUNDS = (np.log(0.05), np.log(4.0))
LOGIT_LAPSE_BOUNDS = (logit(1e-4), logit(0.3))
LOG_TREBLE_NOISE_BOUNDS = (np.log(0.2), np.log(5.0))
LOG_TAU_BOUNDS = (np.log(0.02), np.log(15.0))
GAUGE_SD = 0.01  # dB, soft gauge mean(L) = 0 used during optimisation
# With fewer trials than this the evidence's spurious small-sigma0 basin wins,
# so the noise parameters stay at the prior (or the warm start).
MIN_TRIALS_NOISE_FIT = 200
# tau is estimated only with at least two groups of this many trials in the ear.
MIN_GROUP_TRIALS = 60
TAU_LO_HZ, TAU_HI_HZ = 5000.0, 8000.0
BASIS_TOL = 1e-6  # eigenvalues below this share of the largest are dropped
TOL_FINAL = 1e-7  # Newton decrement (nats) for a converged fit
TOL_REFIT = 1e-3  # looser, for the refit after each answer: it starts next to the answer
MAX_ITER = 200  # Fisher scoring can crawl along a nearly flat ridge: the lapse term makes this non-concave


def tau_curve(grid, tau_lo: float, tau_hi: float) -> np.ndarray:
    return np.exp(np.interp(np.log2(grid), np.log2([TAU_LO_HZ, TAU_HI_HZ]), np.log([tau_lo, tau_hi])))


@lru_cache(maxsize=16)
def _kernel_basis(grid_bytes: bytes, ell: float) -> np.ndarray:
    x = np.log2(np.frombuffer(grid_bytes))
    w, E = np.linalg.eigh(np.exp(-0.5 * ((x[:, None] - x[None, :]) / ell) ** 2))
    keep = w > BASIS_TOL * w[-1]
    return E[:, keep] * np.sqrt(w[keep])


@dataclass
class _Factor:
    """The block-arrow Fisher matrix at a point, factored, and the Newton step."""
    S: tuple  # Cholesky of the Schur complement on (L, bias)
    H: dict  # group -> lower Cholesky of its own block
    C: dict  # group -> (n+1) x r coupling to (L, bias)
    da: np.ndarray
    dv: dict
    decrement: float  # half the squared Newton step: how far from the optimum, in nats
    logdet: float


class _Model:
    def __init__(self, trials: Trials, grid: np.ndarray, priors: Priors, n_groups: int | None = None):
        self.grid = grid = np.asarray(grid, float)
        self.n = n = grid.size
        self.p = priors
        order = np.argsort(trials.group, kind="stable")
        g = trials.group[order]
        self.n_groups = max(int(g[-1]) + 1 if g.size else 1, n_groups or 1)
        self.starts = np.searchsorted(g, np.arange(self.n_groups + 1))
        self.nonempty = [k for k in range(self.n_groups) if self.starts[k + 1] > self.starts[k]]
        f_a, f_b, stim = trials.f_a[order], trials.f_b[order], trials.stimulus[order]
        self.A = (band_rows(grid, f_a, stim) - band_rows(grid, f_b, stim)).tocsr()
        self.off = trials.offset[order]
        self.order = trials.order[order]
        self.y = trials.a_louder[order].astype(float)
        self.f_a, self.f_b = f_a, f_b
        d2, w = second_difference(grid)
        scale = priors.smooth_sd_db * 24**1.5
        self.K = d2.T @ (w[:, None] * d2) / scale**2 + np.eye(n) / priors.level_sd_db**2
        self.P0 = np.zeros((n + 1, n + 1))  # prior precision of (L, bias)
        self.P0[:n, :n] = self.K + 1.0 / (n**2 * GAUGE_SD**2)
        self.P0[n, n] = 1.0 / priors.bias_sd_db**2
        self.Phi0 = _kernel_basis(grid.tobytes(), priors.ell_oct)
        self.r = self.Phi0.shape[1]
        self.mu = np.array([np.log(priors.sigma0), np.log(priors.d0), logit(priors.lapse), 0.0])
        self.mu_tau = np.log([priors.tau_lo, priors.tau_hi])

    # -- hyperparameters -----------------------------------------------------

    def set_noise(self, h) -> None:
        self.s0, self.d0, self.lam = float(np.exp(h[0])), float(np.exp(h[1])), float(expit(h[2]))
        self.treble_noise = float(np.exp(h[3]))
        self.sigma = noise_sd(self.f_a, self.f_b, self.s0, self.d0, self.treble_noise)

    def noise_prior(self, h) -> float:
        p = self.p
        d = (np.asarray(h) - self.mu) / [p.sigma0_log_sd, p.d0_log_sd, p.lapse_logit_sd, p.treble_noise_log_sd]
        return 0.5 * float(d @ d)

    def set_tau(self, log_tau) -> None:
        self.tau_lo, self.tau_hi = float(np.exp(log_tau[0])), float(np.exp(log_tau[1]))
        self.tau = tau_curve(self.grid, self.tau_lo, self.tau_hi)
        Phi = self.tau[:, None] * self.Phi0
        self.Phi = Phi - Phi.mean(axis=0)  # C D Phi
        # A's rows sum to zero, so centring doesn't change what a trial sees.
        self.B = {k: self.A[self.starts[k]:self.starts[k + 1]] @ self.Phi for k in self.nonempty}

    def tau_prior(self, log_tau) -> float:
        d = (np.asarray(log_tau) - self.mu_tau) / self.p.tau_log_sd
        return 0.5 * float(d @ d)

    # -- the inner problem, with the hyperparameters fixed --------------------

    def _eta(self, a, v):
        eta = self.A @ a[:self.n] + a[self.n] * self.order + self.off
        for k in self.nonempty:
            eta[self.starts[k]:self.starts[k + 1]] += self.B[k] @ v[k]
        return eta

    def objective(self, a, v) -> float:
        z = self._eta(a, v) / self.sigma
        p = self.lam / 2 + (1 - self.lam) * ndtr(z)
        L = a[:self.n]
        mean = L.mean()
        return float(-np.sum(self.y * np.log(p) + (1 - self.y) * np.log1p(-p))
                     + 0.5 * L @ self.K @ L + 0.5 * mean**2 / GAUGE_SD**2
                     + 0.5 * a[self.n] ** 2 * self.P0[self.n, self.n] + 0.5 * np.sum(v[self.nonempty] ** 2))

    def gradient(self, a, v):
        """(g_a, g_v) for the finite-difference test."""
        g_trial, _ = self._trial_terms(a, v)
        L = a[:self.n]
        ga = np.empty(self.n + 1)
        ga[:self.n] = self.A.T @ g_trial + self.K @ L + L.mean() / (self.n * GAUGE_SD**2)
        ga[self.n] = self.order @ g_trial + a[self.n] * self.P0[self.n, self.n]
        gv = np.zeros_like(v)
        for k in self.nonempty:
            gv[k] = v[k] + self.B[k].T @ g_trial[self.starts[k]:self.starts[k + 1]]
        return ga, gv

    def _trial_terms(self, a, v):
        """d(nll)/d(eta) and the Fisher weight of each trial."""
        z = self._eta(a, v) / self.sigma
        p = self.lam / 2 + (1 - self.lam) * ndtr(z)
        dp = (1 - self.lam) * np.exp(-0.5 * z * z) / SQRT_2PI / self.sigma
        return -(self.y / p - (1 - self.y) / (1 - p)) * dp, dp * dp / (p * (1 - p))

    def schur(self, a, v):
        """The Fisher matrix with every group eliminated: the Schur complement
        on (L, bias) and its right-hand side, the gradient on (L, bias), and
        each group's factor, coupling and gradient."""
        n = self.n
        g, w = self._trial_terms(a, v)
        L = a[:n]
        ga = np.empty(n + 1)
        ga[:n] = self.A.T @ g + self.K @ L + L.mean() / (n * GAUGE_SD**2)
        ga[n] = self.order @ g + a[n] * self.P0[n, n]
        S = self.P0.copy()
        AW = self.A.multiply(w[:, None]).tocsr()
        S[:n, :n] += (AW.T @ self.A).toarray()
        Asw = AW.T @ self.order
        S[:n, n] += Asw
        S[n, :n] += Asw
        S[n, n] += self.order @ (w * self.order)
        logdet = 0.0
        H, C, gv = {}, {}, {}
        rhs = ga.copy()
        for k in self.nonempty:
            s, e = self.starts[k], self.starts[k + 1]
            B = self.B[k]
            wB = w[s:e, None] * B
            Hk = B.T @ wB
            Hk[np.diag_indices_from(Hk)] += 1.0
            Lk = np.linalg.cholesky(Hk)
            Ck = np.vstack([self.A[s:e].T @ wB, self.order[s:e] @ wB])  # (n+1) x r
            gk = v[k] + B.T @ g[s:e]
            X = solve_triangular(Lk, Ck.T, lower=True, check_finite=False)
            S -= X.T @ X
            rhs -= X.T @ solve_triangular(Lk, gk, lower=True, check_finite=False)
            logdet += 2 * np.log(np.diag(Lk)).sum()
            H[k], C[k], gv[k] = Lk, Ck, gk
        return S, rhs, ga, H, C, gv, logdet

    def group_steps(self, da, H, C, gv) -> dict:
        return {k: -cho_solve((H[k], True), gv[k] + C[k].T @ da, check_finite=False) for k in self.nonempty}

    def factor(self, a, v) -> _Factor:
        S, rhs, ga, H, C, gv, logdet = self.schur(a, v)
        cS = cho_factor(S, lower=True, check_finite=False)
        logdet += 2 * np.log(np.diag(cS[0])).sum()
        da = -cho_solve(cS, rhs, check_finite=False)
        dv = self.group_steps(da, H, C, gv)
        slope = ga @ da + sum(gv[k] @ dv[k] for k in self.nonempty)
        return _Factor(cS, H, C, da, dv, -0.5 * float(slope), float(logdet))

    def solve(self, a, v, tol: float = TOL_FINAL) -> tuple[np.ndarray, np.ndarray, float, _Factor, bool]:
        """MAP of (L, bias, v) by Fisher scoring with step halving."""
        a, v = a.copy(), v.copy()
        val = self.objective(a, v)
        for _ in range(MAX_ITER):
            fac = self.factor(a, v)
            if fac.decrement < tol:
                return a, v, val, fac, True
            t = 1.0
            while True:
                na, nv = a + t * fac.da, v.copy()
                for k in self.nonempty:
                    nv[k] += t * fac.dv[k]
                new = self.objective(na, nv)
                if new <= val or t < 1e-4:
                    break
                t /= 2
            if new > val:  # no progress along the step: as good as it gets
                return a, v, val, fac, fac.decrement < 1e-2
            a, v, val = na, nv, new
        return a, v, val, self.factor(a, v), False

    def neg_log_evidence(self, a0, v0) -> tuple[float, np.ndarray, np.ndarray]:
        """Laplace approximation to -log p(trials | noise, tau), up to a constant:
        v is whitened, so its prior adds no term that depends on tau."""
        a, v, val, fac, _ = self.solve(a0, v0)
        return val + 0.5 * fac.logdet, a, v

    # -- the answer -----------------------------------------------------------

    def covariances(self, a, v, fac: _Factor, all_groups: bool, Sinv: np.ndarray | None = None):
        """Sinv: the inverse of the Schur complement on (L, bias), if it isn't
        fac.S's (the ear's block of a joint fit's)."""
        n = self.n
        if Sinv is None:
            Sinv = cho_solve(fac.S, np.eye(n + 1), check_finite=False)
        P = np.eye(n) - 1.0 / n
        cov_L = P @ Sinv[:n, :n] @ P

        def group(k):
            """Cov(L + u_k), Cov(u_k), Cov(L, L + u_k)."""
            if k in fac.H:
                Hinv_Ct = cho_solve((fac.H[k], True), fac.C[k].T, check_finite=False)  # r x (n+1)
                cross_av = -Sinv @ Hinv_Ct.T  # (n+1) x r: Cov((L, bias), v_k)
                cov_v = cho_solve((fac.H[k], True), np.eye(self.r), check_finite=False) \
                    + Hinv_Ct @ Sinv @ Hinv_Ct.T
            else:
                cross_av = np.zeros((n + 1, self.r))
                cov_v = np.eye(self.r)
            Cu = self.Phi @ cov_v @ self.Phi.T
            cross = P @ cross_av[:n] @ self.Phi.T  # Cov(L, u_k)
            return cov_L + Cu + cross + cross.T, Cu, cov_L + cross

        focus_cov, _, focus_cross = group(self.n_groups - 1)
        group_cov = group_u_cov = None
        if all_groups:
            parts = [group(k) for k in range(self.n_groups)]
            group_cov = np.array([x[0] for x in parts])
            group_u_cov = np.array([x[1] for x in parts])
        return cov_L, float(np.sqrt(Sinv[n, n])), focus_cov, focus_cross, group_cov, group_u_cov


def _simplex(start, lo, hi, step=0.3):
    pts = [start]
    for k in range(start.size):
        p = start.copy()
        p[k] = p[k] + step if p[k] + step <= hi[k] else p[k] - step
        pts.append(p)
    return np.array(pts)


def enough_groups(trials: Trials) -> bool:
    """Whether tau can be estimated at all: two groups with enough trials."""
    if not len(trials):
        return False
    return int(np.sum(np.bincount(trials.group) >= MIN_GROUP_TRIALS)) >= 2


def fit_hierarchical(trials: Trials, grid: np.ndarray, priors: Priors = Priors(),
                     init: ProbitFit | None = None, fit_hyper: bool = True, fit_tau: bool | None = None,
                     n_groups: int | None = None, all_groups: bool = True,
                     tol: float = TOL_FINAL) -> ProbitFit:
    """Fit L_avg and each group's deviation, with Laplace error bars.

    fit_hyper: estimate the noise (with enough trials; otherwise it stays at
    `init` or the prior). fit_tau (default: as fit_hyper): estimate tau (with
    two groups of enough trials; otherwise it stays at the prior). Not fitted,
    tau stays at `init`, or the prior.
    n_groups: at least this many groups, the later ones possibly still empty
    (the scheduler's current session). The last group is the fit's focus.
    all_groups: also return every group's covariance (not needed trial by trial).
    """
    fit_tau = fit_hyper if fit_tau is None else fit_tau
    m = _Model(trials, grid, priors, n_groups)
    noise_lo = np.array([LOG_SIGMA0_BOUNDS[0], LOG_D0_BOUNDS[0], LOGIT_LAPSE_BOUNDS[0], LOG_TREBLE_NOISE_BOUNDS[0]])
    noise_hi = np.array([LOG_SIGMA0_BOUNDS[1], LOG_D0_BOUNDS[1], LOGIT_LAPSE_BOUNDS[1], LOG_TREBLE_NOISE_BOUNDS[1]])
    tau_lo, tau_hi = np.full(2, LOG_TAU_BOUNDS[0]), np.full(2, LOG_TAU_BOUNDS[1])

    a0, v0 = np.zeros(m.n + 1), np.zeros((m.n_groups, m.r))
    h0, t0 = m.mu.copy(), m.mu_tau.copy()
    if init is not None:
        a0 = np.append(init.at(m.grid)[0], init.bias_db)
        h0 = np.array([np.log(init.sigma0), np.log(init.d0), logit(init.lapse), np.log(init.treble_noise)])
        t0 = np.log([init.tau_lo, init.tau_hi])
        if init.v is not None and init.v.shape[1] == m.r and np.array_equal(init.grid, m.grid):
            k = min(init.v.shape[0], m.n_groups)
            v0[:k] = init.v[:k]
    h0, t0 = np.clip(h0, noise_lo, noise_hi), np.clip(t0, tau_lo, tau_hi)

    search_noise = fit_hyper and len(trials) >= MIN_TRIALS_NOISE_FIT
    tau_free = bool(fit_tau) and enough_groups(trials)
    if fit_tau and not tau_free:
        t0 = m.mu_tau.copy()  # too little to go on: the prior, so error bars allow for unseen seatings

    converged = True

    def search(x0, prior, lo, hi, apply, penalty):
        """Maximise the evidence over one set of hyperparameters, from the prior
        means and from the warm start, keeping the better optimum: it has a
        spurious basin at small sigma0 / large lapse, and a warm start from an
        early, noisy estimate can stay trapped in it."""
        nonlocal converged, a0, v0
        best = None
        for start in {tuple(np.clip(prior, lo, hi)), tuple(np.clip(x0, lo, hi))}:
            start = np.array(start)
            apply(start)
            # Every evaluation starts its inner fit from the same point (the
            # fit at the search's start), so the evidence is a deterministic
            # function of the hyperparameters. Starting near the answer keeps
            # it fast.
            a_s, v_s, *_ = m.solve(a0, v0, tol=TOL_REFIT)

            def outer(x):
                apply(x)
                return m.neg_log_evidence(a_s, v_s)[0] + penalty(x)

            res = optimize.minimize(outer, start, method="Nelder-Mead", bounds=list(zip(lo, hi)),
                                    options={"xatol": 1e-2, "fatol": 1e-2, "maxiter": 1000,
                                             "initial_simplex": _simplex(start, lo, hi)})
            if best is None or res.fun < best[0].fun:
                best = (res, a_s, v_s)
        res, a0, v0 = best
        converged = converged and bool(res.success)
        return res.x

    # Noise first, with tau at the prior, then tau once with the noise fixed.
    # Searching both together, or repeating this, runs away: tau grows and
    # sigma0 collapses, since 110-trial sessions rarely compare the same pair.
    if search_noise:
        m.set_tau(m.mu_tau if tau_free else t0)
        h0 = search(h0, m.mu, noise_lo, noise_hi, m.set_noise, m.noise_prior)
    if tau_free:
        m.set_noise(h0)
        t0 = search(t0, m.mu_tau, tau_lo, tau_hi, m.set_tau, m.tau_prior)

    m.set_noise(h0)
    m.set_tau(t0)
    a, v, val, fac, ok = m.solve(a0, v0, tol)
    return _result(m, a, v, val, fac, converged and ok, tau_free, len(trials), all_groups)


def _result(m: _Model, a, v, val, fac: _Factor, converged: bool, tau_free: bool, n_trials: int,
            all_groups: bool, Sinv: np.ndarray | None = None) -> ProbitFit:
    cov_L, bias_sd, focus_cov, focus_cross, group_cov, group_u_cov = m.covariances(a, v, fac, all_groups, Sinv)
    L = a[:m.n]
    return ProbitFit(
        grid=m.grid, L=L - L.mean(), cov=cov_L, sigma0=m.s0, d0=m.d0, lapse=m.lam, n_trials=n_trials,
        neg_log_post=val, converged=converged, bias_db=float(a[m.n]), bias_sd=bias_sd,
        treble_noise=m.treble_noise,
        tau_lo=m.tau_lo, tau_hi=m.tau_hi, tau_fitted=tau_free, tau=m.tau, u=v @ m.Phi.T, v=v,
        group_trials=np.diff(m.starts), group_cov=group_cov, group_u_cov=group_u_cov,
        focus_cov=focus_cov, focus_cross=focus_cross)


# -- the two ears together (DESIGN.md, "Channel balance") ----------------------

BALANCE_STEP_OCT = 1 / 48  # the grid the L-R difference is judged on
BALANCE_NUGGET = 1e-3  # share of the variance left uncorrelated: numerical, and keeps the precision finite


def balance_precision(grid_a: np.ndarray, grid_b: np.ndarray, balance: BalancePrior) -> np.ndarray:
    """The prior precision on (L_a, L_b) of their difference D = L_a - L_b:
    D ~ N(0, K) on a common grid, K with amplitude lo_db below 5 kHz and
    hi_db above 8 kHz and a squared-exponential correlation of width
    ell_oct, plus a free constant (each ear's level is a gauge)."""
    lo, hi = max(grid_a[0], grid_b[0]), min(grid_a[-1], grid_b[-1])
    G = log_grid(lo, hi, BALANCE_STEP_OCT)
    x = np.log2(G)
    amp = tau_curve(G, balance.lo_db, balance.hi_db)
    K = amp[:, None] * np.exp(-0.5 * ((x[:, None] - x[None, :]) / balance.ell_oct) ** 2) * amp[None, :]
    K[np.diag_indices_from(K)] += BALANCE_NUGGET * amp**2
    Q = cho_solve(cho_factor(K, lower=True), np.eye(G.size))
    q1 = Q.sum(axis=1)
    Q -= np.outer(q1, q1) / q1.sum()
    M = np.hstack([interp_matrix(grid_a, G), -interp_matrix(grid_b, G)])
    return M.T @ Q @ M


def fit_balanced(trials: dict, fits: dict, balance: BalancePrior = BalancePrior(), priors: Priors = Priors(),
                 all_groups: bool = True, tol: float = TOL_FINAL) -> dict:
    """Both ears' curves fitted together, with a prior that their difference
    is small (`balance`). Each ear keeps the noise and seating spread of its
    own fit in `fits` (from fit_hierarchical, which also warm-starts this):
    those are estimated ear by ear, and only the curves are coupled."""
    ears = list(fits)
    if len(ears) != 2:
        raise ValueError("a balanced fit needs exactly two ears")
    ms, a0, v0 = [], [], []
    for e in ears:
        f = fits[e]
        m = _Model(trials[e], f.grid, priors)
        m.set_noise([np.log(f.sigma0), np.log(f.d0), logit(f.lapse), np.log(f.treble_noise)])
        m.set_tau(np.log([f.tau_lo, f.tau_hi]))
        v = np.zeros((m.n_groups, m.r))
        if f.v is not None and f.v.shape[1] == m.r:
            k = min(f.v.shape[0], m.n_groups)
            v[:k] = f.v[:k]
        ms.append(m), a0.append(np.append(f.L, f.bias_db)), v0.append(v)
    sizes = [m.n + 1 for m in ms]
    cut = np.cumsum([0] + sizes)
    Q = np.zeros((cut[-1], cut[-1]))
    Qc = balance_precision(ms[0].grid, ms[1].grid, balance)
    idx = np.r_[cut[0]:cut[0] + ms[0].n, cut[1]:cut[1] + ms[1].n]  # the levels, not the biases
    Q[np.ix_(idx, idx)] = Qc

    def objective(a, vs):
        return sum(m.objective(a[cut[i]:cut[i + 1]], vs[i]) for i, m in enumerate(ms)) + 0.5 * a @ Q @ a

    def step(a, vs):
        parts = [m.schur(a[cut[i]:cut[i + 1]], vs[i]) for i, m in enumerate(ms)]
        S = Q.copy()
        for i, p in enumerate(parts):
            S[cut[i]:cut[i + 1], cut[i]:cut[i + 1]] += p[0]
        Qa = Q @ a
        ga = np.concatenate([p[2] for p in parts]) + Qa
        rhs = np.concatenate([p[1] for p in parts]) + Qa
        cS = cho_factor(S, lower=True, check_finite=False)
        da = -cho_solve(cS, rhs, check_finite=False)
        dvs = [m.group_steps(da[cut[i]:cut[i] + m.n + 1], *parts[i][3:6]) for i, m in enumerate(ms)]
        slope = ga @ da + sum(parts[i][5][k] @ dvs[i][k] for i, m in enumerate(ms) for k in m.nonempty)
        return cS, parts, da, dvs, -0.5 * float(slope)

    a, vs = np.concatenate(a0), v0
    val = objective(a, vs)
    ok = False
    for _ in range(MAX_ITER):
        cS, parts, da, dvs, decrement = step(a, vs)
        if decrement < tol:
            ok = True
            break
        t = 1.0
        while True:
            na = a + t * da
            nvs = [v.copy() for v in vs]
            for i, m in enumerate(ms):
                for k in m.nonempty:
                    nvs[i][k] += t * dvs[i][k]
            new = objective(na, nvs)
            if new <= val or t < 1e-4:
                break
            t /= 2
        if new > val:
            ok = decrement < 1e-2
            break
        a, vs, val = na, nvs, new
    else:
        cS, parts, da, dvs, decrement = step(a, vs)
    Sinv = cho_solve(cS, np.eye(cut[-1]), check_finite=False)
    out = {}
    for i, (e, m) in enumerate(zip(ears, ms)):
        ai = a[cut[i]:cut[i + 1]]
        fac = _Factor(None, parts[i][3], parts[i][4], da[cut[i]:cut[i + 1]], dvs[i], decrement, 0.0)
        out[e] = _result(m, ai, vs[i], m.objective(ai, vs[i]), fac, fits[e].converged and ok, fits[e].tau_fitted,
                         len(trials[e]), all_groups, Sinv[cut[i]:cut[i + 1], cut[i]:cut[i + 1]])
    return out
