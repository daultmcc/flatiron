"""Cross-ear trials (DESIGN.md, "Cross-ear trials").

Now and then a trial plays A in one ear and B in the other, at the same
frequency or a few grid steps apart, with the usual "which was louder?". They
measure what the within-ear trials can't: the overall level difference
between the ears, and the L−R shape directly. For now they're only collected:
no fit uses them.

The planner aims each trial near the 50% point as well as it can: the
difference of the two ears' current curves (each with its mean over 1 to
13 kHz, within the measured range, removed, since each ear's level is its
own gauge), plus a running estimate of the overall L−R offset from the
cross answers so far. With loudness matching, each burst is matched in its
own ear, as the scheduler's are.
"""

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import log_ndtr

from flatiron.model.grid import interp_matrix, log_grid
from flatiron.model.probit import band_matrix
from flatiron.session.scheduler import Scheduler, TrialPlan

F_LO, F_HI = 1000.0, 13000.0  # where cross-ear frequencies are drawn and the curves centred (within the measured range)
SAME_FREQ_SHARE = 0.5  # the rest are 1 to 3 grid steps (1/24 octave each) apart
MAX_STEPS = 3
SIGMA_X_DB = 1.5  # assumed judgment noise of a cross-ear pair, for aiming only
OFFSET_PRIOR_SD = 3.0  # prior SD of the overall L−R offset, for aiming only
JITTER_SD, MAX_JITTER = 2.0, 4.0


def cross_range(measured: tuple[float, float]) -> tuple[float, float]:
    """1 to 13 kHz, within the measured range (all of it if they don't overlap)."""
    lo, hi = max(F_LO, measured[0]), min(F_HI, measured[1])
    return (lo, hi) if hi > lo else (float(measured[0]), float(measured[1]))


def centred_curve(fit, centre: np.ndarray, group: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """An ear's curve on its grid (a group's own, if given and known), shifted
    so it averages 0 over the centre frequencies."""
    curve = fit.L if group is None or fit.u is None or group >= len(fit.u) else fit.L + fit.u[group]
    return fit.grid, curve - float(np.mean(interp_matrix(fit.grid, centre) @ curve))


class CrossPlanner:
    """Plans cross-ear trials against a session's scheduler.

    history: earlier sessions' cross-ear trials (TrialRecord), whose answers
    start the offset estimate."""

    def __init__(self, scheduler: Scheduler, rate: float, seed: int, history=()):
        if not 0 <= rate <= 1:
            raise ValueError("the cross-ear rate must be 0 to 1")
        self.sched = scheduler
        self.rate = rate
        self.f_lo, self.f_hi = cross_range(scheduler.measured)
        self.centre = log_grid(self.f_lo, self.f_hi)
        self.rng = np.random.default_rng([seed, 0xC205])
        # Per answer: the known part of the probit argument (curve difference,
        # offset, time-order bias), the sign of the L−R offset in it (+1: A in
        # the left ear), and the answer.
        self.known: list[float] = []
        self.sign: list[float] = []
        self.a_louder: list[bool] = []
        for t in history:
            self._add(t.ear, t.f_a, t.f_b, t.offset, t.a_first, t.a_louder, current=False)

    @property
    def active(self) -> bool:
        return self.rate > 0 and set(self.sched.ears) == {"L", "R"}

    def due(self) -> bool:
        return self.active and bool(self.rng.random() < self.rate)

    # -- the model's view --------------------------------------------------

    def _level(self, ear: str, f: float, current: bool) -> float:
        st = self.sched.ears[ear]
        grid, curve = centred_curve(st.fit, self.centre, st.current if current else None)
        return float(band_matrix(grid, [f], [self.sched.stimulus_key])[0] @ curve)

    def _bias(self) -> float:
        return float(np.mean([st.fit.bias_db for st in self.sched.ears.values()]))

    def offset_estimate(self) -> float:
        """MAP of the overall L−R level difference (dB) from the cross answers."""
        if not self.known:
            return 0.0
        k, s = np.asarray(self.known), np.asarray(self.sign)
        y = np.where(self.a_louder, 1.0, -1.0)

        def nlp(c):
            return -np.sum(log_ndtr(y * (k + s * c) / SIGMA_X_DB)) + 0.5 * (c / OFFSET_PRIOR_SD) ** 2

        return float(minimize_scalar(nlp, bounds=(-20, 20), method="bounded").x)

    def _add(self, ear, f_a, f_b, offset, a_first, a_louder, current=True):
        diff = self._level(ear[0], f_a, current) - self._level(ear[1], f_b, current)
        self.known.append(diff + offset + self._bias() * (1 if a_first else -1))
        self.sign.append(1.0 if ear[0] == "L" else -1.0)
        self.a_louder.append(bool(a_louder))

    # -- planning ----------------------------------------------------------

    def next_trial(self) -> TrialPlan:
        rng = self.rng
        f_a = float(2 ** rng.uniform(np.log2(self.f_lo), np.log2(self.f_hi)))
        f_b = f_a
        if rng.random() >= SAME_FREQ_SHARE:
            f_b = f_a * 2 ** (rng.choice([-1, 1]) * rng.integers(1, MAX_STEPS + 1) / 24)
        ear = "LR" if rng.random() < 0.5 else "RL"
        a_first = bool(rng.random() < 0.5)
        c = self.offset_estimate() * (1 if ear[0] == "L" else -1)
        diff = self._level(ear[0], f_a, True) - self._level(ear[1], f_b, True)
        jitter = float(np.clip(rng.normal() * JITTER_SD, -MAX_JITTER, MAX_JITTER))
        off = -(diff + c) - self._bias() * (1 if a_first else -1) + jitter
        off = float(np.clip(off, -self.sched.cfg.max_delta_db, self.sched.cfg.max_delta_db))
        cfg = self.sched.cfg
        coin = rng.random() < 0.5  # drawn either way, so a seed gives the same trials with matching on or off
        if cfg.match:
            # As the scheduler does (DESIGN.md, "Loudness matching"), each burst
            # matched in its own ear: the offset split evenly around the pair's
            # matched level, then both moved together to respect the caps.
            common = 0.5 * float(self.sched.match_gain(ear[0], f_a) + self.sched.match_gain(ear[1], f_b))
            da, db = common + off / 2, common - off / 2
            shift = min(cfg.match_boost_db - max(da, db), 0.0) + max(-cfg.match_cut_db - min(da, db), 0.0)
            da, db = da + shift, db + shift
        else:
            da, db = (off, 0.0) if coin else (0.0, -off)
        rove = float(rng.uniform(-self.sched.cfg.rove_db, self.sched.cfg.rove_db))
        return TrialPlan(ear, f_a, f_b, da, db, a_first, rove, False)

    def record(self, plan: TrialPlan, a_louder: bool, known_gain_db=(0.0, 0.0)) -> None:
        self._add(plan.ear, plan.f_a, plan.f_b, plan.offset + known_gain_db[0] - known_gain_db[1],
                  plan.a_first, a_louder)

    def pop_last(self) -> None:
        for lst in (self.known, self.sign, self.a_louder):
            if lst:
                lst.pop()
