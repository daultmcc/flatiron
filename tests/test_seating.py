"""DESIGN.md's seating criteria, at fixed seeds. Criterion 11 is only partly
met: these pin down what the Gaussian deviation does today."""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from flatiron.sim.seating import moving_resonance, speed, tau_recovery


def test_criterion_9_spread_is_recovered():
    f = tau_recovery((2.0, 2.0), seed=3)
    assert f.tau_fitted and 2 / 2 < f.tau_hi < 2 * 2
    none = tau_recovery((0.0, 0.0), seed=3)
    assert none.tau_lo < 1 and none.tau_hi < 1.5  # the treble prior's median is 1.5


def test_criterion_11_a_resonance_most_seatings_have_is_corrected():
    r = moving_resonance([True, True, False], seed=3)
    assert r.accepted and r.corrected_db < -3.5


def test_criterion_11_a_resonance_one_seating_has_gets_less():
    """It should be dropped; about half the time some of it survives (DESIGN.md, "Validation")."""
    # The two runs side by side: this test is otherwise the suite's longest by far.
    # Spawned, since forking a multi-threaded worker can deadlock.
    with ProcessPoolExecutor(2, mp_context=multiprocessing.get_context("spawn")) as ex:
        one, two = ex.map(moving_resonance, [[True, False, False], [True, True, False]], [3, 3])
    assert abs(one.corrected_db) < abs(two.corrected_db)


def test_criterion_12_speed():
    assert speed()["refit_ms"] < 100
