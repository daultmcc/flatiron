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
from flatiron.fit.consensus import KINDS_VOTING, MODEL_VERSION, EarConsensus, SoftMedian, Source, raw_consensus
from flatiron.fit.pipeline import Correction, CorrectionSettings, correct, ear_reference
from flatiron.model.grid import log_grid
from flatiron.model.hierarchical import MIN_TRIALS_NOISE_FIT, fit_balanced, fit_hierarchical, fit_shared_noise
from flatiron.model.probit import BalancePrior, Noise, Priors, ProbitFit, Trials
from flatiron.session.store import Profile, Session, now_iso

# 2: time-order bias; 3: per-trial stimulus spectrum; 4: seatings as random effects; 5: treble noise;
# 6: three-knot seating spread; 7: the listener's noise, shared by every profile
CACHE_VERSION = "7"


def trials_key(trials: Trials, grid: np.ndarray, priors: Priors = Priors()) -> str:
    """One ear's trials, grid and priors."""
    h = hashlib.sha256(CACHE_VERSION.encode())
    for a in (trials.f_a, trials.f_b, trials.offset, trials.a_louder.astype(np.int8), trials.order,
              trials.group, grid):
        h.update(np.ascontiguousarray(a).tobytes())
    h.update("|".join(map(str, trials.stimulus)).encode())
    h.update(repr(priors).encode())
    return h.hexdigest()[:20]


def fit_key(trials: Trials, grid: np.ndarray, noise: Noise, priors: Priors = Priors()) -> str:
    """What a perceptual fit depends on: changes whenever it would. The noise
    is the listener's, so a new session in any profile changes it."""
    return hashlib.sha256(f"{trials_key(trials, grid, priors)}|{noise.key()}".encode()).hexdigest()[:20]


def _cache_path(profile: Profile, trials: Trials, grid: np.ndarray, noise: Noise, priors: Priors = Priors()):
    return profile.dir / "cache" / f"probit-{fit_key(trials, grid, noise, priors)}.npz"


def _load_fit(path: Path) -> ProbitFit:
    z = np.load(path)
    return ProbitFit(z["grid"], z["L"], z["cov"], float(z["sigma0"]), float(z["d0"]), float(z["lapse"]),
                     int(z["n_trials"]), float(z["nlp"]), bool(z["converged"]),
                     bias_db=float(z["bias"]), bias_sd=float(z["bias_sd"]), treble_noise=float(z["treble_noise"]),
                     tau_lo=float(z["tau_lo"]), tau_mid=float(z["tau_mid"]), tau_hi=float(z["tau_hi"]), tau_fitted=bool(z["tau_fitted"]),
                     tau=z["tau"], u=z["u"], v=z["v"], group_trials=z["group_trials"],
                     group_cov=z["group_cov"], group_u_cov=z["group_u_cov"],
                     focus_cov=z["focus_cov"], focus_cross=z["focus_cross"])


def _save_fit(path: Path, fit: ProbitFit) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez(tmp, grid=fit.grid, L=fit.L, cov=fit.cov, sigma0=fit.sigma0, d0=fit.d0, lapse=fit.lapse,
             n_trials=fit.n_trials, nlp=fit.neg_log_post, converged=fit.converged,
             bias=fit.bias_db, bias_sd=fit.bias_sd, treble_noise=fit.treble_noise, tau_lo=fit.tau_lo, tau_mid=fit.tau_mid, tau_hi=fit.tau_hi,
             tau_fitted=fit.tau_fitted, tau=fit.tau, u=fit.u, v=fit.v, group_trials=fit.group_trials,
             group_cov=fit.group_cov, group_u_cov=fit.group_u_cov,
             focus_cov=fit.focus_cov, focus_cross=fit.focus_cross)
    tmp.replace(path)


# A speaker's position spread (DESIGN.md, "Positions as random effects"): fixed,
# not estimated, at 1 dB rising log-linearly to 4 dB from 5 to 12 kHz. With
# the borrowed noise even slightly off, an estimate soaks up the difference.
SPEAKER_PRIORS = Priors(tau_lo=1.0, tau_mid=2.1, tau_hi=4.0)


def profile_priors(profile: Profile) -> Priors:
    return SPEAKER_PRIORS if profile.kind == "speaker" else Priors()


def cached_fit(profile: Profile, trials: Trials, grid: np.ndarray, noise: Noise,
               priors: Priors | None = None) -> ProbitFit:
    """The perceptual fit depends only on the trials and the listener's noise,
    so it's cached on disk. The seating spread is estimated here, so it's
    estimated once per set of trials: the first time a fit is wanted after a
    session. This is one ear on its own: what the scheduler works from, and
    where each ear's seating spread comes from."""
    priors = profile_priors(profile) if priors is None else priors
    path = _cache_path(profile, trials, grid, noise, priors)
    if path.exists():
        return _load_fit(path)
    fit = fit_hierarchical(trials, grid, priors, noise=noise, fit_tau=profile.kind != "speaker")
    _save_fit(path, fit)
    return fit


# -- the listener's noise (DESIGN.md, "Shared noise") --------------------------

# Profiles whose trials inform the noise. Speakers' short blocks can't: their
# deviations absorb the answers, and the noise collapses.
NOISE_KINDS = ("headphone", "iem")
NOISE_CACHE_KEEP = 200  # newest entries kept


def noise_sessions(profile: Profile) -> list[Session]:
    """The profile's own sessions that inform the listener's noise: those its
    fit uses (`config --sessions`), not counting pooled ones, which count
    toward their own profile."""
    selection = profile_session_ids(profile)
    own = profile.sessions()
    if not selection:
        return own
    chosen = set(selection)
    return [s for s in own if s.id in chosen or profile.slug in chosen]


def listener_noise(root: Path, on_fit: Callable[[], None] | None = None, fit: bool = True) -> dict[str, Noise]:
    """Per ear, the noise shared by every profile in the data folder: fitted to
    their trials together (each profile with its own curve and seatings).
    Only profiles with MIN_TRIALS_NOISE_FIT trials in the ear count; until
    there's one, the prior's values. Cached at the top of the data folder,
    keyed by every member's trials. fit=False: only what's cached (an ear
    that isn't is left out)."""
    sets: dict[str, list] = {e: [] for e in EARS}
    for p in Profile.all(root):
        if p.kind not in NOISE_KINDS:
            continue
        sessions = noise_sessions(p)
        if not sessions:
            continue
        trials, grids = p.history(sessions)
        for e in EARS:
            if len(trials[e]) >= 2:
                sets[e].append((trials[e], grids.get(e, log_grid())))
    out = {}
    folder = root / "cache" / "noise"
    for e in EARS:
        keys = sorted(trials_key(t, g) for t, g in sets[e])
        key = hashlib.sha256(json.dumps([CACHE_VERSION, e, keys]).encode()).hexdigest()[:24]
        path = folder / f"{key}.json"
        if path.exists():
            out[e] = Noise(**json.loads(path.read_text()))
            os.utime(path)
            continue
        if not fit:
            continue
        if on_fit is not None and any(len(t) >= MIN_TRIALS_NOISE_FIT for t, _ in sets[e]):
            on_fit()
            on_fit = None
        out[e] = fit_shared_noise(sets[e])
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + ".tmp")
        tmp.write_text(json.dumps(asdict(out[e])))
        tmp.replace(path)
        for old in sorted(folder.glob("*.json"), key=lambda q: q.stat().st_mtime)[:-NOISE_CACHE_KEEP]:
            old.unlink(missing_ok=True)
    return out


# The prior that the ears' curves are alike (DESIGN.md, "Channel balance") at
# `config --balance 1`. None: each ear on its own, whatever the setting.
BALANCE: BalancePrior | None = BalancePrior()


def balance_prior(profile: Profile) -> BalancePrior | None:
    """The profile's balance prior: the default's SDs times its setting (0: none)."""
    scale = profile_settings(profile).balance
    if BALANCE is None or scale == 0:
        return None
    return replace(BALANCE, lo_db=BALANCE.lo_db * scale, hi_db=BALANCE.hi_db * scale)


def balanced_key(ear_keys: dict[str, str], balance: BalancePrior) -> str:
    h = hashlib.sha256(f"{CACHE_VERSION}|balance|{balance!r}".encode())
    for e in sorted(ear_keys):
        h.update(f"|{e}={ear_keys[e]}".encode())
    return h.hexdigest()[:20]


def cached_balanced(profile: Profile, trials: dict[str, Trials], fits: dict[str, ProbitFit],
                    ear_keys: dict[str, str], balance: BalancePrior) -> tuple[dict[str, ProbitFit], dict[str, str]]:
    """Both ears' curves fitted together under the balance prior, from each
    ear's own fit; cached like those, keyed on both of them."""
    key = balanced_key(ear_keys, balance)
    paths = {e: profile.dir / "cache" / f"balanced-{key}-{e}.npz" for e in fits}
    keys = {e: f"{key}-{e}" for e in fits}
    if all(p.exists() for p in paths.values()):
        return {e: _load_fit(p) for e, p in paths.items()}, keys
    out = fit_balanced(trials, fits, balance)
    for e, p in paths.items():
        _save_fit(p, out[e])
    return out, keys


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
                   noise={e: {"sigma0": f.sigma0, "d0": f.d0, "treble_noise": f.treble_noise, "lapse": f.lapse, "trials": f.n_trials,
                              "noise_trials": f.noise_trials,
                              "time_order_bias_db": f.bias_db, "time_order_bias_sd": f.bias_sd,
                              "tau_seat": [f.tau_lo, f.tau_mid, f.tau_hi], "tau_fitted": f.tau_fitted}
                          for e, f in self.fits.items()})
        return out


# -- per-profile fit settings (`flatiron config`) ----------------------------

def profile_settings(profile: Profile) -> CorrectionSettings:
    """The profile's settings: defaults, with its changes applied."""
    known = {f.name for f in fields(CorrectionSettings)}
    changed = dict(profile.read().get("settings") or {})
    return CorrectionSettings(**{k: v for k, v in changed.items() if k in known})


def set_profile_settings(profile: Profile, settings: CorrectionSettings) -> None:
    """Stores only what differs from the defaults, so improved defaults reach
    existing profiles."""
    default = asdict(CorrectionSettings())
    changed = {k: v for k, v in asdict(settings).items() if v != default[k]}
    profile.update(settings=changed)


def profile_session_ids(profile: Profile) -> list[str] | None:
    """What the fit uses (None: all its sessions and its pooled profiles'): session ids, and
    profile slugs, each standing for all of that profile's sessions (new ones included)."""
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


# -- pooling (`flatiron config --sessions OTHER`) ------------------------------

def pooled_profiles(profile: Profile) -> list[Profile]:
    """Other profiles (the same headphones) whose trials the fit includes."""
    out = []
    for slug in profile.read().get("pool") or []:
        p = Profile(slug, profile.dir.parent.parent)
        if not p.path.exists():
            raise ValueError(f"Profile {slug!r} is pooled into {profile.read()['label']!r} but doesn't exist "
                             f"any more: flatiron config {profile.slug} --reset sessions")
        out.append(p)
    return out


def fit_sessions(profile: Profile) -> list[Session]:
    """The profile's sessions and those of the profiles pooled into it, oldest
    first (the newest session's grid is the one the fit uses)."""
    return [s for _, s in owned_fit_sessions(profile)]


def owned_fit_sessions(profile: Profile) -> list[tuple[str, Session]]:
    """fit_sessions, each with the slug of the profile it belongs to."""
    pairs = [(profile.slug, s) for s in profile.sessions()]
    pairs += [(p.slug, s) for p in pooled_profiles(profile) for s in p.sessions()]
    return sorted(pairs, key=lambda x: datetime.fromisoformat(x[1].started))


def select_sessions(profile: Profile, selection: list[str] | None) -> list[Session]:
    """The sessions a selection (profile_session_ids) picks from fit_sessions."""
    pairs = owned_fit_sessions(profile)
    if not selection:
        return [s for _, s in pairs]
    known = {s.id for _, s in pairs} | {slug for slug, _ in pairs} | {profile.slug}
    missing = set(selection) - known
    if missing:
        raise ValueError(f"no such session(s): {', '.join(sorted(missing))}")
    chosen = set(selection)
    return [s for slug, s in pairs if s.id in chosen or slug in chosen]


def perceptual_fits(profile: Profile, session_ids: list[str] | None = None,
                    on_fit: Callable[[], None] | None = None) -> tuple[dict[str, ProbitFit], dict[str, str], list[Session]]:
    """Per ear, the profile's cached perceptual fit and what it's keyed on. It
    depends only on the trials (its sessions and pooling) and the listener's
    noise, not on the settings. With the balance prior on (the profile's
    `balance` setting), the two ears' curves are fitted together, each with
    the seating spread of its own fit. A consensus or veto member uses its
    own setting."""
    sessions = select_sessions(profile, session_ids)
    trials, grids = profile.history(sessions)
    for e in EARS:
        if len(trials[e]) < 2:
            raise NotEnoughTrials(f"not enough trials for the {e} ear yet ({len(trials[e])})")
    noise = listener_noise(profile.dir.parent.parent, on_fit)
    fits, keys = {}, {}
    for e in EARS:
        grid = grids.get(e, log_grid())
        priors = profile_priors(profile)
        if on_fit is not None and not _cache_path(profile, trials[e], grid, noise[e], priors).exists():
            on_fit()
            on_fit = None
        fits[e] = cached_fit(profile, trials[e], grid, noise[e], priors)
        keys[e] = fit_key(trials[e], grid, noise[e], priors)
    balance = balance_prior(profile)
    if balance is not None:
        fits, keys = cached_balanced(profile, trials, fits, keys, balance)
    for e in EARS:
        fits[e].groups = [g[0].id for g in Profile.groups(sessions, e)]
        fits[e].noise_trials = noise[e].n_trials
    return fits, keys, sessions


# -- the consensus cache (DESIGN.md, "Consensus reference") --------------------

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


def _consensus_key(ear: str, member_keys: dict[str, str]) -> str:
    """The members and each one's fit: a raw consensus depends on nothing else,
    so every profile using the same list shares it."""
    parts = {"version": MODEL_VERSION, "ear": ear, "members": sorted(member_keys.items()),
             "speaker_trust": None, "speaker_response": None}
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


def shares_trials(a: Profile, b: Profile) -> bool:
    """Whether two profiles' fits include some of the same sessions."""
    return bool(({a.slug} | set(a.read().get("pool") or [])) & ({b.slug} | set(b.read().get("pool") or [])))


def _listed_profiles(profile: Profile, slugs, what: str, option: str) -> list[Profile]:
    """The profiles listed for a consensus or a veto, checked. Profiles that
    share trials through pooling can't be used together: pooling means the same
    headphones measured again, and these need different ones."""
    root = profile.dir.parent.parent
    out = []
    for slug in dict.fromkeys(slugs):
        p = Profile(slug, root)
        if not p.path.exists():
            raise ValueError(f"Profile {slug!r} is in the {what} of {profile.read()['label']!r} but doesn't exist "
                             f"any more: flatiron config {option}")
        if p.kind not in KINDS_VOTING:
            raise ValueError(f"{p.read()['label']!r} is of kind {p.kind}, which doesn't count toward a {what}.")
        for other in [profile, *out]:
            if other.slug != slug and shares_trials(p, other):
                raise ValueError(f"{p.read()['label']!r} and {other.read()['label']!r} share trials through pooling, "
                                 f"so they can't be used in one {what}. Pooling means the same headphones measured "
                                 f"again, a {what} different ones.")
        out.append(p)
    return out


def consensus_members(profile: Profile, st: CorrectionSettings) -> list[Profile]:
    """The profiles a consensus reference is built from: exactly the ones
    listed, so this profile only if it's listed."""
    return _listed_profiles(profile, st.consensus, "consensus", "-r diffuse")


def veto_members(profile: Profile, st: CorrectionSettings) -> list[Profile]:
    """The other profiles voting. This profile always votes as well, whether or
    not it's listed: the rule means "does the median headphone, this one
    included, have it", and two others alone have no majority."""
    return [p for p in _listed_profiles(profile, st.veto, "veto", "--veto none") if p.slug != profile.slug]


def ear_consensus(profile: Profile, st: CorrectionSettings, own_fits: dict[str, ProbitFit],
                  own_keys: dict[str, str], on_fit: Callable[[], None] | None = None) -> dict[str, EarConsensus]:
    """Per ear, the raw consensus of the listed profiles as this profile's
    reference. The raw result is cached, one entry per set of members whichever
    profile asks first, and a new session in any member changes its fit and so
    the key; the smoothing depends on the setting, and is applied afterwards."""
    root = profile.dir.parent.parent
    labels, fits, keys = {}, {}, {}
    for p in consensus_members(profile, st):
        labels[p.slug] = p.read()["label"]
        if p.slug == profile.slug:
            fits[p.slug], keys[p.slug] = own_fits, own_keys
        else:
            fits[p.slug], keys[p.slug], _ = perceptual_fits(p, profile_session_ids(p), on_fit)
    order = sorted(fits)
    own = order.index(profile.slug) if profile.slug in order else None
    out, used = {}, {}
    for e in EARS:
        key = _consensus_key(e, {s: keys[s][e] for s in order})
        path = consensus_dir(root) / f"{key}.npz"
        if path.exists():
            z = np.load(path)
            os.utime(path)
            sm = SoftMedian(z["grid"], z["m"], z["cov"], float(z["tau_hp"]), [z[f"G{i}"] for i in range(len(order))],
                            bool(z["robust"]), z["offsets"])
        else:
            sm = raw_consensus([Source(labels[s], fits[s][e].grid, fits[s][e].L, fits[s][e].cov) for s in order])
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.stem + ".tmp.npz")
            np.savez(tmp, grid=sm.grid, m=sm.m, cov=sm.cov, tau_hp=sm.tau_hp, robust=sm.robust, offsets=sm.offsets,
                     members=np.array(order), **{f"G{i}": g for i, g in enumerate(sm.G)})
            tmp.replace(path)
            _prune_consensus(root)
        out[e] = ear_reference(sm, [labels[s] for s in order], key, st.consensus_smooth_oct, own)
        used[e] = key
    (profile.dir / "cache").mkdir(parents=True, exist_ok=True)
    (profile.dir / "cache" / "consensus.json").write_text(json.dumps(used))
    return out


def ear_voters(profile: Profile, st: CorrectionSettings,
               on_fit: Callable[[], None] | None = None) -> dict[str, list[tuple[str, ProbitFit]]]:
    """Per ear, the other profiles' fits for --veto."""
    others = [(p.read()["label"], perceptual_fits(p, profile_session_ids(p), on_fit)[0])
              for p in veto_members(profile, st)]
    return {e: [(label, fits[e]) for label, fits in others] for e in EARS}


def fit_profile(profile: Profile, settings: CorrectionSettings = CorrectionSettings(),
                session_ids: list[str] | None = None,
                fixed: dict[str, list[PeakingFilter]] | None = None,
                on_fit: Callable[[], None] | None = None) -> ProfileFit:
    """on_fit is called once if a perceptual fit isn't cached and has to be
    computed (it takes a few seconds)."""
    fits, keys, sessions = perceptual_fits(profile, session_ids, on_fit)
    if profile.kind == "speaker":  # positions are meant to differ: no seating step
        settings = replace(settings, consistency=False)
    consensus = ear_consensus(profile, settings, fits, keys, on_fit) if settings.consensus else None
    voters = ear_voters(profile, settings, on_fit) if settings.veto else None
    corr = correct(fits, settings, fixed, consensus, voters)
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
