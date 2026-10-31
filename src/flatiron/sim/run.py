"""Run the scheduler against a simulated listener and score the result."""

from dataclasses import dataclass

import numpy as np

from flatiron.audio.stimulus import stimulus_key
from flatiron.model.grid import log_grid
from flatiron.model.probit import Priors, ProbitFit
from flatiron.session.scheduler import Scheduler, SchedulerConfig
from flatiron.sim.listener import SimulatedListener

EVAL_GRID = log_grid(850, 15_000, 1 / 96)


@dataclass
class EarScore:
    rmse: float
    max_err: float
    coverage: float  # fraction of eval points within 2 SD
    feature_err: list[float]  # fitted minus true depth at each feature, relative to flanks
    n_grid: int


def run(listener: SimulatedListener, trials_per_ear: int, config: SchedulerConfig = SchedulerConfig(),
        seed: int = 0, priors: Priors = Priors()) -> tuple[Scheduler, dict[str, ProbitFit]]:
    ears = tuple(listener.ears)
    sch = Scheduler(config, seed=seed, ears=ears, priors=priors, stimulus_key=stimulus_key(listener.stimulus))
    for _ in range(trials_per_ear * len(ears)):
        plan = sch.next_trial()
        ans = listener.respond(plan.ear, plan.f_a, plan.f_b, plan.offset, plan.a_first)
        sch.record(plan, bool(ans))
    return sch, {e: sch.final_fit(e) for e in ears}


def feature_depth(freqs, level, fc, flank_oct=0.25):
    """Level at fc relative to the mean at fc·2^(±flank_oct)."""
    x = np.log2(freqs)
    at = lambda f: np.interp(np.log2(f), x, level)
    return at(fc) - 0.5 * (at(fc * 2**-flank_oct) + at(fc * 2**flank_oct))


def score(listener: SimulatedListener, ear: str, fit: ProbitFit) -> EarScore:
    f = EVAL_GRID
    truth = listener.ears[ear].level(f)
    L, sd = fit.at(f)
    # Compare shapes: remove the mean over the fitted grid from both.
    tg = listener.ears[ear].level(fit.grid)
    err = L - (truth - tg.mean())
    errs = [feature_depth(f, L, ft.fc) - feature_depth(f, truth, ft.fc)
            for ft in listener.ears[ear].features if abs(np.log2(ft.fc / 4000)) > 0.5]
    return EarScore(float(np.sqrt(np.mean(err**2))), float(np.abs(err).max()),
                    float(np.mean(np.abs(err) < 2 * sd)), errs, fit.grid.size)
