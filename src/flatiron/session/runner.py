"""Trial runner: scheduler + stimuli + session file, with no UI.

The UI calls prepare() for the next trial, plays its buffer, and passes the
listener's answer to answer(), which records it, refits and saves.
"""

import secrets
import threading
from dataclasses import asdict, dataclass, field

import numpy as np

from flatiron import EARS, FS
from flatiron.apo import ApoEq
from flatiron.audio.limiter import DEFAULT_LEVEL_DB, render_pair
from flatiron.audio.stimulus import StimulusSpec, make_stimulus
from flatiron.model.refine import refine_around
from flatiron.session.scheduler import Scheduler, SchedulerConfig, TrialPlan
from flatiron.session.store import Profile, Session, TrialRecord, now_iso
from flatiron.session.sweep import Mark

MAX_SESSION_MIN = 15.0
FAST_MS = 150.0


@dataclass
class RunSettings:
    stimulus: StimulusSpec = field(default_factory=StimulusSpec)
    level_dbfs: float = DEFAULT_LEVEL_DB
    gap_s: float = 0.3
    session_minutes: float = 12.0
    active_eq: ApoEq | None = None
    eq_note: str = "none"
    eq_text: str | None = None

    def __post_init__(self):
        if not 0.15 <= self.gap_s <= 0.6:
            raise ValueError("gap must be 150 to 600 ms")
        if not 0 < self.session_minutes <= MAX_SESSION_MIN:
            raise ValueError(f"sessions are capped at {MAX_SESSION_MIN:g} minutes")


@dataclass
class PreparedTrial:
    plan: TrialPlan
    buffer: np.ndarray  # (n, 2) float32
    first_on: float
    first_off: float
    second_on: float
    second_off: float
    limited_db: float


class TrialRunner:
    def __init__(self, profile: Profile, settings: RunSettings = RunSettings(),
                 config: SchedulerConfig = SchedulerConfig(), seed: int | None = None,
                 device: dict | None = None, fs: int = FS):
        self.profile = profile
        self.settings = settings
        self.fs = fs
        self.seed = secrets.randbelow(2**31) if seed is None else seed
        hist, grids = profile.history()
        self.scheduler = Scheduler(config, seed=self.seed)
        for e in EARS:
            if len(hist[e]) or e in grids:
                self.scheduler.load_history(e, hist[e], grids.get(e))
        self.prior_trials = {e: len(hist[e]) for e in EARS}
        self.discarded = False
        self._lock = threading.Lock()
        self.session: Session = profile.new_session(
            device=device or {}, sample_rate=fs,
            eq_context={"note": settings.eq_note, "apo": settings.eq_text},
            stimulus=asdict(settings.stimulus), level_dbfs=settings.level_dbfs,
            gap_s=settings.gap_s, seed=self.seed, scheduler=asdict(config))
        self._save()

    # -- setup steps -------------------------------------------------------

    def record_channel_check(self, played: str, heard: str) -> bool:
        ok = played == heard
        self.session.channel_checks.append({"played": played, "heard": heard, "ok": ok, "at": now_iso()})
        self._save()
        return ok

    def apply_sweep_marks(self, ear: str, marks: list[Mark]) -> int:
        """Refine the ear's grid around the marks; returns points added."""
        self.session.sweep_marks[ear] = [asdict(m) for m in marks]
        added = 0
        if marks:
            old = self.scheduler.ears[ear].grid
            new = refine_around(old, [m.freq for m in marks])
            added = new.size - old.size
            if added:
                self.scheduler.seed_grid(ear, new)
        self._save()
        return added

    # -- trials ------------------------------------------------------------

    def prepare(self) -> PreparedTrial:
        plan = self.scheduler.next_trial()
        spec, s = self.settings.stimulus, self.settings
        a = make_stimulus(plan.f_a, spec, seed=self.seed, fs=self.fs)
        b = make_stimulus(plan.f_b, spec, seed=self.seed, fs=self.fs)
        la = s.level_dbfs + plan.rove_db + plan.delta_a
        lb = s.level_dbfs + plan.rove_db + plan.delta_b
        if plan.a_first:
            buf, lim = render_pair(a, b, la, lb, s.gap_s, plan.ear, self.fs)
        else:
            buf, lim = render_pair(b, a, lb, la, s.gap_s, plan.ear, self.fs)
        d = a.size / self.fs
        return PreparedTrial(plan, buf, 0.0, d, d + s.gap_s, 2 * d + s.gap_s, lim.gain_db)

    def eq_gains(self, plan: TrialPlan) -> tuple[float, float]:
        eq = self.settings.active_eq
        if eq is None:
            return 0.0, 0.0
        ga, gb = eq.gain_db([plan.f_a, plan.f_b], self.fs)
        return float(ga), float(gb)

    def answer(self, trial: PreparedTrial, first_louder: bool, rt_ms: float,
               replays: int = 0, t: float = 0.0, rt_last_ms: float | None = None) -> TrialRecord:
        """rt_ms counts from the end of B in the first presentation, so a
        quick answer after a replay isn't flagged as fast."""
        p = trial.plan
        ga, gb = self.eq_gains(p)
        rec = TrialRecord(
            ear=p.ear, f_a=p.f_a, f_b=p.f_b, delta_a=p.delta_a, delta_b=p.delta_b,
            a_first=p.a_first, rove_db=p.rove_db, catch=p.catch,
            response="first" if first_louder else "second",
            a_louder=(first_louder == p.a_first), rt_ms=float(rt_ms), replays=replays,
            fast=rt_ms < FAST_MS, eq_gain_a=ga, eq_gain_b=gb, t=round(t, 3),
            rt_last_ms=rt_ms if rt_last_ms is None else float(rt_last_ms))
        self.session.trials.append(rec)
        self._save()  # persist before the (slower) refit
        self.scheduler.record(p, rec.a_louder, known_gain_db=(ga, gb))
        return rec

    def discard(self) -> None:
        """Cancel the session: delete its file and save nothing more."""
        with self._lock:
            self.discarded = True
            self.profile.delete(self.session)
            self.session.ended = now_iso()

    def finish(self) -> None:
        """Close the session. One with no trials and no sweep marks is deleted."""
        if self.discarded:
            return
        if not self.session.trials and not self.session.sweep_marks:
            self.profile.delete(self.session)
            self.session.ended = now_iso()
            return
        self.session.ended = now_iso()
        self._save()

    # -- status ------------------------------------------------------------

    def catch_accuracy(self) -> float | None:
        c = [t for t in self.session.trials if t.catch]
        if not c:
            return None
        # Correct = the +6 dB stimulus judged louder.
        return float(np.mean([(t.offset > 0) == t.a_louder for t in c]))

    def progress(self) -> dict:
        n = len(self.session.trials)
        return {
            "session_trials": n,
            "session_trials_ear": {e: sum(t.ear == e for t in self.session.trials) for e in EARS},
            "total": {e: self.scheduler.n_trials(e) for e in EARS},
            "block_left": min(self.scheduler.until_block_end(e) for e in EARS),
            "fast": sum(t.fast for t in self.session.trials),
        }

    def _save(self) -> None:
        with self._lock:
            if self.discarded:
                return
            self.session.grid = {e: self.scheduler.ears[e].grid.tolist() for e in EARS}
            self.profile.save(self.session)
