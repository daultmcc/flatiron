"""Fit a profile: cached perceptual fits → correction, with the profile's own
settings and checkpoint. Nothing here is saved except the cache: the fit is
recomputed whenever a command needs it."""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from flatiron import EARS
from flatiron.audio.biquad import PeakingFilter
from flatiron.fit.consensus import MODEL_VERSION, EarConsensus, KINDS_VOTING, consensus_of
from flatiron.fit.pipeline import Correction, CorrectionSettings, correct, member_source
from flatiron.model.grid import interp_matrix, log_grid
from flatiron.model.hierarchical import fit_hierarchical
from flatiron.model.probit import Priors, ProbitFit, Trials
from flatiron.session.store import Profile, Session, now_iso

# 2: time-order bias; 3: per-trial stimulus spectrum; 4: seatings as random effects
CACHE_VERSION = "4"


def fit_key(trials: Trials, grid: np.ndarray, priors: Priors = Priors()) -> str:
    """What a perceptual fit depends on: changes whenever it would."""
    h = hashlib.sha256(CACHE_VERSION.encode())
    for a in (trials.f_a, trials.f_b, trials.offset, trials.a_louder.astype(np.int8), trials.order,
              trials.group, grid):
        h.update(np.ascontiguousarray(a).tobytes())
    h.update("|".join(map(str, trials.stimulus)).encode())
    h.update(repr(priors).encode())
    return h.hexdigest()[:20]


def _cache_path(profile: Profile, trials: Trials, grid: np.ndarray, priors: Priors = Priors()):
    return profile.dir / "cache" / f"probit-{fit_key(trials, grid, priors)}.npz"


def cached_fit(profile: Profile, trials: Trials, grid: np.ndarray, priors: Priors = Priors()) -> ProbitFit:
    """The perceptual fit depends only on the trials, so it's cached on disk.
    The seating spread is estimated here, so it's estimated once per set of
    trials: the first time a fit is wanted after a session."""
    path = _cache_path(profile, trials, grid, priors)
    if path.exists():
        z = np.load(path)
        return ProbitFit(z["grid"], z["L"], z["cov"], float(z["sigma0"]), float(z["d0"]), float(z["lapse"]),
                         int(z["n_trials"]), float(z["nlp"]), bool(z["converged"]),
                         bias_db=float(z["bias"]), bias_sd=float(z["bias_sd"]),
                         tau_lo=float(z["tau_lo"]), tau_hi=float(z["tau_hi"]), tau_fitted=bool(z["tau_fitted"]),
                         tau=z["tau"], u=z["u"], v=z["v"], group_trials=z["group_trials"],
                         group_cov=z["group_cov"], group_u_cov=z["group_u_cov"],
                         focus_cov=z["focus_cov"], focus_cross=z["focus_cross"])
    fit = fit_hierarchical(trials, grid, priors)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez(tmp, grid=fit.grid, L=fit.L, cov=fit.cov, sigma0=fit.sigma0, d0=fit.d0, lapse=fit.lapse,
             n_trials=fit.n_trials, nlp=fit.neg_log_post, converged=fit.converged,
             bias=fit.bias_db, bias_sd=fit.bias_sd, tau_lo=fit.tau_lo, tau_hi=fit.tau_hi,
             tau_fitted=fit.tau_fitted, tau=fit.tau, u=fit.u, v=fit.v, group_trials=fit.group_trials,
             group_cov=fit.group_cov, group_u_cov=fit.group_u_cov,
             focus_cov=fit.focus_cov, focus_cross=fit.focus_cross)
    tmp.replace(path)
    return fit


@dataclass
class ProfileFit:
    correction: Correction
    fits: dict[str, ProbitFit]
    sessions: list[Session]
    session_ids: dict[str, list[str]]  # per ear: the session behind each group of the fit
    pooled: list[str] = field(default_factory=list)  # labels of other profiles whose trials are included

    def summary(self) -> dict:
        """JSON-able: what export, audition and verify work from."""
        out = self.correction.summary()
        out.update(sessions=[s.id for s in self.sessions],
                   noise={e: {"sigma0": f.sigma0, "d0": f.d0, "lapse": f.lapse, "trials": f.n_trials,
                              "time_order_bias_db": f.bias_db, "time_order_bias_sd": f.bias_sd,
                              "tau_seat": [f.tau_lo, f.tau_hi], "tau_fitted": f.tau_fitted}
                          for e, f in self.fits.items()})
        return out


# -- per-profile fit settings (`flatiron config`) ----------------------------

def profile_settings(profile: Profile) -> CorrectionSettings:
    """The profile's settings: defaults, with its changes applied."""
    known = {f.name for f in fields(CorrectionSettings)}
    changed = dict(profile.read().get("settings") or {})
    ref = changed.get("reference")
    if isinstance(ref, dict):  # {type: consensus, profiles, mode, base, ...}
        changed["reference"] = ref.get("base", CorrectionSettings().reference)
        changed["consensus"] = tuple(ref["profiles"])
        changed["consensus_mode"] = ref.get("mode", "remove")
    return CorrectionSettings(**{k: v for k, v in changed.items() if k in known})


def set_profile_settings(profile: Profile, settings: CorrectionSettings) -> None:
    """Stores only what differs from the defaults, so improved defaults reach
    existing profiles."""
    default = asdict(CorrectionSettings())
    changed = {k: v for k, v in asdict(settings).items() if v != default[k]}
    changed.pop("consensus", None)
    changed.pop("consensus_mode", None)
    if settings.consensus:
        changed["reference"] = {"type": "consensus", "profiles": list(settings.consensus),
                                "mode": settings.consensus_mode, "base": settings.reference,
                                "speaker_trust": None, "speaker_response": None}
    profile.update(settings=changed)


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


def perceptual_fits(profile: Profile, session_ids: list[str] | None = None,
                    on_fit: Callable[[], None] | None = None) -> tuple[dict[str, ProbitFit], dict[str, str], list[Session]]:
    """Per ear, the profile's cached perceptual fit and what it's keyed on. It
    depends only on the trials (its sessions and pooling), not on the settings."""
    sessions = fit_sessions(profile)
    if session_ids:
        missing = set(session_ids) - {s.id for s in sessions}
        if missing:
            raise ValueError(f"no such session(s): {', '.join(sorted(missing))}")
        sessions = [s for s in sessions if s.id in session_ids]
    trials, grids = profile.history(sessions)
    fits, keys = {}, {}
    for e in EARS:
        if len(trials[e]) < 2:
            raise NotEnoughTrials(f"not enough trials for the {e} ear yet ({len(trials[e])})")
        grid = grids.get(e, log_grid())
        if on_fit is not None and not _cache_path(profile, trials[e], grid).exists():
            on_fit()
            on_fit = None
        fits[e] = cached_fit(profile, trials[e], grid)
        fits[e].groups = [s.id for s in sessions if len(s.trials_for(e))]
        keys[e] = fit_key(trials[e], grid)
    return fits, keys, sessions


# -- the consensus cache (ADDENDUM.md, Feature 1b) -----------------------------

CONSENSUS_CACHE_CAP = 200 * 1024 * 1024


def consensus_dir(root: Path) -> Path:
    return root / "cache" / "consensus"


def _prune_consensus(root: Path, cap: int = CONSENSUS_CACHE_CAP) -> None:
    """Least recently used first (an entry's mtime is touched when it's read)."""
    files = sorted(consensus_dir(root).glob("*.npz"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= cap:
            return
        total -= p.stat().st_size
        p.unlink(missing_ok=True)


def _reference_id(name: str) -> str:
    """A reference CSV counts by its content, so editing it changes the key."""
    p = Path(name)
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.suffix == ".csv" and p.exists() else name


def _consensus_key(ear: str, member_keys: dict[str, str], st: CorrectionSettings) -> str:
    """The set of members, each one's fit, and every setting that affects the
    result; shared by every member of the set."""
    parts = {"version": MODEL_VERSION, "ear": ear, "members": sorted(member_keys.items()),
             "width": st.max_width_oct, "reference": _reference_id(st.reference), "phon": st.phon,
             "penalty": st.baseline_penalty, "speaker_trust": None, "speaker_response": None}
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


def shares_trials(a: Profile, b: Profile) -> bool:
    """Whether two profiles' fits include some of the same sessions."""
    return bool(({a.slug} | set(a.read().get("pool") or [])) & ({b.slug} | set(b.read().get("pool") or [])))


def consensus_members(profile: Profile, st: CorrectionSettings) -> list[Profile]:
    """The other profiles in the set: the ones listed, minus this one."""
    root = profile.dir.parent.parent
    out = []
    for slug in st.consensus:
        p = Profile(slug, root)
        if not p.path.exists():
            raise ValueError(f"Profile {slug!r} is in the consensus of {profile.read()['label']!r} but doesn't exist "
                             "any more: flatiron config -r iso226")
        if slug != profile.slug and shares_trials(profile, p):
            raise ValueError(f"{p.read()['label']!r} and {profile.read()['label']!r} share trials through pooling, so "
                             "they can't be in one consensus. Pooling means the same headphones measured again, "
                             "a consensus different ones.")
        if p.kind not in KINDS_VOTING:
            raise ValueError(f"{p.read()['label']!r} is of kind {p.kind}, which doesn't count toward a consensus.")
        if slug != profile.slug and p not in out:
            out.append(p)
    return out


def ear_consensus(profile: Profile, st: CorrectionSettings, own_fits: dict[str, ProbitFit],
                  own_keys: dict[str, str], on_fit: Callable[[], None] | None = None) -> dict[str, EarConsensus]:
    """Per ear, the soft median of the listed profiles and this one, at this
    profile's W and reference. One entry per set and settings, whichever member
    asks first; a new session in any member changes its fit and so the key."""
    root = profile.dir.parent.parent
    labels = {profile.slug: profile.read()["label"]}
    fits = {profile.slug: own_fits}
    keys = {profile.slug: own_keys}
    for p in consensus_members(profile, st):
        labels[p.slug] = p.read()["label"]
        fits[p.slug], keys[p.slug], _ = perceptual_fits(p, profile_session_ids(p), on_fit)
    order = sorted(fits)
    out, used = {}, {}
    for e in EARS:
        key = _consensus_key(e, {s: keys[s][e] for s in order}, st)
        path = consensus_dir(root) / f"{key}.npz"
        if path.exists():
            z = np.load(path)
            os.utime(path)
            grid, m, cov, tau = z["grid"], z["m"], z["cov"], float(z["tau_hp"])
            G = z[f"G{order.index(profile.slug)}"]
        else:
            sources = [member_source(labels[s], "headphone", fits[s][e], st) for s in order]
            sm = consensus_of(sources)
            grid, m, cov, tau = sm.grid, sm.m, sm.cov, sm.tau_hp
            G = sm.G[order.index(profile.slug)]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.stem + ".tmp.npz")
            np.savez(tmp, grid=grid, m=m, cov=cov, tau_hp=tau, members=np.array(order),
                     **{f"G{i}": g for i, g in enumerate(sm.G)})
            tmp.replace(path)
            _prune_consensus(root)
        A = interp_matrix(grid, own_fits[e].grid)
        out[e] = EarConsensus([labels[s] for s in order], key, A @ m, A @ cov @ A.T, G, A, tau, st.consensus_mode)
        used[e] = key
    (profile.dir / "cache").mkdir(parents=True, exist_ok=True)
    (profile.dir / "cache" / "consensus.json").write_text(json.dumps(used))
    return out


def fit_profile(profile: Profile, settings: CorrectionSettings = CorrectionSettings(),
                session_ids: list[str] | None = None,
                fixed: dict[str, list[PeakingFilter]] | None = None,
                on_fit: Callable[[], None] | None = None) -> ProfileFit:
    """on_fit is called once if a perceptual fit isn't cached and has to be
    computed (it takes a few seconds)."""
    fits, keys, sessions = perceptual_fits(profile, session_ids, on_fit)
    consensus = ear_consensus(profile, settings, fits, keys, on_fit) if settings.consensus else None
    corr = correct(fits, settings, fixed, consensus)
    return ProfileFit(corr, fits, sessions, {e: f.groups for e, f in fits.items()},
                      [p.read()["label"] for p in pooled_profiles(profile)])


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
