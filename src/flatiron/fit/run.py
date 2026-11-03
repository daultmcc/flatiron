"""Fit a profile: cached perceptual fits → correction, with the profile's own
settings and checkpoint. Nothing here is saved except the cache: the fit is
recomputed whenever a command needs it."""

import hashlib
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from typing import Callable

import numpy as np

from flatiron import EARS
from flatiron.audio.biquad import PeakingFilter
from flatiron.fit.pipeline import Correction, CorrectionSettings, correct
from flatiron.model.grid import log_grid
from flatiron.model.probit import Priors, ProbitFit, Trials, fit_probit
from flatiron.session.store import Profile, Session, now_iso

MIN_SESSION_TRIALS = 60  # per ear, for a session to enter the consistency check
CACHE_VERSION = "3"  # 2: time-order bias; 3: per-trial stimulus spectrum


def _key(trials: Trials, grid: np.ndarray, priors: Priors, extra: str = "") -> str:
    h = hashlib.sha256(CACHE_VERSION.encode())
    for a in (trials.f_a, trials.f_b, trials.offset, trials.a_louder.astype(np.int8), trials.order, grid):
        h.update(np.ascontiguousarray(a).tobytes())
    h.update("|".join(map(str, trials.stimulus)).encode())
    h.update(repr(priors).encode() + extra.encode())
    return h.hexdigest()[:20]


def _cache_path(profile: Profile, trials: Trials, grid: np.ndarray, priors: Priors = Priors(),
                init: ProbitFit | None = None, fit_hyper: bool = True):
    extra = "" if fit_hyper else f"fixed:{init.sigma0:.6g},{init.d0:.6g},{init.lapse:.6g},{init.bias_db:.6g}"
    return profile.dir / "cache" / f"probit-{_key(trials, grid, priors, extra)}.npz"


def cached_fit(profile: Profile, trials: Trials, grid: np.ndarray, priors: Priors = Priors(),
               init: ProbitFit | None = None, fit_hyper: bool = True) -> ProbitFit:
    """The perceptual fit depends only on the trials, so it's cached on disk."""
    path = _cache_path(profile, trials, grid, priors, init, fit_hyper)
    if path.exists():
        z = np.load(path)
        return ProbitFit(z["grid"], z["L"], z["cov"], float(z["sigma0"]), float(z["d0"]), float(z["lapse"]),
                         int(z["n_trials"]), float(z["nlp"]), bool(z["converged"]),
                         bias_db=float(z["bias"]), bias_sd=float(z["bias_sd"]))
    fit = fit_probit(trials, grid, priors, init=init, fit_hyper=fit_hyper)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez(tmp, grid=fit.grid, L=fit.L, cov=fit.cov, sigma0=fit.sigma0, d0=fit.d0, lapse=fit.lapse,
             n_trials=fit.n_trials, nlp=fit.neg_log_post, converged=fit.converged,
             bias=fit.bias_db, bias_sd=fit.bias_sd)
    tmp.replace(path)
    return fit


@dataclass
class ProfileFit:
    correction: Correction
    fits: dict[str, ProbitFit]
    sessions: list[Session]
    session_ids: dict[str, list[str]]  # per ear: sessions in the consistency check
    pooled: list[str] = field(default_factory=list)  # labels of other profiles whose trials are included

    def summary(self) -> dict:
        """JSON-able: what export, audition and verify work from."""
        out = self.correction.summary()
        out.update(sessions=[s.id for s in self.sessions],
                   noise={e: {"sigma0": f.sigma0, "d0": f.d0, "lapse": f.lapse, "trials": f.n_trials,
                              "time_order_bias_db": f.bias_db, "time_order_bias_sd": f.bias_sd}
                          for e, f in self.fits.items()})
        return out


# -- per-profile fit settings (`flatiron config`) ----------------------------

def profile_settings(profile: Profile) -> CorrectionSettings:
    """The profile's settings: defaults, with its changes applied."""
    known = {f.name for f in fields(CorrectionSettings)}
    changed = profile.read().get("settings") or {}
    return CorrectionSettings(**{k: v for k, v in changed.items() if k in known})


def set_profile_settings(profile: Profile, settings: CorrectionSettings) -> None:
    """Stores only what differs from the defaults, so improved defaults reach
    existing profiles."""
    default = asdict(CorrectionSettings())
    profile.update(settings={k: v for k, v in asdict(settings).items() if v != default[k]})


def profile_session_ids(profile: Profile) -> list[str] | None:
    """Sessions the fit uses (None: all)."""
    return profile.read().get("sessions_used")


def checkpoint(profile: Profile) -> dict[str, list[PeakingFilter]] | None:
    """Filters fixed by `config --checkpoint` (None if there's no checkpoint)."""
    cp = profile.read().get("checkpoint")
    if not cp:
        return None
    return {e: [PeakingFilter(**f) for f in cp["filters"][e]] for e in EARS}


def set_checkpoint(profile: Profile, pf: "ProfileFit | None") -> None:
    if pf is None:
        profile.update(checkpoint=None)
        return
    filters = {e: [asdict(f) for f in c.filters] for e, c in pf.correction.ears.items()}
    trials = {e: f.n_trials for e, f in pf.fits.items()}
    profile.update(checkpoint={"filters": filters, "made": now_iso(), "trials": trials})


class NotEnoughTrials(ValueError):
    pass


# -- pooling (`flatiron config --pool`) ---------------------------------------

def pooled_profiles(profile: Profile) -> list[Profile]:
    """Other profiles (the same headphones) whose trials the fit includes."""
    out = []
    for slug in profile.read().get("pool") or []:
        p = Profile(slug, profile.dir.parent.parent)
        if not p.path.exists():
            raise ValueError(f"Profile {slug!r} is pooled into {profile.read()['label']!r} but doesn't exist "
                             "any more: flatiron config --pool none")
        out.append(p)
    return out


def fit_sessions(profile: Profile) -> list[Session]:
    """The profile's sessions and those of the profiles pooled into it, oldest
    first (the newest session's grid is the one the fit uses)."""
    sessions = profile.sessions() + [s for p in pooled_profiles(profile) for s in p.sessions()]
    return sorted(sessions, key=lambda s: datetime.fromisoformat(s.started))


def fit_profile(profile: Profile, settings: CorrectionSettings = CorrectionSettings(),
                session_ids: list[str] | None = None,
                fixed: dict[str, list[PeakingFilter]] | None = None,
                on_fit: Callable[[], None] | None = None) -> ProfileFit:
    """on_fit is called once if a perceptual fit isn't cached and has to be
    computed (it takes a few seconds)."""
    sessions = fit_sessions(profile)
    if session_ids:
        missing = set(session_ids) - {s.id for s in sessions}
        if missing:
            raise ValueError(f"no such session(s): {', '.join(sorted(missing))}")
        sessions = [s for s in sessions if s.id in session_ids]
    trials, grids = profile.history(sessions)
    fits, per_session, used = {}, {}, {}
    notify = [on_fit]

    def fit(*a, **k):
        if notify[0] is not None and not _cache_path(profile, *a, **k).exists():
            notify[0]()
            notify[0] = None
        return cached_fit(profile, *a, **k)

    for e in EARS:
        if len(trials[e]) < 2:
            raise NotEnoughTrials(f"not enough trials for the {e} ear yet ({len(trials[e])})")
        grid = grids.get(e, log_grid())
        fits[e] = fit(trials[e], grid)
        per_session[e], used[e] = [], []
        for s in sessions:
            t = s.trials_for(e)
            if len(t) >= MIN_SESSION_TRIALS:
                per_session[e].append(fit(t, grid, init=fits[e], fit_hyper=False))
                used[e].append(s.id)
    corr = correct(fits, settings, per_session, fixed)
    return ProfileFit(corr, fits, sessions, used, [p.read()["label"] for p in pooled_profiles(profile)])


def current_fit(profile: Profile, on_fit: Callable[[], None] | None = None) -> ProfileFit:
    """The profile's fit as of now: all its trials (or the configured
    sessions), its settings, and its checkpoint. Computed on demand; the
    perceptual fits are cached, so this is quick unless there are new trials."""
    return fit_profile(profile, profile_settings(profile), profile_session_ids(profile),
                       fixed=checkpoint(profile), on_fit=on_fit)


def leftovers(profile: Profile, applied: dict[str, list[PeakingFilter]],
              on_fit: Callable[[], None] | None = None) -> tuple[ProfileFit, dict[str, list]]:
    """What's still significant with `applied` (e.g. the filters a verify
    session used) in place, using all trials and the profile's settings. No
    filters are added."""
    st = replace(profile_settings(profile), bands=max(len(v) for v in applied.values()))
    pf = fit_profile(profile, st, profile_session_ids(profile), fixed=applied, on_fit=on_fit)
    return pf, {e: [f for f in c.features if f.accepted] for e, c in pf.correction.ears.items()}
