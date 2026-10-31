"""Write a simulated listener's sessions into a profile, through the real
TrialRunner, so fitting and reports can be exercised end to end."""

from dataclasses import replace

import numpy as np

from flatiron import EARS
from flatiron.audio.biquad import PeakingFilter
from flatiron.session.runner import RunSettings, TrialRunner
from flatiron.session.scheduler import SchedulerConfig
from flatiron.session.store import Profile
from flatiron.sim.listener import EarTruth, SimulatedListener


def reseat(truth: EarTruth, rng: np.random.Generator, shift_oct: float = 1 / 48,
           fragile: tuple[float, ...] = ()) -> EarTruth:
    """Features move by up to ±shift_oct on each reseat. Features at the
    frequencies in `fragile` move a lot more (±1/6 octave): a fit-dependent
    resonance the consistency check should drop."""
    feats = []
    for f in truth.features:
        s = 1 / 6 if any(abs(np.log2(f.fc / x)) < 0.01 for x in fragile) else shift_oct
        feats.append(PeakingFilter(f.fc * 2 ** rng.uniform(-s, s), f.gain_db, f.q))
    return replace(truth, features=feats)


def simulate_profile(profile: Profile, truths: dict[str, EarTruth], sessions: int = 3,
                     trials_per_ear: int = 200, seed: int = 0, fragile: tuple[float, ...] = (),
                     config: SchedulerConfig = SchedulerConfig(), **listener_kw) -> None:
    rng = np.random.default_rng(seed)
    for k in range(sessions):
        lis = SimulatedListener({e: reseat(truths[e], rng, fragile=fragile) for e in EARS},
                                seed=seed * 100 + k, **listener_kw)
        r = TrialRunner(profile, RunSettings(), config, seed=seed * 100 + k)
        for _ in range(trials_per_ear * len(EARS)):
            tr = r.prepare()
            p = tr.plan
            a = bool(lis.respond(p.ear, p.f_a, p.f_b, p.offset))
            r.answer(tr, first_louder=(a == p.a_first), rt_ms=600.0)
        r.finish()
