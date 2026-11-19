"""What the report shows of a profile's threshold sessions (DESIGN.md,
"Threshold-anchored reference"): per ear, the loudness-only curve, the
threshold curve (negated, as a sensitivity), and with the threshold fit on
the joint curve; and the listener's loudness-threshold gap."""

from dataclasses import dataclass

import numpy as np

from flatiron import EARS
from flatiron.fit.run import ProfileFit
from flatiron.model.grid import interp_matrix, log_grid
from flatiron.session.store import Profile

RANGE_HZ = (1000.0, 13000.0)


@dataclass
class Curve:
    grid: np.ndarray
    values: np.ndarray  # mean over the shared range removed (the threshold: the loudness curve's mean)
    sd: np.ndarray


@dataclass
class ThresholdView:
    grid: np.ndarray  # 1 to 13 kHz within the measured range
    loudness: dict  # ear: Curve, the loudness-only fit
    threshold: dict  # ear: Curve, −T
    joint: dict | None  # ear: Curve, with the threshold fit on
    gap: object  # fit.gap.ListenerGap or None
    slug: str
    trials: int
    sessions: int
    sigma: float

    def notes(self) -> list[str]:
        out = [f"Thresholds: {self.trials} trials in {self.sessions} session{'' if self.sessions == 1 else 's'}, "
               f"psychometric spread {self.sigma:.1f} dB."]
        g = self.gap
        if g is None:
            out.append("Loudness-threshold gap: not estimated yet (150 threshold trials in a profile).")
            return out
        k = np.asarray(g.g)
        sd = np.sqrt(np.diag(np.asarray(g.cov)))
        line = (f"Loudness-threshold gap (yours, from {len(g.profiles)} profile{'' if len(g.profiles) == 1 else 's'}): "
                f"{k[4] - k[2]:+.1f} ± {np.hypot(sd[4], sd[2]):.1f} dB from 4 to 13 kHz; ripple {g.ripple_sd:.1f} dB.")
        d = g.profiles.get(self.slug)
        if d is not None:
            line += (f" This profile's own below 3 kHz: {d['delta'][0]:+.1f} ± {d['delta_sd'][0]:.1f} dB at 1 kHz, "
                     f"{d['delta'][1]:+.1f} ± {d['delta_sd'][1]:.1f} at 2 kHz.")
        out.append(line)
        return out


def _curve(fit, G) -> Curve:
    A = interp_matrix(fit.grid, G)
    v = A @ fit.L
    return Curve(G, v - v.mean(), np.sqrt(np.clip(np.einsum("ij,jk,ik->i", A, fit.cov, A), 0, None)))


def threshold_view(profile: Profile, pf: ProfileFit) -> ThresholdView | None:
    """None without threshold sessions."""
    from flatiron.fit.gap import listener_gap, threshold_fit
    from flatiron.fit.run import perceptual_fits, profile_session_ids

    tf = threshold_fit(profile)
    if tf is None or profile.kind == "speaker":
        return None
    lo, hi = profile.measured
    G = log_grid(max(RANGE_HZ[0], lo), min(RANGE_HZ[1], hi))
    st = pf.correction.settings
    joint_on = bool(st.threshold_fit and pf.fits["L"].interaural and pf.fits["L"].interaural.get("threshold_fit"))
    plain = perceptual_fits(profile, profile_session_ids(profile), thresholds=False)[0] if joint_on else pf.fits
    loud = {e: _curve(plain[e], G) for e in EARS}
    M = interp_matrix(tf.grid, G)
    n = tf.grid.size
    thr = {}
    for i, e in enumerate(EARS):
        v = -(M @ tf.T[i])
        c = tf.cov[i * n:(i + 1) * n, i * n:(i + 1) * n]
        thr[e] = Curve(G, v - v.mean(), np.sqrt(np.clip(np.einsum("ij,jk,ik->i", M, c, M), 0, None)))
    joint = {e: _curve(pf.fits[e], G) for e in EARS} if joint_on else None
    gap = pf.gap if pf.gap is not None else listener_gap(profile.dir.parent.parent)
    ss = profile.threshold_sessions()
    return ThresholdView(G, loud, thr, joint, gap, profile.slug, tf.n_signal, len(ss), tf.sigma)
