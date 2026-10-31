"""Non-adaptive trial designs for the simulator (baseline for the scheduler)."""

import numpy as np

from flatiron.model.grid import octaves

CATCH_RATE = 0.05
CATCH_OFFSET_DB = 6.0


def random_pairs(grid: np.ndarray, n: int, rng: np.random.Generator,
                 max_step: int = 3, long_frac: float = 0.25,
                 long_max_oct: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Index pairs from the redundant comparison graph (key decision 4)."""
    N = grid.size
    i = rng.integers(0, N, n)
    j = np.empty(n, dtype=int)
    long = rng.random(n) < long_frac
    for k in range(n):
        if long[k]:
            far = np.flatnonzero((octaves(grid, grid[i[k]]) <= long_max_oct)
                                 & (np.abs(np.arange(N) - i[k]) > max_step))
            if far.size:
                j[k] = rng.choice(far)
                continue
        near = [m for m in range(i[k] - max_step, i[k] + max_step + 1)
                if m != i[k] and 0 <= m < N]
        j[k] = rng.choice(near)
    return i, j


def uniform_design(grid: np.ndarray, n: int, rng: np.random.Generator,
                   delta_range_db: float = 6.0, catch_rate: float = CATCH_RATE):
    """Random graph pairs with Δ uniform in ±delta_range_db, plus catch trials.

    Returns (f_a, f_b, offset, is_catch).
    """
    i, j = random_pairs(grid, n, rng)
    f_a, f_b = grid[i].copy(), grid[j].copy()
    offset = rng.uniform(-delta_range_db, delta_range_db, n)
    catch = rng.random(n) < catch_rate
    f_b[catch] = f_a[catch]
    offset[catch] = rng.choice([-CATCH_OFFSET_DB, CATCH_OFFSET_DB], catch.sum())
    return f_a, f_b, offset, catch
