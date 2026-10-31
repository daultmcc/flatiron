import numpy as np
import pytest

from flatiron.model.grid import MIN_STEP_OCT, log_grid, octaves
from flatiron.model.probit import Priors, Trials, fit_probit
from flatiron.model.refine import curvature_z, refine_grid
from flatiron.session.scheduler import Scheduler, SchedulerConfig
from flatiron.sim.listener import EarTruth, SimulatedListener, design_example
from flatiron.sim.run import run, score


def test_plans_follow_the_design():
    sch = Scheduler(seed=1)
    plans = []
    for _ in range(400):
        p = sch.next_trial()
        plans.append(p)
        sch.record(p, bool(sch.rng.random() < 0.5))
    ears = [p.ear for p in plans]
    assert all(a != b for a, b in zip(ears, ears[1:]))  # ears interleaved
    catch = [p for p in plans if p.catch]
    assert 5 <= len(catch) <= 40
    assert all(p.f_a == p.f_b and abs(p.offset) == 6 for p in catch)
    normal = [p for p in plans if not p.catch]
    assert all(0 < octaves(p.f_a, p.f_b) <= 1 + 1e-9 for p in normal)
    assert all(p.delta_a == 0 or p.delta_b == 0 for p in plans)
    assert all(abs(p.offset) <= 12 and abs(p.rove_db) <= 2 for p in plans)
    assert 0.3 < np.mean([p.a_first for p in plans]) < 0.7
    g = sch.ears["L"].grid
    steps = [abs(np.searchsorted(g, p.f_a) - np.searchsorted(g, p.f_b)) for p in normal if p.ear == "L"]
    assert 0.1 < np.mean(np.array(steps) > 3) < 0.4  # ~25% long jumps


def test_consecutive_trials_move_region():
    sch = Scheduler(seed=2)
    centres = []
    for _ in range(100):
        p = sch.next_trial()
        sch.record(p, True)
        if not p.catch:
            centres.append(0.5 * np.log2(p.f_a * p.f_b))
    gaps = np.abs(np.diff(centres))
    assert np.mean(gaps >= 0.25 - 1e-9) > 0.9


def test_known_gain_enters_offset():
    sch = Scheduler(seed=0, ears=("L",))
    p = sch.next_trial()
    sch.record(p, True, known_gain_db=(-3.0, 1.0))
    assert sch.ears["L"].offset[-1] == pytest.approx(p.offset - 4.0)


def test_refinement_adds_midpoints_only_at_bends():
    g = log_grid()
    truth = design_example()
    lis = SimulatedListener({"L": truth}, seed=0)
    rng = np.random.default_rng(0)
    i = rng.integers(0, g.size - 1, 3000)
    j = i + rng.integers(1, 4, 3000).clip(max=g.size - 1 - i)
    off = -(truth.level(g[i]) - truth.level(g[j])) + rng.normal(0, 1, i.size)
    fit = fit_probit(Trials(g[i], g[j], off, lis.respond("L", g[i], g[j], off)), g)
    new = refine_grid(fit)
    added = np.setdiff1d(new, g)
    assert added.size > 0
    assert np.min(np.diff(np.log2(new))) >= MIN_STEP_OCT * 0.999
    # Every new point sits next to a bend, and the narrow notch got one.
    z = curvature_z(fit)
    for f in added:
        k = np.searchsorted(g, f)
        assert max(z[k - 1], z[k]) > 2
    assert np.any(octaves(added, 8400) < 1 / 24)
    # Already at the minimum spacing: nothing more to add.
    fine = log_grid(6000, 7000, MIN_STEP_OCT)
    assert refine_grid(fit_probit(Trials(fine[:-1], fine[1:], np.zeros(fine.size - 1),
                                         np.ones(fine.size - 1)), fine), k=0).size == fine.size


@pytest.fixture(scope="module")
def adaptive_vs_uniform():
    out = {}
    for name, cfg in [("adaptive", SchedulerConfig()), ("uniform", SchedulerConfig(adaptive=False))]:
        lis = SimulatedListener({"L": design_example()}, seed=100)
        sch, fits = run(lis, 600, cfg, seed=0)
        out[name] = (fits["L"], score(lis, "L", fits["L"]), sch)
    return out


def test_adaptive_recovers_noise_and_curve(adaptive_vs_uniform):
    fit, sc, sch = adaptive_vs_uniform["adaptive"]
    assert 0.7 < fit.sigma0 < 1.4 and fit.lapse < 0.08
    assert sc.rmse < 1.2
    assert sc.coverage > 0.8


def test_adaptive_beats_uniform(adaptive_vs_uniform):
    a = adaptive_vs_uniform["adaptive"][1]
    u = adaptive_vs_uniform["uniform"][1]
    assert a.rmse < 0.8 * u.rmse



def test_scheduler_compensates_for_time_order_bias():
    lis = SimulatedListener({"L": design_example()}, seed=8, order_bias_db=1.5)
    sch, fits = run(lis, 400, SchedulerConfig(), seed=8)
    st = sch.ears["L"]
    assert abs(fits["L"].bias_db - 1.5) < 0.6
    # Once the bias is known, Δ is aimed at the 50% point as heard, so the
    # listener stops favouring the first interval.
    order, a_louder = np.array(st.order[200:]), np.array(st.a_louder[200:])
    chose_first = np.where(order > 0, a_louder, ~a_louder)
    assert abs(chose_first.mean() - 0.5) < 0.08
