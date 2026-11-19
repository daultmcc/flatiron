"""Centring sessions (DESIGN.md, "Centring sessions"). Collected only: no fit
uses them yet.

Per trial, one burst plays in both ears at once, the same waveform in each,
with the left at +d/2 dB and the right at −d/2 around its level; the listener
says whether the image sounds left or right of centre. The d that sounds
centred measures the channels' perceived balance without a comparison across
time, which is what cross-ear trials make (and where a bias towards one ear
can't be told from a real level difference).

The planner aims d at the current estimate of that point, plus jitter so the
answers also show how steep the function is: −(c + s(f)), where s(f) is the
difference of the two ears' fitted curves at f (each with its mean over the
range removed, as for cross-ear trials) and c a running estimate of the
overall balance from the answers so far, this profile's earlier centring
sessions included. The level follows loudness matching with the ears' mean
curve. The UI drives this like a TrialRunner.
"""

import secrets
import threading
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import log_ndtr

from flatiron import EARS, FS
from flatiron.audio.limiter import DEFAULT_LEVEL_DB, MAX_LEVEL_DB, db_to_amp, limit
from flatiron.audio.stimulus import StimulusSpec, make_stimulus, stimulus_key
from flatiron.model.grid import interp_matrix, log_grid
from flatiron.model.probit import band_matrix
from flatiron.session.cross import cross_range
from flatiron.session.runner import FAST_MS, MAX_SESSION_MIN
from flatiron.session.store import CentringRecord, Profile, now_iso

SIGMA_DB = 1.5  # assumed spread of the answers, for aiming only
PRIOR_SD_DB = 3.0  # prior SD of the overall balance, for aiming only
# Wide enough that many trials clearly lean: at ±2 (clipped at 4) nearly every
# burst sounded centred, since the aim (shape included) is accurate.
JITTER_SD, MAX_JITTER = 4.0, 8.0
MAX_ILD_DB = 12.0
ROVE_DB = 2.0
MATCH_BOOST_DB, MATCH_CUT_DB = 12.0, 30.0


@dataclass
class CentringSettings:
    # One-octave bands: 1/12-octave ones gave no stable image to place (DESIGN.md).
    stimulus: StimulusSpec = field(default_factory=lambda: StimulusSpec(bandwidth_oct=1.0))
    session_minutes: float = 5.0
    max_trials: int | None = None
    match: bool = True
    debug: bool = False
    tincan: object = None  # tincan_live.TinCanWatch: TinCan's gain is read at every burst

    def __post_init__(self):
        if not 0 < self.session_minutes <= MAX_SESSION_MIN:
            raise ValueError(f"sessions are capped at {MAX_SESSION_MIN:g} minutes")


@dataclass
class CentringPlan:
    freq: float
    ild_db: float
    rove_db: float
    match_db: float
    level_db: float = 0.0  # before the split: listening level + rove + matching, set when rendered
    eq_gain_l: float = 0.0  # known EQ gain (TinCan's) in each ear, set when rendered
    eq_gain_r: float = 0.0


@dataclass
class PreparedCentring:
    plan: CentringPlan
    buffer: np.ndarray
    first_on: float
    first_off: float
    second_on: float
    second_off: float
    level_dbfs: float  # the session's listening level (the UI compares it, as for a trial)
    retake: bool = False
    played: tuple[float, float] = (0.0, 0.0)  # dBFS RMS left and right, after the limiter (debug)
    estimate_db: float = 0.0  # the running balance estimate when it was planned (debug)


def ear_curves(profile: Profile, stimulus: str, lo: float, hi: float):
    """Per ear, a function giving the fitted curve at frequencies (band
    averaged for the stimulus), with its mean over [lo, hi] removed; None for
    an ear without enough trials."""
    from flatiron.fit.run import cached_fit, listener_noise

    hist, grids = profile.history()
    out = {}
    noise = None
    for e in EARS:
        if e not in grids or len(hist[e]) < 2:
            out[e] = None
            continue
        noise = noise or listener_noise(profile.dir.parent.parent)
        fit = cached_fit(profile, hist[e], np.asarray(grids[e], float), noise[e])
        curve = fit.L - float(np.mean(interp_matrix(fit.grid, log_grid(lo, hi)) @ fit.L))
        out[e] = (lambda g, c: lambda f: band_matrix(g, np.atleast_1d(f), [stimulus] * np.size(f)) @ c)(
            fit.grid, curve)
    return out


class CentringRunner:
    centring = True  # the UI's flag for this kind of session
    speaker = None

    def __init__(self, profile: Profile, settings: CentringSettings = CentringSettings(),
                 seed: int | None = None, device: dict | None = None, fs: int = FS, curves=None):
        """curves: per ear, as ear_curves gives (None: from the profile's fits)."""
        self.profile, self.settings, self.fs = profile, settings, fs
        self.seed = secrets.randbelow(2**31) if seed is None else seed
        self.rng = np.random.default_rng(self.seed)
        self.key = stimulus_key(settings.stimulus)
        self.measured = profile.measured
        self.f_lo, self.f_hi = cross_range(self.measured)
        self.curves = ear_curves(profile, self.key, self.f_lo, self.f_hi) if curves is None else curves
        start = profile.last_level()
        self.level_dbfs = float(DEFAULT_LEVEL_DB if start is None else start)
        self.earlier = profile.centring_sessions()
        self.session = profile.new_centring_session(
            device=device or {}, sample_rate=fs, stimulus=asdict(settings.stimulus), level_dbfs=self.level_dbfs,
            seed=self.seed, measured=list(self.measured), match=settings.match)
        self.discarded = False
        self._lock = threading.Lock()
        from flatiron.tincan_live import SessionEq

        self.tincan = SessionEq(settings.tincan, self.key)
        self._last: PreparedCentring | None = None
        self.current_estimate = self.estimate()
        self._save()

    # -- the ears' curves ----------------------------------------------------

    def shape(self, f: float) -> float:
        """s(f): left minus right fitted curve at f (0 without both fits)."""
        if self.curves.get("L") is None or self.curves.get("R") is None:
            return 0.0
        return float(self.curves["L"](f)[0] - self.curves["R"](f)[0])

    def match_gain(self, f: float) -> float:
        """Loudness matching with the ears' mean curve, capped as trials are."""
        known = [c for c in self.curves.values() if c is not None]
        if not self.settings.match or not known:
            return 0.0
        g = -float(np.mean([c(f)[0] for c in known]))
        return float(np.clip(g, -MATCH_CUT_DB, MATCH_BOOST_DB))

    # -- the running estimate --------------------------------------------------

    def answers(self) -> list[CentringRecord]:
        return [t for s in self.earlier for t in s.live()] + self.live_trials()

    def estimate(self) -> float:
        """MAP of the overall balance c (dB, positive: the left louder) from
        every centring answer so far: P(left) = Φ((d + s(f) + c) / σ)."""
        ts = self.answers()
        if not ts:
            return 0.0
        k = np.array([t.ild + self.shape(t.freq) for t in ts])
        y = np.where([t.left for t in ts], 1.0, -1.0)

        def nlp(c):
            return -np.sum(log_ndtr(y * (k + c) / SIGMA_DB)) + 0.5 * (c / PRIOR_SD_DB) ** 2

        return float(minimize_scalar(nlp, bounds=(-20, 20), method="bounded").x)

    # -- planning --------------------------------------------------------------

    def next_plan(self) -> CentringPlan:
        rng = self.rng
        f = float(2 ** rng.uniform(np.log2(self.f_lo), np.log2(self.f_hi)))
        jitter = float(np.clip(rng.normal() * JITTER_SD, -MAX_JITTER, MAX_JITTER))
        # Aimed at the difference that reaches the ears, the EQ's included.
        eq = self.tincan.gain(f, "L") - self.tincan.gain(f, "R")
        d = float(np.clip(-(self.current_estimate + self.shape(f)) + jitter - eq, -MAX_ILD_DB, MAX_ILD_DB))
        rove = float(rng.uniform(-ROVE_DB, ROVE_DB))
        return CentringPlan(f, d, rove, self.match_gain(f))

    def render(self, plan: CentringPlan, retake: bool = False) -> PreparedCentring:
        x = make_stimulus(plan.freq, self.settings.stimulus, seed=self.seed, fs=self.fs)
        plan.level_db = self.level_dbfs + plan.rove_db + plan.match_db
        plan.eq_gain_l, plan.eq_gain_r = self.tincan.gain(plan.freq, "L"), self.tincan.gain(plan.freq, "R")
        left = x * db_to_amp(plan.level_db + plan.ild_db / 2)
        right = x * db_to_amp(plan.level_db - plan.ild_db / 2)
        lim = limit(np.stack([left, right], axis=1))
        d = x.size / self.fs
        played = (plan.level_db + plan.ild_db / 2 + lim.gain_db, plan.level_db - plan.ild_db / 2 + lim.gain_db)
        return PreparedCentring(plan, lim.signal.astype(np.float32), 0.0, d, 0.0, d, self.level_dbfs, retake,
                                played, self.current_estimate)

    def prepare(self) -> PreparedCentring:
        return self.render(self.next_plan())

    def eq_message(self) -> str | None:
        return self.tincan.message()

    # -- answers ---------------------------------------------------------------

    def answer(self, trial: PreparedCentring, left: bool, rt_ms: float, replays: int = 0, t: float = 0.0,
               rt_last_ms: float | None = None, peeked: bool = False) -> CentringRecord:
        p = trial.plan
        rec = CentringRecord(freq=p.freq, ild_db=p.ild_db, level_dbfs=p.level_db, rove_db=p.rove_db,
                             response="left" if left else "right", rt_ms=float(rt_ms),
                             rt_last_ms=rt_ms if rt_last_ms is None else float(rt_last_ms), replays=replays,
                             fast=rt_ms < FAST_MS, t=round(t, 3), retake=trial.retake, peeked=peeked,
                             eq_gain_l=p.eq_gain_l, eq_gain_r=p.eq_gain_r)
        if (ctx := self.tincan.context()) is not None:
            self.session.eq_context.update(ctx)
        self.session.trials.append(rec)
        self._save()
        self.current_estimate = self.estimate()
        self._last = trial
        return rec

    def can_undo(self) -> bool:
        return self._last is not None

    def undo_last(self) -> PreparedCentring | None:
        if self._last is None:
            return None
        trial = self._last
        self.live_trials()[-1].undone = True
        self._last = None
        self._save()
        self.current_estimate = self.estimate()
        trial.retake = True
        return trial

    def live_trials(self) -> list[CentringRecord]:
        return [t for t in self.session.trials if not t.undone]

    # -- setup steps and the end -------------------------------------------------

    def set_level(self, level_dbfs: float) -> None:
        """The listening level (the level screen, before any trials)."""
        if self.live_trials():
            return
        self.level_dbfs = self.session.level_dbfs = float(level_dbfs)
        self._save()

    def level_gain(self, ear: str, f: float) -> float:
        """For the level screen: the matching a burst at f gets (both ears alike)."""
        return self.match_gain(f)

    def record_channel_check(self, played: str, heard: str) -> bool:
        ok = played == heard
        self.session.channel_checks.append({"played": played, "heard": heard, "ok": ok, "at": now_iso()})
        self._save()
        return ok

    def discard(self) -> None:
        with self._lock:
            self.discarded = True
            self.profile.delete_centring(self.session)
            self.session.ended = now_iso()

    def finish(self) -> None:
        """Close the session. One with no trials is deleted."""
        if self.discarded:
            return
        self.session.ended = now_iso()
        if not self.session.trials:
            self.profile.delete_centring(self.session)
            return
        self._save()

    # -- status ------------------------------------------------------------------

    def progress(self) -> dict:
        live = self.live_trials()
        return {"session_trials": len(live), "left": sum(t.left for t in live),
                "fast": sum(t.fast for t in live), "undone": len(self.session.trials) - len(live)}

    def _save(self) -> None:
        with self._lock:
            if self.discarded:
                return
            self.profile.save_centring(self.session)
