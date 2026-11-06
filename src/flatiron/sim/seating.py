"""The seating scenario (DESIGN.md, "Seating and multi-headphone scenarios", criteria 9 to 12): several
sessions through the real scheduler, with the seatings differing as they're set
to, and how the hierarchical fit and the seating step come out."""

import time
from dataclasses import dataclass, field

import numpy as np

from flatiron.audio.biquad import PeakingFilter
from flatiron.audio.stimulus import StimulusSpec, stimulus_key
from flatiron.fit.pipeline import CorrectionSettings, correct_ear
from flatiron.fit.filters import response
from flatiron.model.grid import MIN_STEP_OCT, log_grid
from flatiron.model.hierarchical import TOL_REFIT, fit_hierarchical
from flatiron.model.probit import Priors, ProbitFit, Trials
from flatiron.session.scheduler import Scheduler, SchedulerConfig
from flatiron.sim.listener import EarTruth, SimulatedListener
from flatiron.sim.profile import reseat


def run_sessions(seatings: list[EarTruth], trials_per_session: int, seed: int = 0,
                 config: SchedulerConfig = SchedulerConfig(), priors: Priors = Priors(),
                 **listener_kw) -> ProbitFit:
    """One ear through the scheduler, one session per seating (each resuming
    from the ones before, as a real session does). Returns the full fit."""
    trials, grid = session_trials(seatings, trials_per_session, seed, config, priors, **listener_kw)
    return fit_hierarchical(trials, grid, priors)


def session_trials(seatings: list[EarTruth], trials_per_session: int, seed: int = 0,
                   config: SchedulerConfig = SchedulerConfig(), priors: Priors = Priors(),
                   **listener_kw) -> tuple[Trials, np.ndarray]:
    hist: list[Trials] = []
    grid = None
    for k, truth in enumerate(seatings):
        lis = SimulatedListener({"L": truth}, seed=seed * 1000 + k, **listener_kw)
        sch = Scheduler(config, seed=seed * 1000 + k + 500, ears=("L",), priors=priors,
                        stimulus_key=stimulus_key(lis.stimulus))
        if hist:
            sch.load_history("L", Trials.grouped(hist), grid)
        for _ in range(trials_per_session):
            plan = sch.next_trial()
            sch.record(plan, bool(lis.respond("L", plan.f_a, plan.f_b, plan.offset, plan.a_first)))
        st = sch.ears["L"]
        keep = np.asarray(st.group) == st.current
        t = st.trials()
        hist.append(Trials(t.f_a[keep], t.f_b[keep], t.offset[keep], t.a_louder[keep], t.order[keep], t.stimulus[keep]))
        grid = st.grid
    return Trials.grouped(hist), grid


def tau_recovery(true_tau: tuple[float, float], seed: int, sessions: int = 3, trials: int = 110,
                 base: EarTruth = EarTruth(tilt_db_per_oct=-1.0)) -> ProbitFit:
    """Criterion 9: what's estimated from 3 × 110 trials, with a known spread."""
    rng = np.random.default_rng(seed)
    return run_sessions([reseat(base, rng, shift_oct=0.0, tau=true_tau) for _ in range(sessions)], trials, seed)


def coverage(true_tau: tuple[float, float], sessions: int, seed: int, trials: int = 110,
             base: EarTruth = EarTruth(tilt_db_per_oct=-1.0)) -> tuple[float, float]:
    """Criterion 10: how often the SD of L_avg covers its error, against the true
    average over seatings in general (the population mean, i.e. `base`), over
    the measured range away from the edges. Returns (coverage, RMS error)."""
    rng = np.random.default_rng(seed)
    fit = run_sessions([reseat(base, rng, shift_oct=0.0, tau=true_tau) for _ in range(sessions)], trials, seed)
    inside = (fit.grid >= 1000) & (fit.grid <= 13000)
    truth = base.level(fit.grid)
    err = (fit.L - fit.L[inside].mean()) - (truth - truth[inside].mean())
    # Judge the shape, error and SD alike: long jumps pin the overall tilt only weakly.
    x = np.log2(fit.grid)
    n = np.arange(fit.grid.size)[inside]
    X = np.column_stack([np.ones(n.size), x[n]])
    T = np.eye(n.size) - X @ np.linalg.solve(X.T @ X, X.T)
    err = T @ err[n]
    sd = np.sqrt(np.clip(np.diag(T @ fit.cov[np.ix_(n, n)] @ T.T), 0, None))
    return float(np.mean(np.abs(err) < sd)), float(np.sqrt(np.mean(err**2)))


@dataclass
class ResonanceResult:
    corrected_db: float  # correction at the resonance (negative: a cut)
    accepted: bool
    scale: float
    seat_spread: float | None
    tau_hi: float
    level_sd: float | None
    all_filters: list = field(default_factory=list)


def moving_resonance(present: list[bool], seed: int, gain_db: float = 9.0, fc: float = 7000.0, q: float = 6.0,
                     trials: int = 110, settings: CorrectionSettings | None = None) -> ResonanceResult:
    """Criterion 11: a resonance present only in the seatings marked."""
    rng = np.random.default_rng(seed)
    base = EarTruth(tilt_db_per_oct=-1.0)
    res = PeakingFilter(fc, gain_db, q)
    seatings = [reseat(base, rng, shift_oct=0.0, extra=(res,) if p else ()) for p in present]
    fit = run_sessions(seatings, trials, seed)
    st = settings or CorrectionSettings()
    c = correct_ear("L", fit, st)
    near = np.abs(np.log2(c.freqs / fc)) <= 1 / 6
    h = response([x for x in c.filters if x not in c.compensation], c.freqs)
    feats = [x for x in c.features if abs(np.log2(x.fc / fc)) <= 1 / 6 and x.seat_spread is not None]
    best = max(feats, key=lambda x: abs(x.peak_db), default=None)
    return ResonanceResult(float(h[near][np.argmax(np.abs(h[near]))]), any(x.accepted for x in feats),
                           best.scale if best else 0.0, best.seat_spread if best else None, fit.tau_hi,
                           best.level_sd if best else None, c.filters)


def speed() -> dict:
    """Criterion 12: a per-answer refit at 8 sessions of 150 trials on a fully
    refined 199-point grid, and the block-end noise re-estimate."""
    g = log_grid(step_oct=MIN_STEP_OCT)
    rng = np.random.default_rng(1)
    n = 8 * 150
    i = rng.integers(0, g.size - 3, n)
    j = i + rng.integers(1, 4, n)
    lis = SimulatedListener({"L": EarTruth()}, seed=1)
    off = rng.normal(0, 2, n)
    order = rng.choice([-1.0, 1.0], n)
    y = lis.respond("L", g[i], g[j], off, order > 0)
    key = stimulus_key(StimulusSpec())
    t = Trials(g[i], g[j], off, y, order, [key] * n, np.repeat(np.arange(8), 150))
    base = fit_hierarchical(t, g, fit_hyper=False, fit_tau=False)
    times = []
    for _ in range(9):
        t0 = time.perf_counter()
        fit_hierarchical(t, g, init=base, fit_hyper=False, fit_tau=False, all_groups=False, tol=TOL_REFIT)
        times.append(time.perf_counter() - t0)
    t0 = time.perf_counter()
    fit_hierarchical(t, g, init=base, fit_hyper=True, fit_tau=False, all_groups=False)
    block = time.perf_counter() - t0
    t0 = time.perf_counter()
    fit_hierarchical(t, g, init=base)
    full = time.perf_counter() - t0
    return {"refit_ms": float(np.median(times) * 1e3), "block_s": block, "full_s": full}
