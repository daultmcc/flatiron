"""Adaptive trial scheduler (DESIGN.md, "Trial" and "Adaptive selection").

Per trial: pick an ear (interleaved), score candidate pairs from the redundant
comparison graph by how much one answer would shrink the posterior variance of
the L_avg difference (weighted up where the curve bends), draw at random from
the best, and set Δ near the 50% point of the current seating's curve, with
±1 SD jitter. About 5% are catch trials. An answer is about the current seating,
L_g = L_avg + u_g, so it says less and less about L_avg once that seating is
well known at a pair.
"""

from dataclasses import dataclass, field

import numpy as np

from flatiron import EARS
from flatiron.audio.stimulus import POINT
from flatiron.model.grid import MIN_STEP_OCT, log_grid, octaves
from flatiron.model.hierarchical import TOL_REFIT, fit_hierarchical
from flatiron.model.probit import Priors, ProbitFit, Trials, noise_sd
from flatiron.model.refine import curvature_z, refine_grid

# Variance of a single probit answer at the 50% point, in units of sigma^2:
# 1 / (Fisher information) = p(1-p) / phi(0)^2 = pi/2.
PROBIT_VAR = np.pi / 2


@dataclass(frozen=True)
class SchedulerConfig:
    max_step: int = 3
    long_frac: float = 0.25
    long_max_oct: float = 1.0
    top_k: int = 8
    bend_weight: float = 0.5  # score multiplier per SD of curvature z (capped)
    bend_cap: float = 4.0
    region_gap_oct: float = 1 / 4  # don't repeat the previous trial's region
    jitter_sd: float = 1.0  # Δ jitter, in posterior SDs of the difference
    max_jitter_db: float = 4.0
    max_delta_db: float = 12.0
    catch_rate: float = 0.05
    catch_offset_db: float = 6.0
    rove_db: float = 2.0
    block_size: int = 50  # per ear: full refit (incl. noise) and refinement
    refit_every: int = 1  # per ear: fast refit of L with noise held fixed
    # Noise (sigma0, d0, lapse, treble_noise) stays at the prior until an ear has this many
    # trials; with fewer, the evidence underestimates sigma0.
    min_trials_noise_fit: int = 200
    estimate_tau: bool = True  # with enough history, estimate the seating spread when it's loaded
    refine_k: float = 2.0
    min_step_oct: float = MIN_STEP_OCT
    adaptive: bool = True  # False: uniform random graph pairs (baseline)


@dataclass
class TrialPlan:
    ear: str
    f_a: float
    f_b: float
    delta_a: float
    delta_b: float
    a_first: bool
    rove_db: float
    catch: bool

    @property
    def offset(self) -> float:
        return self.delta_a - self.delta_b


@dataclass
class EarState:
    grid: np.ndarray
    fit: ProbitFit
    f_a: list = field(default_factory=list)
    f_b: list = field(default_factory=list)
    offset: list = field(default_factory=list)
    a_louder: list = field(default_factory=list)
    order: list = field(default_factory=list)  # +1: A played first
    stimulus: list = field(default_factory=list)  # stimulus key per trial
    group: list = field(default_factory=list)  # seating per trial; earlier sessions come first
    current: int = 0  # this session's group
    since_refit: int = 0
    since_block: int = 0

    def trials(self) -> Trials:
        return Trials(self.f_a, self.f_b, self.offset, self.a_louder, self.order, self.stimulus or None,
                      self.group or None)


def candidate_pairs(grid: np.ndarray, max_step: int, long_max_oct: float):
    """All (i, j), i < j, split into short (≤ max_step steps) and long jumps."""
    i, j = np.triu_indices(grid.size, 1)
    d = octaves(grid[i], grid[j])
    short = (j - i) <= max_step
    long = ~short & (d <= long_max_oct + 1e-9)
    return (i[short], j[short]), (i[long], j[long])


class Scheduler:
    def __init__(self, config: SchedulerConfig = SchedulerConfig(), seed: int = 0,
                 ears=EARS, grid: np.ndarray | None = None, priors: Priors = Priors(),
                 stimulus_key: str = POINT):
        self.cfg = config
        self.stimulus_key = stimulus_key  # what this session's bursts are
        self.priors = priors
        self.rng = np.random.default_rng(seed)
        g = log_grid() if grid is None else np.asarray(grid, float)
        empty = Trials([], [], [], [])
        self.ears = {e: EarState(g, fit_hierarchical(empty, g, priors, fit_hyper=False, all_groups=False))
                     for e in ears}
        self._ear_cycle = list(ears)
        self._n = 0
        self._last_centre: float | None = None
        self.refinements: list[tuple[str, int, int]] = []  # (ear, trial no., points added)

    def load_history(self, ear: str, trials: Trials, grid: np.ndarray | None = None,
                     fit: ProbitFit | None = None) -> None:
        """Start from earlier sessions' trials (and their final grid), each
        session its own group. This session is a new group. fit: the history's
        full fit if it's already known (it holds the noise and the spread)."""
        st = self.ears[ear]
        if grid is not None and len(grid):
            st.grid = np.asarray(grid, float)
        st.f_a, st.f_b = list(trials.f_a), list(trials.f_b)
        st.offset, st.a_louder = list(trials.offset), list(trials.a_louder)
        st.order = list(trials.order)
        st.stimulus = list(trials.stimulus)
        st.group = list(trials.group)
        st.current = int(trials.group.max()) + 1 if len(trials) else 0
        if fit is None:
            fit = fit_hierarchical(trials, st.grid, self.priors, all_groups=False,
                                   fit_hyper=len(trials) >= self.cfg.min_trials_noise_fit,
                                   fit_tau=self.cfg.estimate_tau)
        st.fit = self._refit(st, init=fit)
        st.since_refit = st.since_block = 0

    def _refit(self, st: EarState, grid: np.ndarray | None = None, init: ProbitFit | None = None,
               **kw) -> ProbitFit:
        """L_avg and the deviations, focused on the current group. By default
        the noise and the spread stay as they are: the fast path after an answer."""
        return fit_hierarchical(st.trials(), st.grid if grid is None else grid, self.priors,
                                init=st.fit if init is None else init, n_groups=st.current + 1,
                                **{"fit_hyper": False, "fit_tau": False, "all_groups": False, **kw})

    def seed_grid(self, ear: str, grid: np.ndarray) -> None:
        """Replace an ear's grid (e.g. refined around sweep marks)."""
        st = self.ears[ear]
        st.fit = self._refit(st, grid=np.asarray(grid, float))
        st.grid = st.fit.grid

    def pop_last(self, ear: str) -> None:
        """Withdraw the ear's most recent answer (undo) and refit."""
        st = self.ears[ear]
        if not st.f_a:
            return
        for lst in (st.f_a, st.f_b, st.offset, st.a_louder, st.order, st.stimulus, st.group):
            lst.pop()
        st.since_block = max(st.since_block - 1, 0)
        st.since_refit = 0
        st.fit = self._refit(st)

    def n_trials(self, ear: str) -> int:
        return len(self.ears[ear].f_a)

    def until_block_end(self, ear: str) -> int:
        return self.cfg.block_size - self.ears[ear].since_block

    # -- selection ---------------------------------------------------------

    def next_trial(self) -> TrialPlan:
        ear = self._ear_cycle[self._n % len(self._ear_cycle)]
        self._n += 1
        st = self.ears[ear]
        cfg = self.cfg
        g = st.grid

        if self.rng.random() < cfg.catch_rate:
            f = float(self.rng.choice(g))
            return self._plan(ear, f, f, float(self.rng.choice([-1, 1]) * cfg.catch_offset_db), True)

        (si, sj), (li, lj) = candidate_pairs(g, cfg.max_step, cfg.long_max_oct)
        i, j = (li, lj) if (li.size and self.rng.random() < cfg.long_frac) else (si, sj)

        if cfg.adaptive:
            fit = st.fit
            # One answer about the L_g difference shrinks the variance of the
            # L_avg difference by their covariance² / (Var(L_g difference) + noise).
            c, cx = fit.focus_cov, fit.focus_cross
            v = c[i, i] + c[j, j] - 2 * c[i, j]
            shared = cx[i, i] + cx[j, j] - cx[i, j] - cx[j, i]
            noise = PROBIT_VAR * noise_sd(g[i], g[j], fit.sigma0, fit.d0, fit.treble_noise) ** 2
            score = shared * shared / (v + noise)
            z = curvature_z(fit)
            score = score * (1 + cfg.bend_weight * np.minimum(np.maximum(z[i], z[j]), cfg.bend_cap))
            if self._last_centre is not None:
                centre = 0.5 * (np.log2(g[i]) + np.log2(g[j]))
                far = np.abs(centre - self._last_centre) >= cfg.region_gap_oct
                if far.any():
                    i, j, score = i[far], j[far], score[far]
            top = np.argsort(score)[-cfg.top_k:]
            k = int(self.rng.choice(top))
        else:
            k = int(self.rng.integers(i.size))
        a, b = (i[k], j[k]) if self.rng.random() < 0.5 else (j[k], i[k])

        a_first = bool(self.rng.random() < 0.5)
        if cfg.adaptive:
            fit = st.fit
            c = fit.focus_cov
            sd = float(np.sqrt(max(c[a, a] + c[b, b] - 2 * c[a, b], 0.0)))
            jitter = np.clip(self.rng.normal() * cfg.jitter_sd * sd, -cfg.max_jitter_db, cfg.max_jitter_db)
            # Aim at the 50% point as the listener hears it: this seating's
            # curve, time-order bias included.
            Lg = fit.L + fit.u[st.current]
            off = -(Lg[a] - Lg[b]) - fit.bias_db * (1 if a_first else -1) + jitter
        else:
            off = self.rng.uniform(-6, 6)
        off = float(np.clip(off, -cfg.max_delta_db, cfg.max_delta_db))
        self._last_centre = 0.5 * (np.log2(g[a]) + np.log2(g[b]))
        return self._plan(ear, float(g[a]), float(g[b]), off, False, a_first)

    def _plan(self, ear, f_a, f_b, offset, catch, a_first: bool | None = None) -> TrialPlan:
        # Put the offset on one randomly chosen stimulus.
        if self.rng.random() < 0.5:
            da, db = offset, 0.0
        else:
            da, db = 0.0, -offset
        rove = float(self.rng.uniform(-self.cfg.rove_db, self.cfg.rove_db))
        if a_first is None:
            a_first = bool(self.rng.random() < 0.5)
        return TrialPlan(ear, f_a, f_b, da, db, a_first, rove, catch)

    # -- updates -----------------------------------------------------------

    def record(self, plan: TrialPlan, a_louder: bool, known_gain_db: tuple[float, float] = (0.0, 0.0)) -> None:
        """Add an answer. known_gain_db = EQ gain at (f_a, f_b) in effect for this trial."""
        st = self.ears[plan.ear]
        st.f_a.append(plan.f_a)
        st.f_b.append(plan.f_b)
        st.offset.append(plan.offset + known_gain_db[0] - known_gain_db[1])
        st.a_louder.append(bool(a_louder))
        st.order.append(1.0 if plan.a_first else -1.0)
        st.stimulus.append(self.stimulus_key)
        st.group.append(st.current)
        st.since_refit += 1
        st.since_block += 1
        if st.since_block >= self.cfg.block_size:
            # Every ear's block ends together: ears alternate, so ending only
            # this one would put the other's (slow) block-end refit on the very
            # next trial, a second pause in a row.
            for e, other in self.ears.items():
                if other.since_block:
                    self._end_block(e)
        elif st.since_refit >= self.cfg.refit_every and self.cfg.adaptive:
            st.fit = self._refit(st, tol=TOL_REFIT)
            st.since_refit = 0

    def _end_block(self, ear: str) -> None:
        st = self.ears[ear]
        st.since_refit = st.since_block = 0
        if not self.cfg.adaptive:
            return  # uniform pairs use no fit and no refinement (final_fit fits at the end)
        # The noise is re-estimated with the spread fixed: the spread is
        # re-estimated when a fit of all the trials is next wanted.
        st.fit = self._refit(st, fit_hyper=len(st.f_a) >= self.cfg.min_trials_noise_fit)
        new = refine_grid(st.fit, self.cfg.refine_k, self.cfg.min_step_oct)
        if new.size > st.grid.size:
            self.refinements.append((ear, len(st.f_a), new.size - st.grid.size))
            st.grid = new
            st.fit = self._refit(st)

    def final_fit(self, ear: str) -> ProbitFit:
        st = self.ears[ear]
        return fit_hierarchical(st.trials(), st.grid, self.priors, init=st.fit)
