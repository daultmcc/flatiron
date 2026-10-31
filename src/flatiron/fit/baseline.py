"""Smooth baseline that can't follow anything narrower than about 2W
(DESIGN.md, pipeline step 2): a penalised cubic spline in log2 f with knots
every 2W, refit with robust weights so the features don't drag it.

Weights: Huber to convergence, then Tukey bisquare from there. Huber alone
still gives a 4-SD feature a third of full weight and kept only ~3.3 of a
4 dB broad bump in simulation; bisquare drops such points entirely (~3.85),
and starting it from the Huber fit avoids its bad local solutions.

The final fit is linear in the data (B (BᵀWB + λP)⁻¹ BᵀW with the last
weights), so the residual's covariance follows exactly.
"""

from dataclasses import dataclass

import numpy as np
from scipy.interpolate import BSpline

HUBER_C = 1.345  # in SDs: the usual 95%-efficiency constants
TUKEY_C = 4.685
PENALTY = 1e-3  # second-derivative penalty, relative to the data weight
IRLS_ITERS = 30


def basis(x: np.ndarray, x0: float, x1: float, spacing: float) -> tuple[np.ndarray, np.ndarray]:
    """Cubic B-spline design matrix on x and the knot vector."""
    n_int = max(int(np.floor((x1 - x0) / spacing + 1e-9)) - 1, 0)
    inner = np.linspace(x0, x1, n_int + 2)
    t = np.concatenate([[x0] * 3, inner, [x1] * 3])
    return BSpline.design_matrix(np.clip(x, x0, x1), t, 3).toarray(), t


def curvature_penalty(t: np.ndarray, x0: float, x1: float) -> np.ndarray:
    """∫ s''(x)² dx as a quadratic form in the spline coefficients."""
    xs = np.linspace(x0, x1, 400)
    nb = len(t) - 4
    d2 = np.column_stack([BSpline(t, np.eye(nb)[k], 3).derivative(2)(xs) for k in range(nb)])
    return d2.T @ d2 * (xs[1] - xs[0])


@dataclass
class Baseline:
    values: np.ndarray  # baseline at the grid points
    smoother: np.ndarray  # S: values = S @ data
    weights: np.ndarray  # final Huber weights (1 = full weight)


def fit_baseline(grid: np.ndarray, data: np.ndarray, sd: np.ndarray, max_width_oct: float,
                 penalty: float = PENALTY) -> Baseline:
    x = np.log2(grid)
    x0, x1 = x[0], x[-1]
    B, t = basis(x, x0, x1, 2 * max_width_oct)
    P = curvature_penalty(t, x0, x1)
    # Weight by interval width too, so refined (denser) regions don't dominate.
    dens = np.gradient(x)
    w0 = dens / sd**2
    lam = penalty * w0.sum()
    def huber(r):
        return np.minimum(1.0, HUBER_C / np.maximum(np.abs(r), 1e-12))

    def tukey(r):
        u = np.abs(r) / TUKEY_C
        return np.where(u < 1, (1 - u**2) ** 2, 0.0)

    hub = np.ones_like(data)
    for weight in (huber, tukey):
        for _ in range(IRLS_ITERS):
            W = w0 * hub
            coef = np.linalg.solve(B.T @ (W[:, None] * B) + lam * P, B.T @ (W * data))
            new = np.maximum(weight((data - B @ coef) / sd), 1e-6)  # never all-zero
            done = np.max(np.abs(new - hub)) < 1e-6
            hub = new
            if done:
                break
    W = w0 * hub
    S = B @ np.linalg.solve(B.T @ (W[:, None] * B) + lam * P, B.T * W)
    return Baseline(S @ data, S, hub)
