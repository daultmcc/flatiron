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


@dataclass
class Curve:
    grid: np.ndarray
    values: np.ndarray  # mean over the shared range removed (the threshold: the loudness curve's mean)
    sd: np.ndarray


@dataclass
class ThresholdView:
    grid: np.ndarray  # the listener's default measured range, within the profile's
    loudness: dict  # ear: Curve, the loudness-only fit
    threshold: dict  # ear: Curve, −T
    joint: dict | None  # ear: Curve, with the threshold fit on: the measured response at threshold (gap out)
    gap: object  # fit.gap.ListenerGap or None
    slug: str
    trials: int
    sessions: int
    sigma: float

    def notes(self) -> list[str]:
        """For the terminal: the counts, then the fitted values."""
        return [f"Thresholds: {self.trials} trials in {self.sessions} session{'' if self.sessions == 1 else 's'}."] \
            + self.values()

    def values(self) -> list[str]:
        """The fitted values, one per line."""
        out = [f"Psychometric spread: {self.sigma:.1f} dB"]
        g = self.gap
        if g is None:
            out.append("Loudness-threshold gap: not estimated yet (150 threshold trials in a profile)")
            return out
        rise, rise_sd, top = gap_rise(g)
        out += [f"Loudness-threshold gap, 4 to {top / 1000:g} kHz: {rise:+.1f} ± {rise_sd:.1f} dB "
                f"(the listener's, from {len(g.profiles)} profile{'' if len(g.profiles) == 1 else 's'})",
                f"Gap ripple: {g.ripple_sd:.1f} dB"]
        d = g.profiles.get(self.slug)
        if d is not None:
            out.append(f"This profile's own gap below 3 kHz: {d['delta'][0]:+.1f} ± {d['delta_sd'][0]:.1f} dB at 1 kHz, "
                       f"{d['delta'][1]:+.1f} ± {d['delta_sd'][1]:.1f} at 2 kHz")
        return out


def gap_rise(gap, f_from: float = 4000.0) -> tuple[float, float, float]:
    """g's rise from f_from to its top knot, its SD, and the top knot."""
    from flatiron.fit.gap import hats

    top = float(gap.knots_hz[-1])
    w = np.diff(hats([f_from, top], gap.knots_hz), axis=0)[0]
    return float(w @ np.asarray(gap.g)), float(np.sqrt(max(w @ np.asarray(gap.cov) @ w, 0.0))), top


def _curve(fit, G) -> Curve:
    A = interp_matrix(fit.grid, G)
    v = A @ fit.L
    return Curve(G, v - v.mean(), np.sqrt(np.clip(np.einsum("ij,jk,ik->i", A, fit.cov, A), 0, None)))


def threshold_view(profile: Profile, pf: ProfileFit) -> ThresholdView | None:
    """None without threshold sessions."""
    from flatiron.fit.gap import gap_grid, listener_gap, listener_range, threshold_fit
    from flatiron.fit.run import perceptual_fits, profile_session_ids

    tf = threshold_fit(profile)
    if tf is None or profile.kind == "speaker":
        return None
    st = pf.correction.settings
    joint_on = bool(st.threshold_fit and pf.fits["L"].interaural and pf.fits["L"].interaural.get("threshold_fit"))
    plain = perceptual_fits(profile, profile_session_ids(profile), thresholds=False)[0] if joint_on else pf.fits
    # As g sees it: the listener's range, where every curve was fitted (beyond a
    # grid, interpolation would hold its end value).
    G = gap_grid(profile, *listener_range(profile.dir.parent.parent), tf.grid, *(plain[e].grid for e in EARS))
    if G is None:
        G = log_grid(*profile.measured)
    loud = {e: _curve(plain[e], G) for e in EARS}
    M = interp_matrix(tf.grid, G)
    n = tf.grid.size
    thr = {}
    for i, e in enumerate(EARS):
        v = -(M @ tf.T[i])
        c = tf.cov[i * n:(i + 1) * n, i * n:(i + 1) * n]
        thr[e] = Curve(G, v - v.mean(), np.sqrt(np.clip(np.einsum("ij,jk,ik->i", M, c, M), 0, None)))
    gap = pf.gap if pf.gap is not None else listener_gap(profile.dir.parent.parent)
    joint = None
    if joint_on:
        from flatiron.fit.run import pinned_fits

        pinned = pinned_fits(profile, pf.fits)
        joint = {e: _curve(pinned[e], G) for e in EARS}
    ss = profile.threshold_sessions()
    return ThresholdView(G, loud, thr, joint, gap, profile.slug, tf.n_signal, len(ss), tf.sigma)
