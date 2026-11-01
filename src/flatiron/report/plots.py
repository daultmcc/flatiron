"""Per-ear report figures (matplotlib → PNG), in a light and a dark variant.

Panels share a log-frequency axis:
  1. measured level relative to the reference (±1 SD) and the baseline
  2. residual (±1 SD), the correction, and the predicted result, with the
     corrected features shaded
  3. (two or more sessions) each session's residual against the pooled one
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
               f_lo: float = 1000, f_hi: float = 13000) -> bytes:
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
    ax.plot(g, c.baseline, color=t["s2"], linewidth=2)
    _label_end(ax, xr, D[-1], "measured − reference", t["s1"], t, dy=1.0 if D[-1] >= c.baseline[-1] else -1.0)
    _label_end(ax, xr, c.baseline[-1], "baseline", t["s2"], t, dy=-1.0 if D[-1] >= c.baseline[-1] else 1.0)
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
    ax.plot(c.freqs, H, color=t["s2"], linewidth=2)
    ax.plot(c.freqs, R_f + H, color=t["s3"], linewidth=2)
    ends = sorted([(R_f[-1], "residual", t["s1"]), (H[-1], "correction", t["s2"]),
                   ((R_f + H)[-1], "after correction", t["s3"])])
    span = max(np.ptp(np.concatenate([R_f, H])), 4)
    for k, (y, text, col) in enumerate(ends):
        _label_end(ax, c.freqs[-1], y, text, col, t, dy=(k - 1) * span * 0.12)
    for f0 in (f_lo, f_hi):
        ax.axvline(f0, color=t["axis"], linewidth=0.8)
    ax.set_title(("Residual with the checkpoint filters (±1 SD), added filters and predicted result"
                  if c.fixed else "Residual (±1 SD), correction and predicted result")
                 + " · shaded: corrected features", color=t["ink"], fontsize=10, loc="left")
    ax.set_ylabel("dB", color=t["muted"], fontsize=8)

    if with_sessions:
        ax = axes[2]
        _style(ax, t, g[0], g[-1])
        for r in session_R:
            ax.plot(g, r, color=t["muted"], linewidth=1, alpha=0.8)
        ax.plot(g, c.R, color=t["s1"], linewidth=2)
        _label_end(ax, xr, c.R[-1], "all sessions", t["s1"], t, dy=1.0)
        _label_end(ax, xr, session_R[-1][-1], "each session", t["muted"], t, dy=-1.0)
        ax.set_title("Residual per reseat session", color=t["ink"], fontsize=10, loc="left")
        ax.set_ylabel("dB", color=t["muted"], fontsize=8)
    axes[-1].set_xlabel("frequency (Hz)", color=t["muted"], fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=t["surface"], bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
