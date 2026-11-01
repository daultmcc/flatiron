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
from flatiron.audio.preview import PreviewEQ
from flatiron.audio.limiter import DEFAULT_LEVEL_DB, render_pair
from flatiron.audio.stimulus import StimulusSpec, make_stimulus, stimulus_key
from flatiron.model.refine import refine_around
from flatiron.session.scheduler import Scheduler, SchedulerConfig, TrialPlan
from flatiron.session.store import Profile, Session, TrialRecord, now_iso
from flatiron.session.sweep import Mark

MAX_SESSION_MIN = 15.0
FAST_MS = 150.0


@dataclass
class RunSettings:
    stimulus: StimulusSpec = field(default_factory=StimulusSpec)
    level_dbfs: float | None = None  # starting level; None: the last session's
    gap_s: float = 0.3
    session_minutes: float = 12.0
    active_eq: ApoEq | None = None
    eq_note: str = "none"
    eq_text: str | None = None
    # Verify sessions: Flatiron applies this correction to every burst and
    # records each burst's level change as a known offset.
    preview: PreviewEQ | None = None
    max_trials: int | None = None  # end the session after this many answers

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
    level_dbfs: float
    retake: bool = False
    preview_gain_a: float = 0.0  # level change of each burst from the preview EQ (dB)
    preview_gain_b: float = 0.0


class TrialRunner:
    def __init__(self, profile: Profile, settings: RunSettings = RunSettings(),
                 config: SchedulerConfig = SchedulerConfig(), seed: int | None = None,
                 device: dict | None = None, fs: int = FS):
        self.profile = profile
        self.settings = settings
        self.fs = fs
        self.seed = secrets.randbelow(2**31) if seed is None else seed
        hist, grids = profile.history()
        self.scheduler = Scheduler(config, seed=self.seed, stimulus_key=stimulus_key(settings.stimulus))
        for e in EARS:
            if len(hist[e]) or e in grids:
                self.scheduler.load_history(e, hist[e], grids.get(e))
        self.prior_trials = {e: len(hist[e]) for e in EARS}
        self.discarded = False
        self._lock = threading.Lock()
        self._last_answered: PreparedTrial | None = None
        start = settings.level_dbfs
        if start is None:
            start = profile.last_level()
        self.level_dbfs = float(DEFAULT_LEVEL_DB if start is None else start)
        self.session: Session = profile.new_session(
            device=device or {}, sample_rate=fs,
            eq_context={"note": settings.eq_note, "apo": settings.eq_text},
            stimulus=asdict(settings.stimulus), level_dbfs=self.level_dbfs,
            gap_s=settings.gap_s, seed=self.seed, scheduler=asdict(config),
            preview=settings.preview.to_json() if settings.preview is not None else None)
        self._apply_tone_marks()
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

    def _apply_tone_marks(self) -> None:
        """Refine the grids around tone-generator marks no session has used
        yet (a mark whose session was deleted, e.g. left before any trials,
        counts as unused)."""
        marks = self.profile.marks()
        existing = {s.id for s in self.profile.sessions()}
        pending = [m for m in marks if m.get("session") not in existing]
        if not pending:
            return
        for e in EARS:
            fs = [m["freq"] for m in pending if e in m["ear"]]
            if fs:
                old = self.scheduler.ears[e].grid
                new = refine_around(old, fs)
                if new.size > old.size:
                    self.scheduler.seed_grid(e, new)
        for m in pending:
            m["session"] = self.session.id
        self.session.tone_marks = pending
        self.profile.update(marks=marks)

    def set_sweep_eq(self, fit_hash: str | None) -> None:
        self.session.sweep_eq = fit_hash
        self._save()

    # -- trials ------------------------------------------------------------

    def set_level(self, level_dbfs: float) -> None:
        """Change the presentation level. Before any trials it just sets the
        session's level; later changes are logged with the trial count."""
        level_dbfs = float(level_dbfs)
        n = len(self.live_trials())
        if n == 0:
            self.session.level_dbfs = level_dbfs
        elif level_dbfs != self.level_dbfs:
            self.session.level_changes.append({"after_trial": n, "from": self.level_dbfs,
                                               "to": level_dbfs, "at": now_iso()})
        self.level_dbfs = level_dbfs
        self._save()

    def prepare(self) -> PreparedTrial:
        return self.render(self.scheduler.next_trial())

    def render(self, plan: TrialPlan, retake: bool = False) -> PreparedTrial:
        """Synthesize a plan's buffer at the current level."""
        spec, s = self.settings.stimulus, self.settings
        a = make_stimulus(plan.f_a, spec, seed=self.seed, fs=self.fs)
        b = make_stimulus(plan.f_b, spec, seed=self.seed, fs=self.fs)
        pa = pb = 0.0
        if s.preview is not None:
            a, pa = s.preview.burst_gain_db(a, plan.ear, self.fs)
            b, pb = s.preview.burst_gain_db(b, plan.ear, self.fs)
        la = self.level_dbfs + plan.rove_db + plan.delta_a
        lb = self.level_dbfs + plan.rove_db + plan.delta_b
        if plan.a_first:
            buf, lim = render_pair(a, b, la, lb, s.gap_s, plan.ear, self.fs)
        else:
            buf, lim = render_pair(b, a, lb, la, s.gap_s, plan.ear, self.fs)
        d = a.size / self.fs
        return PreparedTrial(plan, buf, 0.0, d, d + s.gap_s, 2 * d + s.gap_s, lim.gain_db,
                             self.level_dbfs, retake, pa, pb)

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
        ga, gb = ga + trial.preview_gain_a, gb + trial.preview_gain_b
        rec = TrialRecord(
            ear=p.ear, f_a=p.f_a, f_b=p.f_b, delta_a=p.delta_a, delta_b=p.delta_b,
            a_first=p.a_first, rove_db=p.rove_db, catch=p.catch,
            response="first" if first_louder else "second",
            a_louder=(first_louder == p.a_first), rt_ms=float(rt_ms), replays=replays,
            fast=rt_ms < FAST_MS, eq_gain_a=ga, eq_gain_b=gb, t=round(t, 3),
            rt_last_ms=rt_ms if rt_last_ms is None else float(rt_last_ms),
            level_dbfs=trial.level_dbfs, retake=trial.retake)
        self.session.trials.append(rec)
        self._save()  # persist before the (slower) refit
        self.scheduler.record(p, rec.a_louder, known_gain_db=(ga, gb))
        self._last_answered = trial
        return rec

    def can_undo(self) -> bool:
        return self._last_answered is not None

    def undo_last(self) -> PreparedTrial | None:
        """Withdraw the last answer (one step) and return its trial to present
        again. The answer stays in the session file, marked undone."""
        trial = self._last_answered
        if trial is None:
            return None
        rec = self.live_trials()[-1]
        rec.undone = True
        self.scheduler.pop_last(rec.ear)
        self._last_answered = None
        self._save()
        if trial.level_dbfs != self.level_dbfs:
            return self.render(trial.plan, retake=True)
        trial.retake = True
        return trial

    def live_trials(self) -> list[TrialRecord]:
        return [t for t in self.session.trials if not t.undone]

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
        c = [t for t in self.live_trials() if t.catch]
        if not c:
            return None
        # Correct = the +6 dB stimulus judged louder.
        return float(np.mean([(t.offset > 0) == t.a_louder for t in c]))

    def progress(self) -> dict:
        live = self.live_trials()
        return {
            "session_trials": len(live),
            "session_trials_ear": {e: sum(t.ear == e for t in live) for e in EARS},
            "total": {e: self.scheduler.n_trials(e) for e in EARS},
            "block_left": min(self.scheduler.until_block_end(e) for e in EARS),
            "fast": sum(t.fast for t in live),
            "undone": len(self.session.trials) - len(live),
        }

    def _save(self) -> None:
        with self._lock:
            if self.discarded:
                return
            self.session.grid = {e: self.scheduler.ears[e].grid.tolist() for e in EARS}
            self.profile.save(self.session)
