"""The comparison report (ADDENDUM.md, Feature 1a): several profiles against each
other, per ear. Colors follow the profile (the reference palette's categorical
slots in fixed order, the same as the single-profile figures); non-voting kinds
are also dashed, so identity is never color alone. Every line is direct-labelled
when there are few and always in a legend."""

import html
import io
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from flatiron.fit.consensus import EarComparison, compare_ear  # noqa: E402
from flatiron.fit.pipeline import CorrectionSettings, member_source  # noqa: E402
from flatiron.fit.reference import reference  # noqa: E402
from flatiron.model.grid import common_grid, interp_matrix  # noqa: E402
from flatiron.model.probit import ProbitFit  # noqa: E402
from flatiron.report.plots import EAR_NAMES, THEMES, _style  # noqa: E402

SERIES = {"light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
          "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]}
MAX_PROFILES = len(SERIES["light"])


@dataclass
class Member:
    label: str
    kind: str
    fits: dict[str, ProbitFit]


def compare(members: list[Member], settings: CorrectionSettings, ears, k: float, min_width_oct: float,
            voting_kinds) -> dict[str, EarComparison]:
    out = {}
    for e in ears:
        sources = [member_source(m.label, m.kind, m.fits[e], settings) for m in members]
        out[e] = compare_ear(e, sources, k, min_width_oct, voting_kinds)
    return out


def aligned_curves(members: list[Member], settings: CorrectionSettings, ear: str):
    """Each L minus the reference on the common grid, shifted by the
    least-squares offset that best aligns it with the others: its own mean."""
    common = common_grid([m.fits[ear].grid for m in members])
    out = []
    for m in members:
        f = m.fits[ear]
        D = f.L - reference(settings.reference, f.grid, settings.phon)
        up = interp_matrix(f.grid, common)
        Dc = up @ D
        out.append((Dc - Dc.mean(), np.sqrt(np.clip(np.einsum("ij,jk,ik->i", up, f.cov, up), 0, None))))
    return common, out


def ear_figure(c: EarComparison, members: list[Member], settings: CorrectionSettings, theme: str, k: float) -> bytes:
    t = THEMES[theme]
    colors = dict(zip([m.label for m in members], SERIES[theme]))
    common = c.consensus.grid
    fig, axes = plt.subplots(4, 1, figsize=(8.6, 10.2), sharex=True, facecolor=t["surface"],
                             gridspec_kw={"hspace": 0.32, "height_ratios": [3, 3, 0.35, 3]})
    x0, x1 = common[0], common[-1]
    for ax in axes:
        _style(ax, t, x0, x1)
        ax.set_ylabel("dB", color=t["muted"], fontsize=8)

    ax = axes[0]
    grid, curves = aligned_curves(members, settings, c.ear)
    dash = {e.source.label: (not e.voting) for e in c.entries}
    for m, (d, sd) in zip(members, curves):
        col = colors[m.label]
        ax.fill_between(grid, d - sd, d + sd, color=col, alpha=0.14, linewidth=0)
        ax.plot(grid, d, color=col, linewidth=2, linestyle="--" if dash[m.label] else "-", label=m.label)
    ax.set_title(f"{EAR_NAMES.get(c.ear, c.ear)} ear · each curve minus the reference, aligned (±1 SD)",
                 color=t["ink"], fontsize=10, loc="left")
    ax.legend(loc="upper right", fontsize=8, frameon=False, labelcolor=t["ink2"], ncol=min(len(members), 4))

    ax = axes[1]
    for e in c.entries:
        s = e.source
        ax.plot(s.grid, s.R, color=colors[s.label], linewidth=1.5, linestyle="--" if not e.voting else "-", alpha=0.85)
    ax.fill_between(common, c.consensus.m - c.consensus.sd, c.consensus.m + c.consensus.sd,
                    color=t["ink"], alpha=0.18, linewidth=0)
    ax.plot(common, c.consensus.m, color=t["ink"], linewidth=2.2,
            label="precision-weighted mean" if c.voters == 2 else "soft median")
    ax.set_title("Residuals, narrower than W · black: their "
                 + ("precision-weighted mean" if c.voters == 2 else "soft median") + " (±1 SD)",
                 color=t["ink"], fontsize=10, loc="left")

    ax = axes[2]
    ax.fill_between(common, 0, 1, where=c.heterogeneous, color=t["muted"], linewidth=0, step="mid")
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_ylabel("")
    ax.grid(False)
    ax.set_title("Where the profiles as a whole disagree (Cochran's Q, p < 0.01)", color=t["ink2"], fontsize=8, loc="left")

    ax = axes[3]
    for e in c.entries:
        s = e.source
        ax.plot(s.grid, e.z, color=colors[s.label], linewidth=1.5, linestyle="--" if not e.voting else "-")
        for f in e.flags:
            ax.axvspan(f.f_lo, f.f_hi, color=colors[s.label], alpha=0.16, linewidth=0, zorder=0)
    for y in (-k, k):
        ax.axhline(y, color=t["muted"], linewidth=0.8, linestyle=":")
    ax.set_ylabel("z", color=t["muted"], fontsize=8)
    ax.set_title("Each profile against the consensus, in SDs · shaded: flagged", color=t["ink"],
                 fontsize=10, loc="left")
    axes[-1].set_xlabel("frequency (Hz)", color=t["muted"], fontsize=8)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=t["surface"], bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def flag_rows(c: EarComparison) -> list[tuple]:
    return [(f.label, EAR_NAMES.get(c.ear, c.ear), f"{f.f_lo:,.0f}–{f.f_hi:,.0f} Hz",
             f"{f.deviation_db:+.1f} ± {f.sd_db:.1f}", f.sign if c.voters > 2 else "differs from the other")
            for f in c.flags]


def print_comparison(comparisons: dict[str, EarComparison], members: list[Member]) -> None:
    for e, c in comparisons.items():
        word = "precision-weighted mean" if c.voters == 2 else "soft median"
        print(f"\n{EAR_NAMES.get(e, e)} ear: {word} of {c.voters} profiles, between-headphone spread "
              f"{c.consensus.tau_hp:.2f} dB")
        if c.voters == 2:
            print("  Only two profiles: these are differences, not outliers.")
        rows = flag_rows(c)
        if not rows:
            print("  No region stands out.")
        for label, _, span, dev, sign in rows:
            print(f"  {label:<20} {span:<20} {dev:>12} dB  {sign}")


def write_comparison(path: Path, comparisons: dict[str, EarComparison], members: list[Member],
                     settings: CorrectionSettings, k: float) -> Path:
    from flatiron.report.html import CSS, _png, _table

    names = ", ".join(m.label for m in members)
    out = [f"<h1>Flatiron comparison · {html.escape(names)}</h1>",
           f'<p class="meta">Max feature width {settings.max_width_oct:g} octave · reference '
           f"{html.escape(settings.reference)}</p>"]
    for e, c in comparisons.items():
        out.append(f"<h2>{EAR_NAMES.get(e, e)} ear</h2>")
        if c.voters == 2:
            out.append('<p class="note">With two profiles the consensus is their precision-weighted mean, and the '
                       "table shows differences, not outliers.</p>")
        light = _png(ear_figure(c, members, settings, "light", k))
        dark = _png(ear_figure(c, members, settings, "dark", k))
        out.append(f'<div class="card"><picture><source srcset="{dark}" media="(prefers-color-scheme: dark)">'
                   f'<img src="{light}" alt="{EAR_NAMES.get(e, e)} ear: curves, residuals and disagreement">'
                   "</picture></div>")
        out.append(f'<p class="meta">Between-headphone spread {c.consensus.tau_hp:.2f} dB.</p>')
        rows = flag_rows(c)
        out.append(_table(["profile", "ear", "range", "peak deviation (dB)", "reading"], rows)
                   if rows else '<p class="muted">No region stands out.</p>')
    doc = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width, initial-scale=1">'
           f"<title>Flatiron comparison · {html.escape(names)}</title><style>{CSS}</style></head>"
           f"<body><main>{''.join(out)}</main></body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc)
    return path


def write_csv(path, pf) -> None:
    """L, the reference, the baseline and the residual with their SDs, per ear."""
    lines = ["ear,freq_hz,level_db,level_sd,reference_db,baseline_db,residual_db,residual_sd"]
    for e, c in pf.correction.ears.items():
        for row in zip(c.grid, c.L, c.L_sd, c.E, c.baseline, c.R, c.R_sd):
            lines.append(e + "," + ",".join(f"{v:.4f}" for v in row))
    Path(path).write_text("\n".join(lines) + "\n")
