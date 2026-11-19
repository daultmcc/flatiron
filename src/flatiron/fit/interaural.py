"""Cross-ear, centring and threshold data in the two-ear fit (DESIGN.md,
"Using cross-ear trials").

A cross-ear trial compares the ears' curves, offset by b + c_p: b the
listener's bias in comparing loudness across ears (the same for every
profile), c_p the profile's own overall balance (left minus right). A
centring trial sees c_p alone, and a profile's threshold sessions give a
direct, if rough, observation of it (T_R − T_L). Each ear's curve enters with
its mean over the reference range (1 to 13 kHz within the measured range)
removed, so c_p is the balance over that range, whatever the fit's gauge.

b is the listener's, like the noise: fitted once to every member profile
together (each with its own curves, balance prior and c_p), cached, and held
fixed in each profile's own fit, where the cross-ear trials then shape L_L −
L_R and the profile's c_p.
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from scipy.optimize import minimize_scalar

from flatiron import EARS
from flatiron.audio.stimulus import POINT, StimulusSpec, stimulus_key
from flatiron.model.grid import interp_matrix, log_grid
from flatiron.model.hierarchical import Extra, fit_joint
from flatiron.model.probit import ProbitFit, band_matrix
from flatiron.session.cross import cross_range
from flatiron.session.store import Profile, Session

C_SD_DB = 1.5  # prior SD of a profile's overall balance c_p (about a driver mismatch)
B_SD_DB = 10.0  # prior SD of the listener's cross-ear bias: no real pull
LAPSE = 0.02
SIGMA_START_DB = 2.5  # a judgment noise before it's estimated (and σ_c without centring data)
SIGMA_MIN_DB, SIGMA_MAX_DB = 0.3, 12.0
# Log-normal prior on both: few trials aimed near the 50% point can be all
# but separable, and then nothing else stops a noise collapsing.
SIGMA_PRIOR_DB, SIGMA_LOG_SD = 2.5, 0.7
MIN_CENTRING_BW_OCT = 0.5  # narrower bands gave no stable image to place (DESIGN.md, "Centring sessions")
MIN_THRESHOLD_SIGNAL = 100  # signal trials before a profile's thresholds count
THRESHOLD_SD_SCALE = 1.3  # the threshold fit's error bars covered ~75% in simulation: widened
BIAS_CACHE_KEEP = 200
VERSION = "1"


@dataclass
class ProfileRows:
    """One profile's observations, on its own block of parameters (L_L,
    bias_L, L_R, bias_R, in the fits' grids)."""
    cross_X: np.ndarray
    cross_off: np.ndarray
    cross_y: np.ndarray
    cross_sign: np.ndarray  # +1: A in the left ear (b + c_p adds), −1: in the right
    cent_X: np.ndarray
    cent_off: np.ndarray
    cent_y: np.ndarray
    threshold: tuple[float, float] | None  # T_R − T_L over the reference range, and its SD
    ref: tuple[float, float]

    @property
    def n_cross(self) -> int:
        return int(self.cross_y.size)

    @property
    def n_centring(self) -> int:
        return int(self.cent_y.size)

    def empty(self) -> bool:
        return not self.n_cross and not self.n_centring and self.threshold is None

    def c_prior(self) -> tuple[float, float]:
        """c_p's prior with the threshold observation folded in."""
        if self.threshold is None:
            return 0.0, C_SD_DB
        m, s = self.threshold
        prec = 1 / C_SD_DB**2 + 1 / s**2
        return m / s**2 / prec, float(np.sqrt(1 / prec))

    def key(self) -> str:
        h = hashlib.sha256(VERSION.encode())
        for a in (self.cross_X, self.cross_off, self.cross_y, self.cross_sign, self.cent_X, self.cent_off,
                  self.cent_y, np.asarray(self.threshold or (np.nan, np.nan)), np.asarray(self.ref)):
            h.update(np.ascontiguousarray(a, float).tobytes())
        return h.hexdigest()[:20]


def _ear_rows(grids: dict, ref_grid: np.ndarray, ear: str, freqs, stimulus: list) -> np.ndarray:
    """Rows over the block for each frequency in one ear, the ear's mean over the reference range removed."""
    nL, nR = grids["L"].size, grids["R"].size
    out = np.zeros((len(freqs), nL + nR + 2))
    rows = band_matrix(grids[ear], np.asarray(freqs, float), stimulus)
    rows = rows - interp_matrix(grids[ear], ref_grid).mean(axis=0)
    if ear == "L":
        out[:, :nL] = rows
    else:
        out[:, nL + 1:nL + 1 + nR] = rows
    return out


def _threshold_observation(profile: Profile, ref_grid: np.ndarray) -> tuple[float, float] | None:
    from flatiron.fit.gap import threshold_fit

    fit = threshold_fit(profile)
    if fit is None or fit.n_signal < MIN_THRESHOLD_SIGNAL:
        return None
    n = fit.grid.size
    w = interp_matrix(fit.grid, ref_grid).mean(axis=0)
    J = np.concatenate([-w, w])  # T_R − T_L
    mean = float(J @ fit.T.ravel())
    sd = float(np.sqrt(J @ fit.cov[:2 * n, :2 * n] @ J))
    return mean, sd * THRESHOLD_SD_SCALE


_THRESHOLD_MEMO: dict = {}


def profile_rows(profile: Profile, sessions: list[Session], grids: dict) -> ProfileRows:
    """The profile's cross-ear trials (from `sessions`), centring trials and threshold observation."""
    lo, hi = cross_range(profile.measured)
    ref = log_grid(lo, hi)
    cX, coff, cy, csg = [], [], [], []
    for s in sessions:
        key = stimulus_key(StimulusSpec(**s.stimulus)) if s.stimulus else POINT
        for t in s.cross_trials():
            if t.undone:
                continue
            x = _ear_rows(grids, ref, t.ear[0], [t.f_a], [key]) - _ear_rows(grids, ref, t.ear[1], [t.f_b], [key])
            o = 1.0 if t.a_first else -1.0
            nL = grids["L"].size
            x[0, nL] += 0.5 * o  # the time-order bias: the mean of the ears'
            x[0, -1] += 0.5 * o
            cX.append(x[0]), coff.append(t.offset), cy.append(float(t.a_louder))
            csg.append(1.0 if t.ear[0] == "L" else -1.0)
    zX, zoff, zy = [], [], []
    for s in profile.centring_sessions():
        if s.stimulus.get("kind", "noise") != "noise" or s.stimulus.get("bandwidth_oct", 0) < MIN_CENTRING_BW_OCT:
            continue
        key = s.stimulus_key()
        for t in s.live():
            x = _ear_rows(grids, ref, "L", [t.freq], [key]) - _ear_rows(grids, ref, "R", [t.freq], [key])
            zX.append(x[0]), zoff.append(t.ild), zy.append(float(t.left))
    from flatiron.fit.gap import threshold_key

    tk = f"{profile.dir}|{threshold_key(profile)}|{lo}|{hi}"
    if tk not in _THRESHOLD_MEMO:
        _THRESHOLD_MEMO[tk] = _threshold_observation(profile, ref)
    width = grids["L"].size + grids["R"].size + 2
    arr = lambda rows: np.array(rows) if rows else np.zeros((0, width))
    return ProfileRows(arr(cX), np.array(coff), np.array(cy), np.array(csg), arr(zX), np.array(zoff), np.array(zy),
                       _THRESHOLD_MEMO[tk], (lo, hi))


def empty_rows(profile: Profile, grids: dict) -> ProfileRows:
    """No cross-ear, centring or threshold observations (the cross-ear fit off)."""
    width = grids["L"].size + grids["R"].size + 2
    z = np.zeros(0)
    return ProfileRows(np.zeros((0, width)), z, z, z, np.zeros((0, width)), z, z, None,
                       cross_range(profile.measured))


@dataclass
class ListenerBias:
    b: float
    b_sd: float
    sigma_x: float  # cross-ear judgment noise
    sigma_c: float  # centring judgment noise
    profiles: list[str]
    c: dict  # per member slug: (c_p, SD), as the listener fit has them
    key: str = ""


def _rows_extra(rows_list: list[ProfileRows], widths: list[int], b: float | None, sigma_x: float,
                sigma_c: float) -> Extra:
    """The Extra for these profiles. b None: b is a parameter (the first extra); else it's held at b."""
    P = len(rows_list)
    nb = 1 if b is None else 0
    N = sum(widths) + nb + P
    X, off, y, sig = [], [], [], []
    start = 0
    for p, (r, w) in enumerate(zip(rows_list, widths)):
        ic = sum(widths) + nb + p
        if r.n_cross:
            x = np.zeros((r.n_cross, N))
            x[:, start:start + w] = r.cross_X
            x[:, ic] = r.cross_sign
            o = r.cross_off.copy()
            if b is None:
                x[:, sum(widths)] = r.cross_sign
            else:
                o = o + r.cross_sign * b
            X.append(x), off.append(o), y.append(r.cross_y), sig.append(np.full(r.n_cross, sigma_x))
        if r.n_centring:
            x = np.zeros((r.n_centring, N))
            x[:, start:start + w] = r.cent_X
            x[:, ic] = 1.0
            X.append(x), off.append(r.cent_off), y.append(r.cent_y), sig.append(np.full(r.n_centring, sigma_c))
        start += w
    priors = [r.c_prior() for r in rows_list]
    mean = np.array(([0.0] if b is None else []) + [m for m, _ in priors])
    sd = np.array(([B_SD_DB] if b is None else []) + [s for _, s in priors])
    if not X:
        X, off, y, sig = [np.zeros((0, N))], [np.zeros(0)], [np.zeros(0)], [np.zeros(0)]
    return Extra(np.vstack(X), np.concatenate(off), np.concatenate(y), np.concatenate(sig), LAPSE, mean, sd)


def fit_listener_bias(members: list[tuple[str, dict, dict, object, object, ProfileRows]]) -> ListenerBias:
    """members: (slug, trials, fits, balance, priors, rows). The two judgment
    noises (cross-ear σ_x, centring σ_c) maximise the Laplace evidence (with a weak log-normal prior), one
    then the other (their trials don't overlap); fitting them to the curves
    they shape would shrink them (as for the within-ear noise: "not a joint
    MAP")."""
    blocks = [(t, f, bal, pr) for _, t, f, bal, pr, _ in members]
    rows = [m[5] for m in members]
    widths = [f["L"].grid.size + f["R"].grid.size + 2 for _, _, f, _, _, _ in members]
    has_centring = any(r.n_centring for r in rows)

    def neg_ev(sx, sc):
        return fit_joint(blocks, _rows_extra(rows, widths, None, sx, sc)).neg_evidence

    def prior(ls):
        z = (ls - np.log(SIGMA_PRIOR_DB)) / SIGMA_LOG_SD
        return 0.5 * z * z + ls

    def best(f):
        r = minimize_scalar(lambda ls: f(float(np.exp(ls))) + prior(ls),
                            bounds=(np.log(SIGMA_MIN_DB), np.log(SIGMA_MAX_DB)), method="bounded",
                            options={"xatol": 0.05})
        return float(np.exp(r.x))

    sx = best(lambda s: neg_ev(s, SIGMA_START_DB))
    sc = best(lambda s: neg_ev(sx, s)) if has_centring else SIGMA_START_DB
    res = fit_joint(blocks, _rows_extra(rows, widths, None, sx, sc))
    sd = np.sqrt(np.diag(res.extra_cov))
    return ListenerBias(float(res.extra[0]), float(sd[0]), sx, sc if has_centring else float("nan"),
                        [m[0] for m in members],
                        {m[0]: (float(res.extra[1 + p]), float(sd[1 + p])) for p, m in enumerate(members)})


def listener_bias(root: Path, on_fit: Callable[[], None] | None = None) -> ListenerBias | None:
    """The listener's cross-ear bias, from every headphone or IEM profile with
    cross-ear trials and the setting on; cached at the top of the data folder,
    keyed by everything it depends on. None: no such profile."""
    from flatiron.fit.run import (NOISE_KINDS, balance_prior, cached_fit, fit_key, listener_noise, noise_sessions,
                                  profile_priors, profile_settings)

    members = []
    noise = None
    for p in Profile.all(root):
        if p.kind not in NOISE_KINDS or not profile_settings(p).cross_ear_fit:
            continue
        sessions = noise_sessions(p)
        if not any(s.cross_trials() for s in sessions):
            continue
        trials, grids = p.history(sessions)
        if any(len(trials[e]) < 2 for e in EARS):
            continue
        noise = noise or listener_noise(root, on_fit)
        priors = profile_priors(p)
        fits = {e: cached_fit(p, trials[e], grids.get(e, log_grid()), noise[e], priors) for e in EARS}
        rows = profile_rows(p, sessions, {e: fits[e].grid for e in EARS})
        if not rows.n_cross:
            continue
        keys = [fit_key(trials[e], grids.get(e, log_grid()), noise[e], priors) for e in EARS]
        members.append((p.slug, trials, fits, balance_prior(p), priors, rows, keys))
    if not members:
        return None
    key = hashlib.sha256(json.dumps([VERSION, C_SD_DB, B_SD_DB, LAPSE] + sorted(
        [m[0], m[6], repr(m[3]), m[5].key()] for m in members)).encode()).hexdigest()[:24]
    folder = root / "cache" / "bias"
    path = folder / f"{key}.json"
    if path.exists():
        os.utime(path)
        return ListenerBias(**json.loads(path.read_text()))
    if on_fit is not None:
        on_fit()
    out = fit_listener_bias([m[:6] for m in members])
    out.key = key
    folder.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp")
    tmp.write_text(json.dumps(asdict(out)))
    tmp.replace(path)
    for old in sorted(folder.glob("*.json"), key=lambda q: q.stat().st_mtime)[:-BIAS_CACHE_KEEP]:
        old.unlink(missing_ok=True)
    return out


@dataclass
class ThresholdRows:
    """A profile's threshold trials as rows of its two-ear fit (DESIGN.md,
    "Threshold-anchored reference"). Ear e's threshold is
        T_e(f) = k + c_s − (L̃_e(f) − g(f) − δ(f)) + m_e(f) ∓ c_p/2
    (− in the left ear: a louder left channel is heard sooner), with g and δ
    the listener's gap, held fixed; k the profile's threshold level; c_s each
    later session's offset; m_e the threshold's ripple, in a whitened basis
    (prior N(0, 1) per coefficient). The burst's interval is picked with
    chance γ_s + (1 − γ_s − λ) Φ((x − T̄_e(f)) / σ), γ, λ and σ from the
    profile's own threshold fit."""
    X: np.ndarray  # n x block width: L̃_e's band rows
    half: np.ndarray  # c_p's coefficient: +½ in the left ear, −½ in the right
    session: np.ndarray
    M: np.ndarray  # n x q: −m's band rows, whitened
    off: np.ndarray  # x − (g + δ), band averaged
    y: np.ndarray
    floor: np.ndarray  # γ of the trial's session
    lapse: float
    sigma: float
    k0: float  # starting values, from the threshold fit
    c0: np.ndarray
    n_sessions: int
    gap_key: str

    @property
    def n(self) -> int:
        return int(self.y.size)

    @property
    def n_params(self) -> int:
        return 1 + (self.n_sessions - 1) + self.M.shape[1]

    def key(self) -> str:
        h = hashlib.sha256((VERSION + self.gap_key).encode())
        for a in (self.X, self.half, self.session, self.M, self.off, self.y, self.floor,
                  np.asarray([self.lapse, self.sigma])):
            h.update(np.ascontiguousarray(a, float).tobytes())
        return h.hexdigest()[:20]


RIPPLE_KEEP = 1e-4  # whitened basis: eigenvectors of the ripple's correlation above this share of the largest


def threshold_rows(profile: Profile, grids: dict, gap, fits: dict[str, ProbitFit]) -> ThresholdRows | None:
    """The profile's threshold trials, with the listener's gap `gap`
    (fit/gap.py), or None if it has none."""
    from flatiron.fit.gap import RIPPLE_ELL_OCT, threshold_fit
    from flatiron.model.threshold import ThresholdData

    tf = threshold_fit(profile)
    if tf is None:
        return None
    ss = profile.threshold_sessions()
    data = ThresholdData.build([(s.stimulus_key(), s.trials) for s in ss])
    lo, hi = cross_range(profile.measured)
    ref = log_grid(lo, hi)
    tg = tf.grid
    x = np.log2(tg)
    K = np.exp(-0.5 * ((x[:, None] - x[None, :]) / RIPPLE_ELL_OCT) ** 2)
    val, vec = np.linalg.eigh(K)
    keep = val > RIPPLE_KEEP * val.max()
    W = gap.ripple_sd * vec[:, keep] * np.sqrt(val[keep])  # m = W z, z ~ N(0, I)
    q = W.shape[1]
    curve = gap.curve_for(profile.slug, tg)
    n = data.freq.size
    X, M = [], np.zeros((n, 2 * q))
    off = np.empty(n)
    for t in range(n):
        e = EARS[data.ear[t]]
        row = band_matrix(tg, [data.freq[t]], [data.stimulus[t]])[0]
        X.append(_ear_rows(grids, ref, e, [data.freq[t]], [data.stimulus[t]])[0])
        M[t, data.ear[t] * q:(data.ear[t] + 1) * q] = -(row @ W)
        off[t] = data.level[t] - row @ curve
    half = np.where(data.ear == 0, 0.5, -0.5)
    # k's starting value: over the reference range L̃ averages 0, so k is the
    # threshold fit's first-session level there less the gap's.
    w = interp_matrix(tg, ref).mean(axis=0)
    k0 = float(np.mean([w @ tf.T[i] for i in range(2)]) - w @ curve)
    return ThresholdRows(np.array(X), half, data.session, M, off, data.positive.astype(float),
                         tf.gamma[data.session], tf.lapse, tf.sigma, float(k0), np.asarray(tf.c[1:], float),
                         data.n_sessions, gap.key)


K_SD_DB = 1e3  # the threshold level: no real pull
OFFSET_SD_DB = 40.0  # a later threshold session's offset, as in the threshold model


def profile_extra(rows: ProfileRows, fits: dict[str, ProbitFit], bias: ListenerBias | None,
                  thr: ThresholdRows | None = None) -> Extra | None:
    """The profile's own fit: b held at the listener's, c_p a parameter.
    Without a listener bias, cross-ear trials can't be used (centring and
    thresholds still can). With threshold rows (`thr`), the threshold trials
    are rows of the fit, and their observation of c_p isn't folded into its
    prior as well; their parameters (k, the later sessions' offsets, the
    ripple) follow c_p."""
    if bias is None:
        rows = ProfileRows(rows.cross_X[:0], rows.cross_off[:0], rows.cross_y[:0], rows.cross_sign[:0],
                           rows.cent_X, rows.cent_off, rows.cent_y, rows.threshold, rows.ref)
    if thr is not None:
        rows = ProfileRows(rows.cross_X, rows.cross_off, rows.cross_y, rows.cross_sign, rows.cent_X,
                           rows.cent_off, rows.cent_y, None, rows.ref)
    elif rows.empty():
        return None
    w = fits["L"].grid.size + fits["R"].grid.size + 2
    base = _rows_extra([rows], [w], 0.0 if bias is None else bias.b,
                       SIGMA_START_DB if bias is None else bias.sigma_x,
                       SIGMA_START_DB if bias is None or np.isnan(bias.sigma_c) else bias.sigma_c)
    if thr is None:
        return base
    nb, nt = base.X.shape[0], thr.n
    N = w + 1 + thr.n_params
    X = np.zeros((nb + nt, N))
    X[:nb, :w + 1] = base.X
    X[nb:, :w] = thr.X
    X[nb:, w] = thr.half
    X[nb:, w + 1] = -1.0  # k
    later = thr.session > 0
    X[nb + np.flatnonzero(later), w + 1 + thr.session[later]] = -1.0  # c_s, s ≥ 1
    X[nb:, w + 1 + thr.n_sessions:] = thr.M
    lam = base.lapse / 2
    return Extra(X, np.concatenate([base.off, thr.off]), np.concatenate([base.y, thr.y]),
                 np.concatenate([base.sigma, np.full(nt, thr.sigma)]), base.lapse,
                 np.concatenate([base.prior_mean, [thr.k0], thr.c0, np.zeros(thr.M.shape[1])]),
                 np.concatenate([base.prior_sd, [K_SD_DB], np.full(thr.n_sessions - 1, OFFSET_SD_DB),
                                 np.ones(thr.M.shape[1])]),
                 floor=np.concatenate([np.full(nb, lam), thr.floor]),
                 ceil=np.concatenate([np.full(nb, lam), np.full(nt, thr.lapse)]))
