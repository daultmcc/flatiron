"""Threshold of hearing from detection trials (DESIGN.md, "Threshold sessions").

Each ear has a threshold curve T_e(f) in dBFS RMS, as it was in the profile's
first threshold session. Every later session s is shifted by its own offset
c_s (c_0 = 0), shared by both ears: the system volume changes between
sessions, but it's the same for both ears, so T_L − T_R is measured too. The
chance of a positive answer (heard, or the right interval) to a burst at x dBFS:

    P = γ + (1 − γ − λ) Φ((x − T̄_e(f) − c_s) / σ)

γ is the session's false-alarm rate in a yes/no task (from its blank trials),
½ in a forced two-interval one, and in a two-interval one with a "neither"
answer (2ifc-neither) the chance that an unheard burst is claimed and its
interval guessed right: half the claim rate, estimated from the answers that
picked the wrong interval (positive there means the right interval;
"neither" and the wrong one are negative), λ the lapse rate (from the probe trials, clearly
audible ones), σ the psychometric function's spread. T̄ averages T over the
stimulus spectrum, as the perceptual model does.

T has the perceptual model's curvature prior, centred on the shape of the
ISO 226 threshold (held constant above 12.5 kHz), so a curve with little data
looks like a threshold rather than a straight line; and a loose prior on its
spread about its own mean (no pull on its level). (T, c) are the posterior
mode by Fisher scoring, with Laplace error bars. γ and λ are Beta posterior
means from their own trials; σ stays at its prior until there are
MIN_TRIALS_SIGMA signal trials, then maximises the Laplace evidence.

Collected only: nothing else uses these fits yet.
"""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import log_ndtr, ndtr

from flatiron import EARS, iso226
from flatiron.model.grid import log_grid, second_difference
from flatiron.model.probit import band_matrix

SIGMA_PRIOR_DB = 3.0  # median of the psychometric spread's log-normal prior
SIGMA_LOG_SD = 0.5
SIGMA_RANGE = (0.5, 12.0)
MIN_TRIALS_SIGMA = 150  # signal trials before σ is estimated
SMOOTH_SD_DB = 1.0  # SD of second differences at 1/24 octave, as for L
SPREAD_SD_DB = 20.0  # SD of T about its own mean, beyond the ISO shape
LEVEL_SD_DB = 1e4  # keeps the prior proper; no real pull
OFFSET_SD_DB = 40.0  # prior SD of a session's offset from the first session
FALSE_ALARM_PRIOR = (1.0, 19.0)  # Beta prior of a yes/no session's false-alarm rate: mean 5%
LAPSE_PRIOR = (1.0, 49.0)  # Beta prior of the lapse rate: mean 2%
P_CLIP = 1e-9


@dataclass
class ThresholdData:
    """Detection trials of one profile, in both ears. session: 0, 1, ... in
    order; blank trials have signal False."""
    ear: np.ndarray  # 0: left, 1: right
    freq: np.ndarray
    level: np.ndarray  # dBFS RMS of the burst (a blank's: what it would have been), plus any known EQ gain
    positive: np.ndarray
    signal: np.ndarray
    probe: np.ndarray
    session: np.ndarray
    task: np.ndarray  # per session: yesno, 2ifc or 2ifc-neither
    stimulus: np.ndarray  # per trial: stimulus key
    wrong: np.ndarray  # per trial: picked the wrong interval (2ifc-neither: a false claim)

    @property
    def n_sessions(self) -> int:
        return int(self.task.size)

    @property
    def neither(self) -> np.ndarray:
        """Per session: a 2ifc-neither one, whose γ comes from the fit."""
        return self.task == "2ifc-neither"

    @classmethod
    def build(cls, sessions) -> "ThresholdData":
        """sessions: (task, stimulus key, records) per session, in order;
        records have ear, freq, level_dbfs, positive, kind, undone and
        response."""
        rows = []
        for s, (task, key, recs) in enumerate(sessions):
            for r in recs:
                if not r.undone:
                    wrong = task == "2ifc-neither" and getattr(r, "response", "") in ("first", "second") \
                        and not r.positive
                    rows.append((EARS.index(r.ear), r.freq, r.level_dbfs + getattr(r, "eq_gain", 0.0),
                                 r.positive, r.kind != "blank",
                                 r.kind == "probe", s, key, wrong))
        cols = list(zip(*rows)) if rows else [()] * 9
        return cls(np.asarray(cols[0], int), np.asarray(cols[1], float), np.asarray(cols[2], float),
                   np.asarray(cols[3], bool), np.asarray(cols[4], bool), np.asarray(cols[5], bool),
                   np.asarray(cols[6], int), np.asarray([s[0] for s in sessions], dtype=object),
                   np.asarray(cols[7], dtype=object), np.asarray(cols[8], bool))

    def false_alarms(self, missed: np.ndarray | None = None) -> np.ndarray:
        """Per session: γ. Yes/no: the Beta posterior mean from its blank
        trials. 2IFC: ½. 2ifc-neither: from its wrong-interval answers, whose
        chance is (1 − λ)(1 − p) γ per trial; missed[s] is the session's sum
        of (1 − λ)(1 − p) under the current fit (without it, the prior mean)."""
        a, b = FALSE_ALARM_PRIOR
        out = np.full(self.n_sessions, 0.5)
        for s in range(self.n_sessions):
            if self.task[s] == "yesno":
                m = (self.session == s) & ~self.signal
                out[s] = (self.positive[m].sum() + a) / (m.sum() + a + b)
            elif self.task[s] == "2ifc-neither":
                n_wrong = self.wrong[self.session == s].sum()
                out[s] = (n_wrong + a) / (missed[s] + a + b) if missed is not None else a / (a + b)
        return out

    def lapse(self) -> float:
        """λ: misses on probe trials, pooled over sessions."""
        a, b = LAPSE_PRIOR
        m = self.probe & self.signal
        return float(((~self.positive[m]).sum() + a) / (m.sum() + a + b))


@dataclass
class ThresholdFit:
    grid: np.ndarray
    T: np.ndarray  # 2 x points: each ear's threshold in the first session's terms
    c: np.ndarray  # per session: its offset (c[0] = 0)
    cov: np.ndarray  # of (T_L, T_R, c[1:])
    sigma: float
    lapse: float
    false_alarm: np.ndarray  # per session (½ for a 2IFC one)
    n_signal: int
    sigma_fitted: bool
    neg_log_post: float
    converged: bool

    @property
    def n(self) -> int:
        return self.grid.size

    def level(self, ear: str, freqs, session: int, stimulus) -> tuple[np.ndarray, np.ndarray]:
        """T̄_e + c_s at the frequencies, as a burst there would be heard in
        that session, and its SD."""
        e, n = EARS.index(ear), self.n
        rows = band_matrix(self.grid, np.atleast_1d(freqs), [stimulus] * np.size(freqs))
        J = np.zeros((rows.shape[0], self.cov.shape[0]))
        J[:, e * n:(e + 1) * n] = rows
        if 0 < session < self.c.size:
            J[:, 2 * n + session - 1] = 1.0
        mean = rows @ self.T[e] + (self.c[session] if session < self.c.size else 0.0)
        return mean, np.sqrt(np.einsum("ij,jk,ik->i", J, self.cov, J))

    def sd(self, ear: str) -> np.ndarray:
        e, n = EARS.index(ear), self.n
        return np.sqrt(np.clip(np.diag(self.cov)[e * n:(e + 1) * n], 0, None))


def prior_shape(grid: np.ndarray) -> np.ndarray:
    return iso226.threshold(np.minimum(grid, iso226.F[-1]))


def _prior(grid: np.ndarray, n_sessions: int) -> tuple[np.ndarray, np.ndarray]:
    """Prior precision and mean of (T_L, T_R, c[1:])."""
    n = grid.size
    d2, w = second_difference(grid)
    scale = SMOOTH_SD_DB * 24**1.5
    centre = np.eye(n) - np.full((n, n), 1.0 / n)
    K = d2.T @ (w[:, None] * d2) / scale**2 + centre / SPREAD_SD_DB**2 + np.eye(n) / LEVEL_SD_DB**2
    m = 2 * n + max(n_sessions - 1, 0)
    P = np.zeros((m, m))
    P[:n, :n] = P[n:2 * n, n:2 * n] = K
    P[2 * n:, 2 * n:] = np.eye(m - 2 * n) / OFFSET_SD_DB**2
    mu = np.zeros(m)
    mu[:n] = mu[n:2 * n] = prior_shape(grid)
    return P, mu


class _Problem:
    def __init__(self, data: ThresholdData, grid: np.ndarray, n_sessions: int):
        self.grid, self.n = grid, grid.size
        self.S = max(n_sessions, 1)
        sig = data.signal
        self.x = data.level[sig]
        self.y = data.positive[sig].astype(float)
        s = data.session[sig]
        self.gamma = data.false_alarms()[s] if s.size else np.zeros(0)
        self.lam = data.lapse()
        n, m = self.n, 2 * self.n + self.S - 1
        rows = band_matrix(grid, data.freq[sig], data.stimulus[sig]) if s.size else np.zeros((0, n))
        self.J = np.zeros((s.size, m))  # d(T̄_e + c_s)/dθ
        e = data.ear[sig]
        self.J[e == 0, :n] = rows[e == 0]
        self.J[e == 1, n:2 * n] = rows[e == 1]
        later = s > 0
        self.J[np.flatnonzero(later), 2 * n + s[later] - 1] = 1.0
        self.P, self.mu = _prior(grid, self.S)
        self.data = data

    def terms(self, theta, sigma):
        eta = (self.x - self.J @ theta) / sigma
        span = 1.0 - self.gamma - self.lam
        p = np.clip(self.gamma + span * ndtr(eta), P_CLIP, 1 - P_CLIP)
        dp = span * np.exp(-0.5 * eta**2) / np.sqrt(2 * np.pi)
        return p, dp

    def update_gamma(self, theta, sigma) -> float:
        """Re-estimate the 2ifc-neither sessions' γ at θ; returns the largest change."""
        if not self.data.neither.any() or not self.x.size:
            return 0.0
        miss = (1 - self.lam) * (1 - ndtr((self.x - self.J @ theta) / sigma))
        s = self.data.session[self.data.signal]
        missed = np.bincount(s, weights=miss, minlength=self.S)
        new = self.data.false_alarms(missed)[s]
        change = float(np.max(np.abs(new - self.gamma))) if new.size else 0.0
        self.gamma = new
        return change

    def neg_log_post(self, theta, sigma) -> float:
        p, _ = self.terms(theta, sigma)
        r = theta - self.mu
        return float(-np.sum(self.y * np.log(p) + (1 - self.y) * np.log1p(-p)) + 0.5 * r @ self.P @ r)

    def blocks(self) -> list[tuple[np.ndarray, np.ndarray]]:
        """The levels that can move as a whole: each ear's T (its trials in
        the first session move with it, later sessions' too), and each later
        session's offset. Per block: its parameters and its trials."""
        n, s = self.n, self.data.session[self.data.signal]
        e = self.data.ear[self.data.signal]
        out = [(np.arange(k * n, (k + 1) * n), e == k) for k in (0, 1)]
        out += [(np.array([2 * n + k - 1]), s == k) for k in range(1, self.S)]
        return out

    def search_levels(self, theta, sigma, span=80.0, step=2.0) -> np.ndarray:
        """Move each block's level to its best value on a coarse grid of
        shifts. Far from the data the likelihood is flat (a burst far below
        threshold is answered at the guess rate, one far above at the lapse
        rate), so Fisher scoring alone can sit on a plateau: the first
        trials are 60 dB or more below a start at 0 dBFS, and an ear whose
        estimate is far off gets trials where its answers can't move it."""
        theta = theta.copy()
        shifts = np.arange(-span, span + step / 2, step)
        for idx, rows in self.blocks():
            if not rows.any():
                continue
            eta = (self.x[rows] - self.J[rows] @ theta)[None, :] - shifts[:, None]
            g = self.gamma[rows]
            p = np.clip(g + (1 - g - self.lam) * ndtr(eta / sigma), P_CLIP, 1 - P_CLIP)
            y = self.y[rows]
            nll = -(y * np.log(p) + (1 - y) * np.log1p(-p)).sum(axis=1)
            r = theta - self.mu
            one = np.zeros_like(theta)
            one[idx] = 1.0
            nll += shifts * (one @ self.P @ r) + 0.5 * shifts**2 * (one @ self.P @ one)
            best = shifts[int(np.argmin(nll))]
            if nll[int(np.argmin(nll))] < nll[np.searchsorted(shifts, 0.0)]:
                theta[idx] += best
        return theta

    def solve(self, sigma, init=None, max_iter=50, tol=1e-6):
        theta = self.mu.copy() if init is None else init.copy()
        if self.x.size:
            theta = self.search_levels(theta, sigma)
        f = self.neg_log_post(theta, sigma)
        converged = False
        for _ in range(max_iter):
            p, dp = self.terms(theta, sigma)
            # dη/dθ = −J/σ
            u = (self.y - p) / (p * (1 - p)) * dp
            g = self.J.T @ u / sigma + self.P @ (theta - self.mu)  # gradient of the negative log posterior
            W = dp**2 / (p * (1 - p))
            H = (self.J.T * W) @ self.J / sigma**2 + self.P
            step = np.linalg.solve(H, g)
            t = 1.0
            while True:
                new = theta - t * step
                f_new = self.neg_log_post(new, sigma)
                if f_new <= f + 1e-10 or t < 1e-4:
                    break
                t *= 0.5
            done = f - f_new < tol
            theta, f = new, min(f, f_new)
            if done:
                converged = True
                break
        p, dp = self.terms(theta, sigma)
        H = (self.J.T * (dp**2 / (p * (1 - p)))) @ self.J / sigma**2 + self.P
        return theta, f, H, converged

    def neg_evidence(self, sigma, init=None):
        """Laplace: −log p(y | σ) − log prior(σ), up to a constant."""
        theta, f, H, _ = self.solve(sigma, init)
        logdet = np.linalg.slogdet(H)[1]
        z = (np.log(sigma) - np.log(SIGMA_PRIOR_DB)) / SIGMA_LOG_SD
        return f + 0.5 * logdet + 0.5 * z * z + np.log(sigma), theta


def fit_thresholds(data: ThresholdData, grid: np.ndarray | None = None, sigma: float | None = None,
                   init: ThresholdFit | None = None, fit_sigma: bool | None = None) -> ThresholdFit:
    """sigma: hold σ there. Otherwise σ is the prior median, or (fit_sigma, by
    default once there are MIN_TRIALS_SIGMA signal trials) the evidence's maximum."""
    grid = log_grid() if grid is None else np.asarray(grid, float)
    prob = _Problem(data, grid, data.n_sessions)
    n_signal = int(prob.x.size)
    theta0 = None
    if init is not None and init.grid.size == grid.size and np.allclose(init.grid, grid):
        theta0 = np.concatenate([init.T.ravel(), init.c[1:prob.S]])
        if theta0.size < prob.mu.size:  # a new session since: its offset starts at the last one's
            theta0 = np.concatenate([theta0, np.full(prob.mu.size - theta0.size, init.c[-1])])
    if fit_sigma is None:
        fit_sigma = sigma is None and n_signal >= MIN_TRIALS_SIGMA
    if sigma is None:
        sigma = init.sigma if init is not None else SIGMA_PRIOR_DB
    if theta0 is not None:
        prob.update_gamma(theta0, sigma)  # 2ifc-neither: γ from the last fit's curve
    if fit_sigma:
        start = theta0
        res = minimize_scalar(lambda ls: prob.neg_evidence(np.exp(ls), start)[0],
                              bounds=np.log(SIGMA_RANGE), method="bounded", options={"xatol": 0.02})
        sigma = float(np.exp(res.x))
    theta, f, H, converged = prob.solve(sigma, theta0)
    # 2ifc-neither: γ depends on the curve; a few rounds (each warm-started).
    for _ in range(4):
        if prob.update_gamma(theta, sigma) < 0.002:
            break
        theta, f, H, converged = prob.solve(sigma, theta)
    n = grid.size
    gamma = data.false_alarms()
    if data.neither.any() and prob.x.size:
        s = data.session[data.signal]
        for k in np.flatnonzero(data.neither):
            if (s == k).any():
                gamma[k] = prob.gamma[np.flatnonzero(s == k)[0]]
    return ThresholdFit(grid, theta[:2 * n].reshape(2, n), np.concatenate([[0.0], theta[2 * n:]]),
                        np.linalg.inv(H), float(sigma), prob.lam, gamma, n_signal,
                        bool(fit_sigma), f, converged)
