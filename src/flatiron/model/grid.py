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


def cover(grid: np.ndarray, f_lo: float, f_hi: float, step_oct: float = INITIAL_STEP_OCT) -> np.ndarray:
    """The grid, extended at INITIAL_STEP_OCT-ish spacing so it reaches f_lo and
    f_hi (a measured range wider than an existing profile's grid). Points
    already there are kept; nothing is removed."""
    g = np.asarray(grid, float)
    parts = [g]
    if f_lo < g[0] * (1 - 1e-9):
        parts.append(log_grid(f_lo, g[0], step_oct)[:-1])
    if f_hi > g[-1] * (1 + 1e-9):
        parts.append(log_grid(g[-1], f_hi, step_oct)[1:])
    return np.sort(np.concatenate(parts)) if len(parts) > 1 else g


def in_range(grid: np.ndarray, f_lo: float, f_hi: float) -> np.ndarray:
    """Mask of the grid points inside [f_lo, f_hi] (a hair of slack for rounding)."""
    g = np.asarray(grid, float)
    return (g >= f_lo * (1 - 1e-6)) & (g <= f_hi * (1 + 1e-6))


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


def smoother_matrix(freqs, fwhm_oct, weights) -> np.ndarray:
    """Gaussian smoothing in log2 f as a matrix, normalised by `weights` (so
    the edges average only over what's there)."""
    x = np.log2(freqs)
    k = np.exp(-0.5 * ((x[:, None] - x[None, :]) / (fwhm_oct / 2.355)) ** 2) * weights[None, :]
    return k / np.maximum(k.sum(axis=1, keepdims=True), 1e-12)


def local_linear_smoother(freqs, fwhm_oct, weights) -> np.ndarray:
    """Gaussian-weighted local linear regression in log2 f, as a matrix. Unlike
    a plain weighted average it has no bias at the ends of the range (a
    one-sided average lags a slope there, which reads as a feature)."""
    x = np.log2(freqs)
    k = np.exp(-0.5 * ((x[:, None] - x[None, :]) / (fwhm_oct / 2.355)) ** 2) * weights[None, :]
    d = x[None, :] - x[:, None]  # (i, j): x_j − x_i
    s0, s1, s2 = k.sum(1), (k * d).sum(1), (k * d * d).sum(1)
    # Row i: weights of the local line's value at x_i.
    return k * (s2[:, None] - s1[:, None] * d) / np.maximum(s0 * s2 - s1**2, 1e-12)[:, None]
