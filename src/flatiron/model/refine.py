"""Adaptive grid refinement (DESIGN.md, "Adaptive selection")."""

import numpy as np

from flatiron.model.grid import MIN_STEP_OCT, second_difference
from flatiron.model.probit import ProbitFit


def curvature_z(fit: ProbitFit) -> np.ndarray:
    """|second difference of L̂| / its posterior SD, per grid point (0 at the ends)."""
    d2, _ = second_difference(fit.grid)
    val = d2 @ fit.L
    sd = np.sqrt(np.einsum("ij,jk,ik->i", d2, fit.cov, d2))
    z = np.zeros(fit.grid.size)
    z[1:-1] = np.abs(val) / sd
    return z


def refine_grid(fit: ProbitFit, k: float = 2.0, min_step_oct: float = MIN_STEP_OCT) -> np.ndarray:
    """Insert log-midpoints on both sides of every point where the curve bends
    by more than k SD, unless the halves would be narrower than min_step_oct."""
    g = fit.grid
    x = np.log2(g)
    bend = np.flatnonzero(curvature_z(fit) > k)
    # Interval m spans grid points m and m+1.
    intervals = np.unique(np.concatenate([bend - 1, bend]))
    intervals = intervals[(intervals >= 0) & (intervals < g.size - 1)]
    wide = (x[intervals + 1] - x[intervals]) / 2 >= min_step_oct * (1 - 1e-6)
    mids = 2.0 ** (0.5 * (x[intervals[wide]] + x[intervals[wide] + 1]))
    return np.sort(np.concatenate([g, mids]))


def refine_around(grid: np.ndarray, freqs, half_width_oct: float = 1 / 12,
                  min_step_oct: float = MIN_STEP_OCT) -> np.ndarray:
    """Halve every grid interval overlapping fc ± half_width_oct for each fc."""
    g = np.asarray(grid, float)
    x = np.log2(g)
    mids = []
    for fc in np.atleast_1d(freqs):
        c = np.log2(fc)
        for m in range(g.size - 1):
            if x[m + 1] >= c - half_width_oct and x[m] <= c + half_width_oct \
                    and (x[m + 1] - x[m]) / 2 >= min_step_oct * (1 - 1e-6):
                mids.append(2.0 ** (0.5 * (x[m] + x[m + 1])))
    return np.unique(np.concatenate([g, mids]))
