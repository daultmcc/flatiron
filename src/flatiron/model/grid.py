"""Log-frequency measurement grids (DESIGN.md, "Frequency grid")."""

import numpy as np

MEASURE_LO = 850.0
MEASURE_HI = 15_000.0
INITIAL_STEP_OCT = 1 / 24
MIN_STEP_OCT = 1 / 48


def log_grid(f_lo: float = MEASURE_LO, f_hi: float = MEASURE_HI,
             step_oct: float = INITIAL_STEP_OCT) -> np.ndarray:
    """Log-spaced grid from f_lo, stepping as close to step_oct as fits f_hi exactly."""
    span = np.log2(f_hi / f_lo)
    n = int(round(span / step_oct))
    return f_lo * 2.0 ** (np.arange(n + 1) * span / n)


def octaves(f_a, f_b) -> np.ndarray:
    return np.abs(np.log2(np.asarray(f_a, float) / np.asarray(f_b, float)))


def interp_matrix(grid: np.ndarray, freqs) -> np.ndarray:
    """Rows map grid values to `freqs` by linear interpolation in log2 f.

    Frequencies outside the grid are clamped to the end nodes.
    """
    x = np.log2(grid)
    q = np.clip(np.log2(np.asarray(freqs, float)), x[0], x[-1])
    hi = np.clip(np.searchsorted(x, q, side="right"), 1, x.size - 1)
    lo = hi - 1
    w = (q - x[lo]) / (x[hi] - x[lo])
    m = np.zeros((q.size, x.size))
    rows = np.arange(q.size)
    m[rows, lo] = 1.0 - w
    m[rows, hi] += w
    return m


def common_grid(grids) -> np.ndarray:
    """A grid shared by several fits: the finest spacing any fit uses, over the
    range they all measured. It holds shared curves; statistics stay on each
    fit's own grid (see interp_matrix(common, own))."""
    lo = max(float(g[0]) for g in grids)
    hi = min(float(g[-1]) for g in grids)
    if lo >= hi:
        raise ValueError("these fits have no measured range in common")
    return log_grid(lo, hi, MIN_STEP_OCT)


def second_difference(grid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Divided second-difference operator in log2 f and per-row interval widths."""
    x = np.log2(grid)
    h = np.diff(x)
    n = x.size
    d2 = np.zeros((n - 2, n))
    for i in range(1, n - 1):
        h1, h2 = h[i - 1], h[i]
        s = 2.0 / (h1 + h2)
        d2[i - 1, i - 1] = s / h1
        d2[i - 1, i] = -s * (1 / h1 + 1 / h2)
        d2[i - 1, i + 1] = s / h2
    return d2, 0.5 * (h[:-1] + h[1:])
