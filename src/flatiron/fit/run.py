"""Fit a profile: cached perceptual fits → correction → profile.json."""

import hashlib
from dataclasses import asdict, dataclass

import numpy as np

from flatiron import EARS
from flatiron.fit.pipeline import Correction, CorrectionSettings, correct
from flatiron.model.grid import log_grid
from flatiron.model.probit import Priors, ProbitFit, Trials, fit_probit
from flatiron.session.store import Profile, Session, now_iso

MIN_SESSION_TRIALS = 60  # per ear, for a session to enter the consistency check
CACHE_VERSION = "2"  # 2: time-order bias


def _key(trials: Trials, grid: np.ndarray, priors: Priors, extra: str = "") -> str:
    h = hashlib.sha256(CACHE_VERSION.encode())
    for a in (trials.f_a, trials.f_b, trials.offset, trials.a_louder.astype(np.int8), trials.order, grid):
        h.update(np.ascontiguousarray(a).tobytes())
    h.update(repr(priors).encode() + extra.encode())
    return h.hexdigest()[:20]


def cached_fit(profile: Profile, trials: Trials, grid: np.ndarray, priors: Priors = Priors(),
               init: ProbitFit | None = None, fit_hyper: bool = True) -> ProbitFit:
    """The perceptual fit depends only on the trials, so it's cached on disk."""
    extra = "" if fit_hyper else f"fixed:{init.sigma0:.6g},{init.d0:.6g},{init.lapse:.6g},{init.bias_db:.6g}"
    path = profile.dir / "cache" / f"probit-{_key(trials, grid, priors, extra)}.npz"
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


def fit_profile(profile: Profile, settings: CorrectionSettings = CorrectionSettings(),
                session_ids: list[str] | None = None, save: bool = True) -> ProfileFit:
    sessions = profile.sessions()
    if session_ids:
        missing = set(session_ids) - {s.id for s in sessions}
        if missing:
            raise ValueError(f"no such session(s): {', '.join(sorted(missing))}")
        sessions = [s for s in sessions if s.id in session_ids]
    trials, grids = profile.history(sessions)
    fits, per_session, used = {}, {}, {}
    for e in EARS:
        if len(trials[e]) < 2:
            raise ValueError(f"not enough trials for the {e} ear yet ({len(trials[e])})")
        grid = grids.get(e, log_grid())
        fits[e] = cached_fit(profile, trials[e], grid)
        per_session[e], used[e] = [], []
        for s in sessions:
            t = s.trials_for(e)
            if len(t) >= MIN_SESSION_TRIALS:
                per_session[e].append(cached_fit(profile, t, grid, init=fits[e], fit_hyper=False))
                used[e].append(s.id)
    corr = correct(fits, settings, per_session)
    if save:
        summary = corr.summary()
        summary.update(fitted=now_iso(), sessions=[s.id for s in sessions],
                       noise={e: {"sigma0": f.sigma0, "d0": f.d0, "lapse": f.lapse, "trials": f.n_trials,
                                  "time_order_bias_db": f.bias_db, "time_order_bias_sd": f.bias_sd}
                              for e, f in fits.items()})
        profile.update(fit=summary)
    return ProfileFit(corr, fits, sessions, used)
