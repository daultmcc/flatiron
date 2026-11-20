"""Threshold sessions: detection trials near the threshold of hearing
(DESIGN.md, "Threshold sessions").

Per trial, one burst in one ear (drawn at random, so the listener
can't know which to attend to), at a level near the current
estimate of the threshold there. The burst is in one of two marked intervals
and the listener says which, or "neither" if they heard nothing; the right
interval counts as heard. Some trials are probes well above threshold, to
measure lapses.

Frequencies come from the profile's measured range; the grid spans it, and
any earlier threshold session's frequencies, since one fit covers them all.

Each session starts with a descending run in each ear at 2 kHz (or the
nearest end of the measured range): 10 dB down
after each positive answer, until the first negative one (or up, if the first
bursts aren't heard). That finds the
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
from flatiron.session.store import THRESHOLD_TASK, Profile, ThresholdRecord, ThresholdSession, now_iso

MIN_LEVEL_DB = -140.0  # no burst is played quieter than this (dBFS RMS)


@dataclass(frozen=True)
class ThresholdConfig:
    probe_rate: float = 0.04
    probe_db: float = 15.0  # above the estimated threshold
    top_k: int = 8
    region_gap_oct: float = 0.25  # don't repeat the previous trial's region
    startup_freq: float = 2000.0
    startup_below_comfort_db: float = 30.0
    startup_above_known_db: float = 15.0  # with earlier sessions: start this far above their threshold
    startup_step_db: float = 10.0
    sigma_every: int = 50  # trials between re-estimates of the psychometric spread
    climb_after_misses: int = 2  # misses at a frequency, none heard above them, before climbing
    climb_db: float = 6.0  # the climb: at least this far above the loudest miss


@dataclass
class ThresholdSettings:
    stimulus: StimulusSpec = field(default_factory=StimulusSpec)
    gap_s: float = 0.3
    session_minutes: float = 8.0
    max_trials: int | None = None
    debug: bool = False  # the trial screen can show each burst's frequency, level and the estimate
    tincan: object = None  # tincan_live.TinCanWatch: TinCan's gain is read at every burst

    def __post_init__(self):
        if not 0 < self.session_minutes <= MAX_SESSION_MIN:
            raise ValueError(f"sessions are capped at {MAX_SESSION_MIN:g} minutes")


@dataclass
class ThresholdPlan:
    ear: str
    freq: float
    level_db: float
    kind: str
    interval: int = 1  # which interval holds the burst
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
    # limiter), and this session's
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
    task = THRESHOLD_TASK

    def __init__(self, profile: Profile, settings: ThresholdSettings = ThresholdSettings(),
                 config: ThresholdConfig = ThresholdConfig(), seed: int | None = None,
                 device: dict | None = None, fs: int = FS, volume: bool = True):
        self.profile, self.settings, self.cfg, self.fs = profile, settings, config, fs
        self.seed = secrets.randbelow(2**31) if seed is None else seed
        self.rng = np.random.default_rng(self.seed)
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
        self._curves = None
        self.startup: dict[str, float | None] = {}
        self._start_run()
        self._save()

    def _start_run(self) -> None:
        """The descending run: each ear's next level, or None once it's done."""
        cfg = self.cfg
        start = self.level_dbfs - cfg.startup_below_comfort_db
        if self.earlier:
            # The last session's offset is the best guess at this one's.
            known = {e: float(self.fit.level(e, self.startup_freq, self.index - 1, self.key)[0][0])
                     for e in EARS}
            start = min(start, max(known.values()) + cfg.startup_above_known_db)
        self.startup = {e: start for e in EARS}

    # -- the listening level -------------------------------------------------

    def set_level(self, level_dbfs: float) -> None:
        """The listening level from the level screen, before any trials: the
        descending run starts below it."""
        if self.live_trials():
            return
        self.level_dbfs = self.session.comfort_dbfs = float(level_dbfs)
        self._start_run()
        self._save()

    def match_gain(self, ear: str, f: float) -> float:
        """For the level screen: loudness matching with the ear's fitted curve,
        as in trial sessions (0 without one)."""
        from flatiron.session.centring import MATCH_BOOST_DB, MATCH_CUT_DB, ear_curves

        if self._curves is None:
            self._curves = ear_curves(self.profile, self.key, *self.measured)
        c = self._curves.get(ear)
        return 0.0 if c is None else float(np.clip(-c(f)[0], -MATCH_CUT_DB, MATCH_BOOST_DB))

    # -- the model ---------------------------------------------------------

    def data(self) -> ThresholdData:
        parts = [(s.stimulus_key(), s.trials) for s in self.earlier]
        parts.append((self.key, self.session.trials))
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
        interval = int(rng.integers(1, 3))
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
        probe = rng.random() < cfg.probe_rate
        if probe:
            x = mean[i] + cfg.probe_db
        else:
            # Around the threshold, so the answers also say how steep the
            # psychometric function is.
            x = mean[i] + self.fit.sigma * rng.normal()
        x = self._climb(ear, f, x)
        x -= self.tincan.gain(f, ear)  # the estimate is of what reaches the headphones, EQ included
        x = float(np.clip(x, MIN_LEVEL_DB, MAX_LEVEL_DB))
        return ThresholdPlan(ear, f, x, "probe" if probe else "signal", interval)

    def _climb(self, ear: str, f: float, x: float) -> float:
        """Above the loudest miss where nothing has been heard yet. Near a
        steep rise (the top of hearing) the estimate is extrapolated from
        below, and each miss moves it only a dB or two, so the trials there
        can stay below threshold all session. Once the ear has missed
        climb_after_misses bursts within half a grid step of f in this
        session, with nothing heard at or above the loudest of them, the
        level is at least climb_db above it, and no higher than the listening
        level. Levels are as they reach the headphones (known EQ included)."""
        cfg = self.cfg
        near = [t for t in self.live_trials()
                if t.ear == ear and t.kind != "startup" and abs(np.log2(t.freq / f)) < 1 / 48]
        missed = [t.level_dbfs + t.eq_gain for t in near if not t.positive]
        if len(missed) < cfg.climb_after_misses:
            return x
        top = max(missed)
        if any(t.positive and t.level_dbfs + t.eq_gain >= top for t in near):
            return x
        return max(x, min(top + cfg.climb_db, self.level_dbfs))

    def render(self, plan: ThresholdPlan, retake: bool = False) -> PreparedThreshold:
        d = self.settings.stimulus.duration
        plan.eq_gain = self.tincan.gain(plan.freq, plan.ear)
        burst = make_stimulus(plan.freq, self.settings.stimulus, seed=self.seed, fs=self.fs)
        burst = burst * db_to_amp(plan.level_db)
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
        """first: True for the first interval, False the second, None "neither"."""
        p = trial.plan
        if first is None:
            response, positive = "neither", False
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

    def _passes(self, ear: str) -> tuple[set[float], set[float]]:
        """This session's start-up levels in the ear: those passed (heard) and those failed."""
        same = [t for t in self.live_trials() if t.ear == ear and t.kind == "startup"]
        return {t.level_dbfs for t in same if t.positive}, {t.level_dbfs for t in same if not t.positive}

    def _startup_step(self, p: ThresholdPlan, positive: bool) -> None:
        """Down a step after each pass, until the first miss once something has
        passed. A miss before any pass means the start was below threshold (the
        volume turned down since the last session, say): up a step instead, as
        far as the listening level."""
        step = self.cfg.startup_step_db
        passed, failed = self._passes(p.ear)
        if not positive:
            if passed:
                self.startup[p.ear] = None
                return
            nxt = p.level_db + step
            self.startup[p.ear] = nxt if nxt <= self.level_dbfs else None
            return
        nxt = p.level_db - step
        # Below a level already missed: the threshold is bracketed.
        self.startup[p.ear] = nxt if nxt >= MIN_LEVEL_DB and nxt not in failed else None

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
        probes = [t for t in live if t.kind == "probe"]
        return {
            "session_trials": len(live),
            "session_trials_ear": {e: sum(t.ear == e for t in live) for e in EARS},
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
