import numpy as np
import pytest

from flatiron.model.grid import interp_matrix, log_grid, second_difference


def test_default_grid():
    g = log_grid()
    assert g[0] == pytest.approx(850) and g[-1] == pytest.approx(15000)
    assert 95 <= g.size <= 105
    steps = np.diff(np.log2(g))
    assert np.allclose(steps, steps[0]) and abs(steps[0] - 1 / 24) < 0.001


def test_interp_exact_at_nodes_and_rows_sum_to_one():
    g = log_grid()
    m = interp_matrix(g, g)
    assert np.allclose(m, np.eye(g.size))
    q = np.geomspace(500, 20000, 77)
    assert np.allclose(interp_matrix(g, q).sum(1), 1)


def test_interp_linear_in_log_f():
    g = np.array([1000.0, 2000.0, 4000.0])
    m = interp_matrix(g, [1000 * 2**0.25])
    assert np.allclose(m, [[0.75, 0.25, 0]])


def test_second_difference_kills_linear_trends():
    g = np.array([1000, 1100, 1300, 1400, 1800, 2500.0])
    d2, w = second_difference(g)
    x = np.log2(g)
    assert np.allclose(d2 @ (3 * x + 1), 0)
    assert np.allclose(d2 @ x**2, 2)
