"""Threshold sessions: detection trials near the threshold of hearing
(DESIGN.md, "Threshold sessions"). Collected only: no fit uses them yet.

Per trial, one burst in one ear (drawn at random, so the listener
can't know which to attend to), at a level near the current
estimate of the threshold there. In the yes/no task the listener says whether
they heard anything in the marked interval; some trials are blank, to measure
false alarms. In the two-interval task (2IFC) the burst is in one of two
marked intervals and the listener says which: forced to guess (2ifc), or
allowed to answer "neither" (2ifc-neither), when the right interval counts as
heard. In all, some trials are probes well above threshold, to measure lapses.

Frequencies come from the profile's measured range; the grid spans it, and
any earlier threshold session's frequencies, since one fit covers them all.

Each session starts with a descending run in each ear at 2 kHz (or the
nearest end of the measured range): 10 dB down
after each positive answer (in 2IFC, after two in a row at that level, since
a guess is right half the time), until the first negative one. That finds the
session's level (the system volume, which only the session's offset knows)
before the adaptive trials start. The UI drives this like a TrialRunner:
prepare(), answer(), undo_last() and so on.
"""

import secrets
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field

import numpy as np

from flatiron import EARS, FS
from flatiron.audio.limiter import DEFAULT_LEVEL_DB, MAX_LEVEL_DB, db_to_amp, limit, monaural
from flatiron.audio.stimulus import StimulusSpec, make_stimulus, stimulus_key
from flatiron.model.grid import in_range, log_grid
from flatiron.model.threshold import ThresholdData, ThresholdFit, fit_thresholds
from flatiron.session.runner import FAST_MS, MAX_SESSION_MIN
from flatiron.session.store import THRESHOLD_TASKS, Profile, ThresholdRecord, ThresholdSession, now_iso

MIN_LEVEL_DB = -140.0  # no burst is played quieter than this (dBFS RMS)


@dataclass(frozen=True)
class ThresholdConfig:
    blank_rate: float = 0.10  # yes/no only
    probe_rate: float = 0.04
    probe_db: float = 15.0  # above the estimated threshold
    top_k: int = 8
    region_gap_oct: float = 0.25  # don't repeat the previous trial's region
    startup_freq: float = 2000.0
    startup_below_comfort_db: float = 30.0
    startup_above_known_db: float = 15.0  # with earlier sessions: start this far above their threshold
    startup_step_db: float = 10.0
    sigma_every: int = 50  # trials between re-estimates of the psychometric spread


@dataclass
class ThresholdSettings:
    stimulus: StimulusSpec = field(default_factory=StimulusSpec)
    task: str = "yesno"
    gap_s: float = 0.3
    session_minutes: float = 8.0
    max_trials: int | None = None
    debug: bool = False  # the trial screen can show each burst's frequency, level and the estimate
    tincan: object = None  # tincan_live.TinCanWatch: TinCan's gain is read at every burst

    def __post_init__(self):
        if self.task not in THRESHOLD_TASKS:
            raise ValueError(f"the task must be one of {', '.join(THRESHOLD_TASKS)}")
        if not 0 < self.session_minutes <= MAX_SESSION_MIN:
            raise ValueError(f"sessions are capped at {MAX_SESSION_MIN:g} minutes")


@dataclass
class ThresholdPlan:
    ear: str
    freq: float
    level_db: float
    kind: str
    interval: int = 1  # 2ifc: which interval holds the burst
    eq_gain: float = 0.0  # known EQ gain at the burst (TinCan's), set when it's rendered


@dataclass
class PreparedThreshold:
    plan: ThresholdPlan
    buffer: np.ndarray
    first_on: float
    first_off: float
    second_on: float
    second_off: float
    level_dbfs: float  # the session's listening level (the UI compares it, as for a trial)
    retake: bool = False
    # For the debug readout: the burst's level as played (dBFS RMS, after the
    # limiter; a blank's is what it would have been), and this session's
    # threshold estimate at its frequency, with its SD, when it was planned.
    played_db: float = 0.0
    estimate_db: float = 0.0
    estimate_sd: float = 0.0


def system_volume() -> str | None:
    """The default sink's volume as `wpctl` reports it (Linux, PipeWire), or None."""
    if shutil.which("wpctl") is None:
        return None
    try:
        out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True,
                             text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


class ThresholdRunner:
    threshold = True  # the UI's flag for this kind of session
    speaker = None

    @property
    def two_interval(self) -> bool:
        return self.task != "yesno"

    def __init__(self, profile: Profile, settings: ThresholdSettings = ThresholdSettings(),
                 config: ThresholdConfig = ThresholdConfig(), seed: int | None = None,
                 device: dict | None = None, fs: int = FS, volume: bool = True):
        self.profile, self.settings, self.cfg, self.fs = profile, settings, config, fs
        self.seed = secrets.randbelow(2**31) if seed is None else seed
        self.rng = np.random.default_rng(self.seed)
        self.task = settings.task
        self.key = stimulus_key(settings.stimulus)
        self.earlier = profile.threshold_sessions()
        self.measured = profile.measured
        lo, hi = self.measured
        seen = [t.freq for s in self.earlier for t in s.trials]
        self.grid = log_grid(min([lo, *seen]), max([hi, *seen]))
        inside = np.flatnonzero(in_range(self.grid, lo, hi))
        self.inside = inside if inside.size >= 2 else np.arange(self.grid.size)  # where trials may go
        self.startup_freq = float(np.clip(config.startup_freq, lo, hi))
        self.index = len(self.earlier)  # this session's offset in the fit
        comfort = profile.last_level()
        self.level_dbfs = float(DEFAULT_LEVEL_DB if comfort is None else comfort)
        self.session = profile.new_threshold_session(
            task=self.task, device=device or {}, sample_rate=fs, stimulus=asdict(settings.stimulus),
            comfort_dbfs=self.level_dbfs, gap_s=settings.gap_s, seed=self.seed,
            system_volume=system_volume() if volume else None, grid=self.grid.tolist(),
            measured=list(self.measured))
        self.discarded = False
        self._lock = threading.Lock()
        from flatiron.tincan_live import SessionEq

        self.tincan = SessionEq(settings.tincan, self.key)
        self._last: tuple[PreparedThreshold, dict] | None = None
        self._last_freq: float | None = None
        self.fit = self._fit()
        # The descending run: each ear's next level, or None once it's done.
        start = self.level_dbfs - config.startup_below_comfort_db
        if self.earlier:
            # The last session's offset is the best guess at this one's.
            known = {e: float(self.fit.level(e, self.startup_freq, self.index - 1, self.key)[0][0])
                     for e in EARS}
            start = min(start, max(known.values()) + config.startup_above_known_db)
        self.startup: dict[str, float | None] = {e: start for e in EARS}
        self._save()

    # -- the model ---------------------------------------------------------

    def data(self) -> ThresholdData:
        parts = [(s.task, s.stimulus_key(), s.trials) for s in self.earlier]
        parts.append((self.task, self.key, self.session.trials))
        return ThresholdData.build(parts)

    def _fit(self, fit_sigma: bool = False) -> ThresholdFit:
        init = getattr(self, "fit", None)
        return fit_thresholds(self.data(), self.grid, init=init,
                              sigma=None if fit_sigma or init is None else init.sigma,
                              fit_sigma=None if init is None else fit_sigma)

    def estimate(self, ear: str, freqs) -> tuple[np.ndarray, np.ndarray]:
        """This session's threshold (dBFS) at the frequencies, and its SD."""
        return self.fit.level(ear, freqs, self.index, self.key)

    # -- planning ----------------------------------------------------------

    def next_plan(self) -> ThresholdPlan:
        cfg, rng = self.cfg, self.rng
        ear = EARS[int(rng.integers(2))]  # at random: no pattern to track
        interval = int(rng.integers(1, 3)) if self.two_interval else 1
        level = self.startup[ear]
        if level is not None:
            return ThresholdPlan(ear, self.startup_freq, level, "startup", interval)
        g = self.grid[self.inside]
        mean, sd = self.estimate(ear, g)
        ok = np.ones(g.size, bool)
        if self._last_freq is not None:
            far = np.abs(np.log2(g / self._last_freq)) >= cfg.region_gap_oct
            ok = far if far.any() else ok
        cand = np.flatnonzero(ok)
        top = cand[np.argsort(sd[cand])[-cfg.top_k:]]
        i = int(rng.choice(top))
        f = float(g[i])
        self._last_freq = f
        r = rng.random()
        blank = self.task == "yesno" and r < cfg.blank_rate
        probe = not blank and r < cfg.blank_rate * (self.task == "yesno") + cfg.probe_rate
        if probe:
            x = mean[i] + cfg.probe_db
        else:
            # Around the threshold, so the answers also say how steep the
            # psychometric function is; a little above it for forced 2IFC,
            # whose midpoint is 75% correct.
            x = mean[i] + self.fit.sigma * (rng.normal() + (0.5 if self.task == "2ifc" else 0.0))
        x -= self.tincan.gain(f, ear)  # the estimate is of what reaches the headphones, EQ included
        x = float(np.clip(x, MIN_LEVEL_DB, MAX_LEVEL_DB))
        return ThresholdPlan(ear, f, x, "blank" if blank else "probe" if probe else "signal", interval)

    def render(self, plan: ThresholdPlan, retake: bool = False) -> PreparedThreshold:
        d = self.settings.stimulus.duration
        plan.eq_gain = self.tincan.gain(plan.freq, plan.ear)
        burst = make_stimulus(plan.freq, self.settings.stimulus, seed=self.seed, fs=self.fs)
        burst = np.zeros_like(burst) if plan.kind == "blank" else burst * db_to_amp(plan.level_db)
        if self.task == "yesno":
            parts, times = [burst], (0.0, d, 0.0, d)
        else:
            g = self.settings.gap_s
            gap, quiet = np.zeros(int(round(g * self.fs))), np.zeros_like(burst)
            parts = [burst, gap, quiet] if plan.interval == 1 else [quiet, gap, burst]
            times = (0.0, d, d + g, 2 * d + g)
        lim = limit(np.concatenate(parts))
        (est,), (sd,) = self.estimate(plan.ear, [plan.freq])
        return PreparedThreshold(plan, monaural(lim.signal, plan.ear), *times, self.level_dbfs, retake,
                                 plan.level_db + lim.gain_db, float(est), float(sd))

    def prepare(self) -> PreparedThreshold:
        return self.render(self.next_plan())

    def eq_message(self) -> str | None:
        return self.tincan.message()

    # -- answers -----------------------------------------------------------

    def answer(self, trial: PreparedThreshold, first: bool | None, rt_ms: float, replays: int = 0,
               t: float = 0.0, rt_last_ms: float | None = None, peeked: bool = False) -> ThresholdRecord:
        """first: the upper answer: "heard" (yes/no) or the first interval
        (2IFC); None: "neither" (2ifc-neither only)."""
        p = trial.plan
        if first is None:
            if self.task != "2ifc-neither":
                raise ValueError(f"no \"neither\" answer in a {self.task} session")
            response, positive = "neither", False
        elif self.task == "yesno":
            response, positive = ("heard" if first else "nothing"), first
        else:
            response, positive = ("first" if first else "second"), (1 if first else 2) == p.interval
        rec = ThresholdRecord(
            ear=p.ear, freq=p.freq, level_dbfs=p.level_db, kind=p.kind, response=response, positive=positive,
            interval=p.interval, rt_ms=float(rt_ms), rt_last_ms=rt_ms if rt_last_ms is None else float(rt_last_ms),
            replays=replays, fast=rt_ms < FAST_MS, t=round(t, 3), retake=trial.retake,
            peeked=peeked, eq_gain=p.eq_gain)
        if (ctx := self.tincan.context()) is not None:
            self.session.eq_context.update(ctx)
        state = dict(self.startup)
        self.session.trials.append(rec)
        self._save()
        if p.kind == "startup":
            self._startup_step(p, positive)
        n = len(self.live_trials())
        self.fit = self._fit(fit_sigma=n % self.cfg.sigma_every == 0)
        self._last = (trial, state)
        return rec

    def _startup_step(self, p: ThresholdPlan, positive: bool) -> None:
        if not positive:
            self.startup[p.ear] = None
            return
        if self.task == "2ifc":
            # A level passes after two right answers in a row there (a guess
            # is right half the time; with "neither" one is enough).
            same = [t for t in self.live_trials() if t.ear == p.ear and t.kind == "startup"]
            if len(same) < 2 or same[-2].level_dbfs != p.level_db:
                return
        nxt = p.level_db - self.cfg.startup_step_db
        self.startup[p.ear] = nxt if nxt >= MIN_LEVEL_DB else None

    def can_undo(self) -> bool:
        return self._last is not None

    def undo_last(self) -> PreparedThreshold | None:
        if self._last is None:
            return None
        trial, state = self._last
        self.live_trials()[-1].undone = True
        self.startup = state
        self._last = None
        self._save()
        self.fit = self._fit()
        trial.retake = True
        return trial

    def live_trials(self) -> list[ThresholdRecord]:
        return [t for t in self.session.trials if not t.undone]

    # -- setup steps and the end -------------------------------------------

    def record_channel_check(self, played: str, heard: str) -> bool:
        ok = played == heard
        self.session.channel_checks.append({"played": played, "heard": heard, "ok": ok, "at": now_iso()})
        self._save()
        return ok

    def discard(self) -> None:
        with self._lock:
            self.discarded = True
            self.profile.delete_threshold(self.session)
            self.session.ended = now_iso()

    def finish(self) -> None:
        """Close the session. One with no trials is deleted."""
        if self.discarded:
            return
        self.session.ended = now_iso()
        if not self.session.trials:
            self.profile.delete_threshold(self.session)
            return
        self._save()

    # -- status ------------------------------------------------------------

    def progress(self) -> dict:
        live = self.live_trials()
        blanks = [t for t in live if t.kind == "blank"]
        probes = [t for t in live if t.kind == "probe"]
        return {
            "session_trials": len(live),
            "session_trials_ear": {e: sum(t.ear == e for t in live) for e in EARS},
            "false_alarms": sum(t.positive for t in blanks), "blanks": len(blanks),
            "probe_hits": sum(t.positive for t in probes), "probes": len(probes),
            "neither": sum(t.response == "neither" for t in live),
            "wrong": sum(t.response in ("first", "second") and not t.positive for t in live),
            "starting": any(v is not None for v in self.startup.values()),
            "fast": sum(t.fast for t in live),
            "undone": len(self.session.trials) - len(live),
        }

    def _save(self) -> None:
        with self._lock:
            if self.discarded:
                return
            self.profile.save_threshold(self.session)
