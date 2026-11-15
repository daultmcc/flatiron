"""Per-ear report figures (matplotlib → PNG), in a light and a dark variant.

Panels share a log-frequency axis:
  1. measured level relative to the reference (±1 SD) and the baseline
  2. residual (±1 SD), the correction, and the predicted result, with the
     corrected features shaded
  3. (two or more sessions) each seating's residual against the average;
     above MAX_SESSION_LINES sessions, the range holding the middle 90% of
     them (weighted by their trials) instead of a line each
A second figure (reference_figure) draws the reference in use; for a consensus,
its parts and the share of its narrow part kept.
Colors are the dataviz reference palette's first three categorical slots,
validated all-pairs in both modes; lines are direct-labelled because the light
aqua sits under 3:1 contrast.
"""

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import FixedLocator, NullLocator  # noqa: E402

from flatiron.fit.filters import response  # noqa: E402
from flatiron.fit.pipeline import EarCorrection  # noqa: E402
from flatiron.model.grid import interp_matrix  # noqa: E402

THEMES = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781", grid="#e1e0d9",
                  axis="#c3c2b7", s1="#2a78d6", s2="#eb6834", s3="#1baf7a", shade="#e9e8e2"),
    "dark": dict(surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", muted="#898781", grid="#2c2c2a",
                 axis="#383835", s1="#3987e5", s2="#d95926", s3="#199e70", shade="#262624"),
}
TICKS = [1000, 1500, 2000, 3000, 4000, 6000, 8000, 10000, 13000]
EAR_NAMES = {"L": "Left", "R": "Right"}
MAX_SESSION_LINES = 5


def weighted_quantiles(values: np.ndarray, weights, qs) -> np.ndarray:
    """Per column of `values` (sessions × points), the weighted quantiles qs
    (midpoint rule: each session's weight is centred on its value)."""
    w = np.asarray(weights, float)
    order = np.argsort(values, axis=0)
    v = np.take_along_axis(values, order, axis=0)
    ws = w[order]
    cum = (np.cumsum(ws, axis=0) - 0.5 * ws) / w.sum()
    return np.array([[np.interp(q, cum[:, j], v[:, j]) for j in range(v.shape[1])] for q in qs])


def _style(ax, t, f_lo, f_hi):
    ax.set_facecolor(t["surface"])
    ax.set_xscale("log")
    ax.set_xlim(f_lo, f_hi)
    ax.xaxis.set_major_locator(FixedLocator(TICKS))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.set_xticklabels([f"{f / 1000:g}k" for f in TICKS])
    ax.grid(True, color=t["grid"], linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["axis"])
    ax.tick_params(colors=t["muted"], labelsize=8, length=0)
    ax.axhline(0, color=t["axis"], linewidth=0.8)


def _label_end(ax, x, y, text, color, t, dy=0.0):
    """Direct label at a line's right end: a short colored tick plus ink text."""
    ax.plot([x * 1.01, x * 1.04], [y + dy] * 2, color=color, linewidth=2, solid_capstyle="round", clip_on=False)
    ax.annotate(text, (x * 1.05, y + dy), color=t["ink2"], fontsize=8, va="center", annotation_clip=False)


def ear_figure(c: EarCorrection, session_R: list[np.ndarray] | None = None, theme: str = "light",
               f_lo: float = 1000, f_hi: float = 13000, speaker: bool = False) -> bytes:
    """speaker: no correction, and the groups are positions, not seatings."""
    t = THEMES[theme]
    with_sessions = bool(session_R) and len(session_R) >= 2
    n = 3 if with_sessions else 2
    fig, axes = plt.subplots(n, 1, figsize=(8.2, 2.7 * n + 0.4), sharex=True, facecolor=t["surface"],
                             gridspec_kw={"hspace": 0.38})
    plt.rcParams["font.family"] = "sans-serif"
    g = c.grid
    D = c.L - c.E
    xr = g[-1]
    m = interp_matrix(g, c.freqs)
    R_f, sd_f = m @ c.R, c.sd
    H = response(c.added, c.freqs)  # with a checkpoint, R already includes its filters

    ax = axes[0]
    _style(ax, t, g[0], g[-1])
    ax.fill_between(g, D - c.L_sd, D + c.L_sd, color=t["s1"], alpha=0.18, linewidth=0)
    ax.plot(g, D, color=t["s1"], linewidth=2)
    if c.baseline_sd is not None:
        ax.fill_between(g, c.baseline - c.baseline_sd, c.baseline + c.baseline_sd, color=t["s2"], alpha=0.18,
                        linewidth=0)
    ax.plot(g, c.baseline, color=t["s2"], linewidth=2)
    _label_end(ax, xr, D[-1], "measured − reference", t["s1"], t, dy=1.0 if D[-1] >= c.baseline[-1] else -1.0)
    _label_end(ax, xr, c.baseline[-1], "baseline" if c.baseline_sd is None else "baseline ±1 SD", t["s2"], t, dy=-1.0 if D[-1] >= c.baseline[-1] else 1.0)
    ax.set_title(f"{EAR_NAMES.get(c.ear, c.ear)} ear · measured level relative to the reference (±1 SD)",
                 color=t["ink"], fontsize=10, loc="left")
    ax.set_ylabel("dB", color=t["muted"], fontsize=8)

    ax = axes[1]
    _style(ax, t, g[0], g[-1])
    for f in c.features:
        if f.accepted:
            ax.axvspan(g[f.lo], g[f.hi], color=t["shade"], linewidth=0, zorder=0)
    ax.fill_between(c.freqs, R_f - sd_f, R_f + sd_f, color=t["s1"], alpha=0.18, linewidth=0)
    ax.plot(c.freqs, R_f, color=t["s1"], linewidth=2)
    if speaker:
        ends = [(R_f[-1], "residual", t["s1"])]
    else:
        ax.plot(c.freqs, H, color=t["s2"], linewidth=2)
        ax.plot(c.freqs, R_f + H, color=t["s3"], linewidth=2)
        ends = sorted([(R_f[-1], "residual", t["s1"]), (H[-1], "correction", t["s2"]),
                       ((R_f + H)[-1], "after correction", t["s3"])])
    span = max(np.ptp(np.concatenate([R_f, H])), 4)
    for k, (y, text, col) in enumerate(ends):
        _label_end(ax, c.freqs[-1], y, text, col, t, dy=(k - 1) * span * 0.12)
    for f0 in (f_lo, f_hi):
        ax.axvline(f0, color=t["axis"], linewidth=0.8)
    ax.set_title("Residual (±1 SD) · shaded: significant features" if speaker else
                 ("Residual with the checkpoint filters (±1 SD), added filters and predicted result"
                  if c.fixed else "Residual (±1 SD), correction and predicted result")
                 + " · shaded: corrected features", color=t["ink"], fontsize=10, loc="left")
    ax.set_ylabel("dB", color=t["muted"], fontsize=8)

    if with_sessions:
        ax = axes[2]
        _style(ax, t, g[0], g[-1])
        if len(session_R) > MAX_SESSION_LINES:
            w = c.session_trials if len(c.session_trials) == len(session_R) else np.ones(len(session_R))
            lo, hi = weighted_quantiles(np.array(session_R), w, (0.05, 0.95))
            ax.fill_between(g, lo, hi, color=t["muted"], alpha=0.3, linewidth=0)
            ax.plot(g, c.R, color=t["s1"], linewidth=2)
            _label_end(ax, xr, c.R[-1], "average", t["s1"], t, dy=1.0)
            _label_end(ax, xr, hi[-1], f"90% of {'positions' if speaker else 'sessions'}", t["muted"], t, dy=1.0)
            title = (f"Residual per position: middle 90% of {len(session_R)} blocks, weighted by trials" if speaker
                     else f"Residual per reseat session: middle 90% of {len(session_R)} sessions, weighted by trials")
        else:
            for r in session_R:
                ax.plot(g, r, color=t["muted"], linewidth=1, alpha=0.8)
            ax.plot(g, c.R, color=t["s1"], linewidth=2)
            _label_end(ax, xr, c.R[-1], "average", t["s1"], t, dy=1.0)
            _label_end(ax, xr, session_R[-1][-1], "each position" if speaker else "each session", t["muted"], t,
                       dy=-1.0)
            title = "Residual per position" if speaker else "Residual per reseat session"
        ax.set_title(title, color=t["ink"], fontsize=10, loc="left")
        ax.set_ylabel("dB", color=t["muted"], fontsize=8)
    axes[-1].set_xlabel("frequency (Hz)", color=t["muted"], fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=t["surface"], bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def reference_figure(c: EarCorrection, reference_name: str, theme: str = "light") -> bytes:
    """The reference as used, mean removed. For a consensus: its broad median,
    the broad median plus all of the shared narrow part, and the reference
    itself (±1 SD); then how much of the narrow part is kept, k, with what it
    came from."""
    t = THEMES[theme]
    x = c.consensus
    n = 1 if x is None else 2
    fig, axes = plt.subplots(n, 1, figsize=(8.2, 2.7 * n + 0.4), sharex=True, facecolor=t["surface"],
                             gridspec_kw={"hspace": 0.38}, squeeze=False)
    axes = axes[:, 0]
    name = EAR_NAMES.get(c.ear, c.ear)
    ax = axes[0]
    if x is None:
        g = c.grid
        _style(ax, t, g[0], g[-1])
        ax.plot(g, c.E, color=t["s1"], linewidth=2)
        ax.set_title(f"{name} ear · reference: {reference_name}", color=t["ink"], fontsize=10, loc="left")
    else:
        g = x.grid
        _style(ax, t, g[0], g[-1])
        shift = x.m.mean()
        sd = np.sqrt(np.clip(np.diag(x.cov), 0, None))
        full = x.broad + x.narrow - shift
        ax.plot(g, x.broad - shift, color=t["s2"], linewidth=1.6)
        ax.plot(g, full, color=t["s3"], linewidth=1.4, linestyle="--")
        ax.fill_between(g, x.m - shift - sd, x.m - shift + sd, color=t["s1"], alpha=0.18, linewidth=0)
        ax.plot(g, x.m - shift, color=t["s1"], linewidth=2.2)
        ends = sorted([(x.m[-1] - shift, "reference ±1 SD", t["s1"]), (x.broad[-1] - shift, "broad median", t["s2"]),
                       (full[-1], "broad + all narrow", t["s3"])])
        span = max(np.ptp(full), 4)
        for k, (y, text, col) in enumerate(ends):
            _label_end(ax, g[-1], y, text, col, t, dy=(k - 1) * span * 0.12)
        ax.set_title(f"{name} ear · consensus reference of {', '.join(x.members)}: broad + k · narrow",
                     color=t["ink"], fontsize=10, loc="left")

        ax = axes[1]
        _style(ax, t, g[0], g[-1])
        sh = x.share
        ends = [(sh.k[-1], "k", t["s1"]), (sh.k_hp[-1], "headphones only", t["s2"])]
        if sh.g is not None:
            lo, hi = np.clip(sh.g - sh.g_sd, -0.5, 1.5), np.clip(sh.g + sh.g_sd, -0.5, 1.5)
            ax.fill_between(g, lo, hi, color=t["s3"], alpha=0.15, linewidth=0)
            ax.plot(g, np.clip(sh.g, -0.5, 1.5), color=t["s3"], linewidth=1.4, linestyle="--")
            ends.append((float(np.clip(sh.g[-1], -0.5, 1.5)), "speaker gain ±1 SD", t["s3"]))
        ax.plot(g, sh.k_hp, color=t["s2"], linewidth=1.6)
        ax.plot(g, sh.k, color=t["s1"], linewidth=2.2)
        ax.axhline(1, color=t["axis"], linewidth=0.8)
        ax.set_ylim(-0.5, 1.5)
        for k, (y, text, col) in enumerate(sorted(ends)):
            _label_end(ax, g[-1], y, text, col, t, dy=(k - (len(ends) - 1) / 2) * 0.18)
        speakers = x.shares.speakers if x.shares is not None else []
        ax.set_title("Share of the narrow median kept, k" + (f" · speakers: {', '.join(speakers)} (gain clipped to "
                                                             "−0.5..1.5 for display)" if speakers else ""),
                     color=t["ink"], fontsize=10, loc="left")
        ax.set_ylabel("share", color=t["muted"], fontsize=8)
    axes[0].set_ylabel("dB", color=t["muted"], fontsize=8)
    axes[-1].set_xlabel("frequency (Hz)", color=t["muted"], fontsize=8)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=t["surface"], bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
