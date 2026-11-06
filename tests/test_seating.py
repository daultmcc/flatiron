"""DESIGN.md's seating criteria, at fixed seeds. Criterion 11 is only partly
met: these pin down what the Gaussian deviation does today."""

import numpy as np

from flatiron.sim.seating import moving_resonance, speed, tau_recovery


def test_criterion_9_spread_is_recovered():
    f = tau_recovery((2.0, 2.0), seed=1)
    assert f.tau_fitted and 2 / 2 < f.tau_hi < 2 * 2
    none = tau_recovery((0.0, 0.0), seed=1)
    assert none.tau_lo < 1 and none.tau_hi < 1.5  # the treble prior's median is 1.5


def test_criterion_11_a_resonance_most_seatings_have_is_corrected():
    r = moving_resonance([True, True, False], seed=3)
    assert r.accepted and r.corrected_db < -3.5


def test_criterion_11_a_resonance_one_seating_has_gets_less():
    """It should be dropped; about half the time some of it survives (DESIGN.md, "Validation")."""
    one = moving_resonance([True, False, False], seed=3)
    assert abs(one.corrected_db) < abs(moving_resonance([True, True, False], seed=3).corrected_db)


def test_criterion_12_speed():
    assert speed()["refit_ms"] < 100
