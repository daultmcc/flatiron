"""The comparison report (ADDENDUM.md, Feature 1a): several profiles against each
other, per ear. Colors follow the profile (the reference palette's categorical
slots in fixed order, the same as the single-profile figures); non-voting kinds
are also dashed, so identity is never color alone. Every line is in the legend."""

import html
import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from flatiron.fit.compare import EarComparison, Member, compare_ear  # noqa: E402
from flatiron.fit.pipeline import CorrectionSettings  # noqa: E402
from flatiron.fit.reference import reference  # noqa: E402
from flatiron.model.grid import interp_matrix  # noqa: E402
from flatiron.report.plots import EAR_NAMES, THEMES, _style  # noqa: E402

SERIES = {"light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
          "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]}
MAX_PROFILES = len(SERIES["light"])


def compare(members: list[Member], settings: CorrectionSettings, ears, voting_kinds) -> dict[str, EarComparison]:
    return {e: compare_ear(e, members, settings, voting_kinds) for e in ears}


def ear_figure(c: EarComparison, members: list[Member], settings: CorrectionSettings, theme: str) -> bytes:
    t = THEMES[theme]
    colors = dict(zip([m.label for m in members], SERIES[theme]))
    common = c.consensus.grid
    fig, axes = plt.subplots(2, 1, figsize=(8.6, 5.6), sharex=True, facecolor=t["surface"],
                             gridspec_kw={"hspace": 0.28, "height_ratios": [8, 0.5]})
    for ax in axes:
        _style(ax, t, common[0], common[-1])

    ax = axes[0]
    ax.set_ylabel("dB", color=t["muted"], fontsize=8)
    E = reference(settings.reference, common, settings.phon)
    m = c.consensus.m - E
    shift = m.mean()
    for mem in members:
        f = mem.fits[c.ear]
        d = interp_matrix(f.grid, common) @ f.L - c.offsets[mem.label] - E - shift
        sd = interp_matrix(f.grid, common) @ f.sd
        col = colors[mem.label]
        ax.fill_between(common, d - sd, d + sd, color=col, alpha=0.14, linewidth=0)
        ax.plot(common, d, color=col, linewidth=1.6, linestyle="-" if c.voting[mem.label] else "--", label=mem.label)
    word = "mean" if c.voters == 2 else "soft median"
    ax.fill_between(common, m - shift - c.consensus.sd, m - shift + c.consensus.sd, color=t["ink"], alpha=0.18, linewidth=0)
    ax.plot(common, m - shift, color=t["ink"], linewidth=2.2, label=word)
    ax.set_title(f"{EAR_NAMES.get(c.ear, c.ear)} ear · each curve minus the reference, aligned (±1 SD) · black: their "
                 f"{word}", color=t["ink"], fontsize=10, loc="left")
    ax.legend(loc="upper right", fontsize=8, frameon=False, labelcolor=t["ink2"], ncol=min(len(members) + 1, 5))

    ax = axes[1]
    ax.fill_between(common, 0, 1, where=c.differ, color=t["muted"], linewidth=0, step="mid")
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.grid(False)
    ax.set_title("Where the curves differ, broad differences included (Cochran's Q, p < 0.01)", color=t["ink2"],
                 fontsize=8, loc="left")
    ax.set_xlabel("frequency (Hz)", color=t["muted"], fontsize=8)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=t["surface"], bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def flag_rows(c: EarComparison) -> list[tuple]:
    return [(s.label, EAR_NAMES.get(c.ear, c.ear), f"{s.fc:,.0f} Hz", f"{s.height_db:+.1f}", f"{s.correction_db:+.1f}",
             s.reading if c.voters > 2 else "differs from the other") for s in c.flags]


def feature_rows(c: EarComparison, members: list[Member]) -> list[tuple]:
    """One row per group of overlapping features: what each profile has there,
    and what the veto would correct."""
    rows = []
    for g in c.groups:
        by = {s.label: s for s in g}
        lo, hi = min(s.fc for s in g), max(s.fc for s in g)
        rows.append((f"{lo:,.0f}–{hi:,.0f} Hz" if hi > lo else f"{lo:,.0f} Hz",
                     *(f"{by[m.label].height_db:+.1f}" if m.label in by else "–" for m in members),
                     "; ".join(f"{s.label} {s.correction_db:+.1f}" for s in g if s.flagged) or "nothing"))
    return rows


def print_comparison(comparisons: dict[str, EarComparison], members: list[Member]) -> None:
    for e, c in comparisons.items():
        word = "their mean" if c.voters == 2 else "soft median"
        print(f"\n{EAR_NAMES.get(e, e)} ear: {word} of {c.voters} profiles, between-headphone spread "
              f"{c.consensus.tau_hp:.2f} dB")
        if c.voters == 2:
            print("  Only two profiles: these are differences, not outliers.")
        rows = flag_rows(c)
        if not rows:
            print("  No feature stands out.")
        for label, _, at, height, corr, reading in rows:
            print(f"  {label:<20} {at:<12} {height:>6} dB, the veto corrects {corr:>6} dB  {reading}")


def write_comparison(path: Path, comparisons: dict[str, EarComparison], members: list[Member],
                     settings: CorrectionSettings) -> Path:
    from flatiron.report.html import CSS, _png, _table

    names = ", ".join(m.label for m in members)
    out = [f"<h1>Flatiron comparison · {html.escape(names)}</h1>",
           f'<p class="meta">Max feature width {settings.max_width_oct:g} octave · reference '
           f"{html.escape(settings.reference)} · features judged by the veto, with an import limit of "
           f"{settings.veto_import_db:g} dB</p>"]
    for e, c in comparisons.items():
        out.append(f"<h2>{EAR_NAMES.get(e, e)} ear</h2>")
        if c.voters == 2:
            out.append('<p class="note">With two profiles the median is their average, and the tables show '
                       "differences, not outliers.</p>")
        light = _png(ear_figure(c, members, settings, "light"))
        dark = _png(ear_figure(c, members, settings, "dark"))
        out.append(f'<div class="card"><picture><source srcset="{dark}" media="(prefers-color-scheme: dark)">'
                   f'<img src="{light}" alt="{EAR_NAMES.get(e, e)} ear: aligned curves and where they differ">'
                   "</picture></div>")
        out.append(f'<p class="meta">Between-headphone spread {c.consensus.tau_hp:.2f} dB.</p>')
        out.append("<h3>Flagged features</h3>")
        rows = flag_rows(c)
        out.append(_table(["profile", "ear", "centre", "height (dB)", "corrected (dB)", "reading"], rows)
                   if rows else '<p class="muted">No feature stands out.</p>')
        out.append("<h3>All features</h3>")
        rows = feature_rows(c, members)
        out.append(_table(["range", *[m.label for m in members], "the veto corrects (dB)"], rows)
                   if rows else '<p class="muted">None.</p>')
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
