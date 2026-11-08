"""The joint baseline (DESIGN.md, "Joint baseline"): the posterior mean of the
tonality curve under an explicit model of D, in place of the robust fit's mode.

    D_i = s(x_i) + e_i,   s = B c   (today's spline, knots every 2W)

Each point is either tonality plus the fit's error, or that plus a feature:

    e_i ~ (1 − π) N(0, sd_i² + floor²) + π N(0, sd_i² + floor² + spread²)

with the feature share π, and the spread and floor in dB. The curvature prior
is today's stiffness, but set at a fixed SD in dB rather than at the fit's
own, so shrinking the error bars means more confidence in the same split, not
a stiffer spline.

The robust fit finds one mode of a model like this, and where two coexist it
can jump between them. The posterior mean weighs every reading by how well it
explains the data, so it moves continuously with the data and the error bars.
The tonality has only a handful of coefficients, so the mean is integrated
directly, by importance sampling over fixed draws:

- The first proposal is centred on the Huber fit (convex, so unique), and as
  wide as the posterior with the likelihood raised to a small power.
- Each later one is matched to the posterior's moments under a higher power
  (tempering), so it never has to find a narrow posterior far from its start.
- Where a block sits far off the reference, the posterior can have several
  narrow modes far apart (one per reading), and no single moment-matched
  proposal covers them. So the modes are found too, by local optimisation
  from a diverse set of the broad rounds' best draws, and each gets a
  proposal of its own (its Laplace approximation, wider).
- Every draw is weighted against the mixture of all the proposals (multiple
  importance sampling). The broad rounds keep everything covered.

The draws never change and nothing is resampled, so the estimate is a smooth
function of the data and the SDs, except where a mode is found or not. That
changes only the proposal: the estimate moves by its Monte Carlo error
(hundredths of a dB), not from one reading to another as a mode-finder does.

The residual's covariance needs the estimate's Jacobian. For a posterior mean
it's exact: J = Cov(s, ∂ log p(D | c)/∂D | D), from the same weighted draws.
"""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy.linalg import solve_triangular
from scipy.special import expit, logsumexp

from flatiron.fit.baseline import HUBER_C, basis, curvature_penalty, fit_baseline
from flatiron.model.grid import INITIAL_STEP_OCT

# The curvature prior is today's stiffness at this SD, fixed in dB: about the
# real profiles' median SD (0.84 to 1.41 dB per ear).
SD_REF_DB = 1.1
FEATURE_SHARE = 0.4  # π: the share of points expected to sit on a feature
FEATURE_SPREAD_DB = 8.0  # how far features reach (SD of their heights)
TONALITY_FLOOR_DB = 0.5  # the tonality's own ripple, added to the fit's SD: makes the split less sensitive to the SDs
DRAWS = 4000  # per proposal
# The power the likelihood is raised to for each round's proposal (the last
# round's posterior moments, so the first is matched to the 0.01 posterior).
TEMPERS = (0.01, 0.03, 0.1, 0.3, 1.0)
STARTS = 16  # local optimisations: the Huber and robust fits, and the best draws, spread out
CANDIDATES = 400  # the draws with the highest posterior that the starts are spread among
MODE_GAP_NATS = 20.0  # modes this far below the best (Laplace evidence) get no proposal
MODE_INFLATE = 1.3  # a mode's proposal: its Laplace covariance, this much wider
INFLATE = 1.5  # each proposal: the posterior covariance it's matched to, this much wider
DOF = 4  # the proposals are multivariate t
SEED = 20261108  # fixed, not derived from the data: the same draws every time
HUBER_ITERS = 100


@dataclass
class JointBaseline:
    values: np.ndarray  # posterior mean of the tonality at the grid points
    jacobian: np.ndarray  # J = ∂values/∂data; the residual is (I − J) to first order
    sd: np.ndarray  # posterior SD of the tonality at the grid points
    ess: float  # effective number of draws behind the mean
    feature_prob: np.ndarray  # posterior probability that each point sits on a feature


def _mixture_hessian(B, R, w, s1, s2, log_in, log_out):
    """Gradient and Hessian of the log likelihood in c (R = data − B c)."""
    p = expit((log_in - 0.5 * R**2 / s1) - (log_out - 0.5 * R**2 / s2))
    v = p / s1 + (1 - p) / s2
    second = R**2 * (p / s1**2 + (1 - p) / s2**2 - v**2) - v  # d² log f / dR²
    return B.T @ (w * R * v), B.T @ ((w * second)[:, None] * B)


@lru_cache(maxsize=8)
def _draws(rounds: int, n: int, m: int) -> np.ndarray:
    """Standard t draws (DOF) in m dimensions, n per round."""
    rng = np.random.default_rng(SEED)
    z = rng.standard_normal((rounds, n, m))
    z *= np.sqrt(DOF / rng.chisquare(DOF, (rounds, n)))[..., None]
    z.setflags(write=False)
    return z


def _log_t(C: np.ndarray, mu: np.ndarray, L: np.ndarray) -> np.ndarray:
    """Log density of a multivariate t (DOF) with location mu and scale L Lᵀ, up to a constant."""
    u = solve_triangular(L, (C - mu).T, lower=True)
    return -(DOF + mu.size) / 2 * np.log1p((u**2).sum(0) / DOF) - np.log(np.diag(L)).sum()


def fit_joint_baseline(grid: np.ndarray, data: np.ndarray, sd: np.ndarray, max_width_oct: float,
                       penalty: float = 0.1, share: float = FEATURE_SHARE, spread_db: float = FEATURE_SPREAD_DB,
                       floor_db: float = TONALITY_FLOOR_DB, sd_ref_db: float = SD_REF_DB,
                       draws: int = DRAWS) -> JointBaseline:
    """penalty: the robust baseline's stiffness setting, at sd_ref_db."""
    x = np.log2(grid)
    B, t = basis(x, x[0], x[-1], 2 * max_width_oct)
    P = curvature_penalty(t, x[0], x[-1])
    m = B.shape[1]
    # Each point counts for the width it stands for, in units of the initial
    # grid step, so refined (denser) regions don't dominate: as in the robust fit.
    w = np.gradient(x) / INITIAL_STEP_OCT
    # The robust fit's penalty is relative to its total data weight, Σ w/sd²;
    # here that's taken at sd_ref_db everywhere, so it's fixed in dB.
    Q = penalty * max_width_oct**4 * w.sum() / sd_ref_db**2 * P
    s1 = sd**2 + floor_db**2
    s2 = s1 + spread_db**2
    log_in = np.log1p(-share) - 0.5 * np.log(s1)
    log_out = np.log(share) - 0.5 * np.log(s2)

    def loglik(C):
        R = data - C @ B.T
        return np.logaddexp(log_in - 0.5 * R**2 / s1, log_out - 0.5 * R**2 / s2) @ w

    # Start: the Huber fit, which is convex and so a continuous function of the data.
    wd = w / s1
    h = np.ones_like(data)
    for _ in range(HUBER_ITERS):
        c = np.linalg.solve(B.T @ ((wd * h)[:, None] * B) + Q, B.T @ (wd * h * data))
        new = np.minimum(1.0, HUBER_C / np.maximum(np.abs(data - B @ c) / np.sqrt(s1), 1e-12))
        done = np.max(np.abs(new - h)) < 1e-12
        h = new
        if done:
            break
    c_huber = c
    mu, L = c, np.linalg.cholesky(np.linalg.inv(TEMPERS[0] * (B.T @ ((wd * h)[:, None] * B)) + Q))

    z = _draws(len(TEMPERS) + STARTS + 1, draws, m)
    props, Cs, lls = [], [], []

    def add(mu, L):
        props.append((mu, L))
        Cs.append(mu + z[len(props) - 1] @ L.T)
        lls.append(loglik(Cs[-1]))

    def weights(beta):
        """Every draw so far, against the mixture of every proposal so far."""
        C, ll = np.concatenate(Cs), np.concatenate(lls)
        lq = logsumexp(np.stack([_log_t(C, a, b) for a, b in props]), axis=0)
        lw = beta * ll - 0.5 * np.einsum("ij,jk,ik->i", C, Q, C) - lq
        return C, ll, lw

    L0 = L
    for k, beta in enumerate(TEMPERS):
        add(mu, L)
        C, ll, lw = weights(beta)
        wt = np.exp(lw - logsumexp(lw))
        mu = wt @ C
        d = C - mu
        L = np.linalg.cholesky((d.T @ (wt[:, None] * d)) * INFLATE**2 + 1e-12 * np.eye(m))

    # The modes: Newton from the Huber fit, the robust (Tukey) fit's mode, and
    # the best draws so far under the full posterior, chosen farthest-first in
    # the broadest proposal's metric so that every reading gets a start.
    lpost = ll - 0.5 * np.einsum("ij,jk,ik->i", C, Q, C)
    cand = C[np.argsort(-lpost, kind="stable")[:CANDIDATES]]
    robust = np.linalg.lstsq(B, fit_baseline(grid, data, np.sqrt(s1), max_width_oct, penalty).values, rcond=None)[0]
    starts = [c_huber, robust, cand[0]]
    dist = lambda c0: (solve_triangular(L0, (cand - c0).T, lower=True) ** 2).sum(0)
    near = np.minimum.reduce([dist(c0) for c0 in starts])
    while len(starts) < STARTS:
        i = int(np.argmax(near))
        starts.append(cand[i])
        near = np.minimum(near, dist(cand[i]))
    modes = []
    for c in starts:
        for _ in range(50):
            grad, Hl = _mixture_hessian(B, data - B @ c, w, s1, s2, log_in, log_out)
            Hp = Q - Hl
            ev, V = np.linalg.eigh(Hp)
            step = V @ ((V.T @ (grad - Q @ c)) / np.maximum(np.abs(ev), 1e-6))  # Newton on |H|: always uphill
            f0 = loglik(c[None])[0] - 0.5 * c @ Q @ c
            t = 1.0
            while t > 1e-4:
                cn = c + t * step
                if loglik(cn[None])[0] - 0.5 * cn @ Q @ cn >= f0:
                    break
                t /= 2
            c = cn
            if np.max(np.abs(t * step)) < 1e-6:
                break
        grad, Hl = _mixture_hessian(B, data - B @ c, w, s1, s2, log_in, log_out)
        ev, V = np.linalg.eigh(Q - Hl)
        if ev[0] <= 0 or any(np.max(np.abs(B @ (c - c0))) < 0.05 for c0, _, _ in modes):
            continue  # not a maximum, or one already found
        lap = loglik(c[None])[0] - 0.5 * c @ Q @ c - 0.5 * np.log(ev).sum()
        modes.append((c, V / np.sqrt(ev), lap))
    best = max((lap for *_, lap in modes), default=0.0)
    for c, A, lap in modes:
        if lap > best - MODE_GAP_NATS:
            # A Cholesky factor of the Laplace covariance A Aᵀ, wider.
            add(c, np.linalg.cholesky(A @ A.T + 1e-12 * np.eye(m)) * MODE_INFLATE)
    C, ll, lw = weights(1.0)
    wt = np.exp(lw - logsumexp(lw))

    S = C @ B.T  # each draw's tonality on the grid
    values = wt @ S
    dS = S - values
    R = data - S
    a, b = log_in - 0.5 * R**2 / s1, log_out - 0.5 * R**2 / s2
    p_in = expit(a - b)
    score = -w * R * (p_in / s1 + (1 - p_in) / s2)  # ∂ log p(D | c)/∂D for each draw
    J = dS.T @ (wt[:, None] * (score - wt @ score))
    return JointBaseline(values, J, np.sqrt(wt @ dS**2), float(1 / np.sum(wt**2)), 1 - wt @ p_in)
