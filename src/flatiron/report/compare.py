"""The comparison report (DESIGN.md, "Comparing profiles"): several profiles against each
other, per ear. Colors follow the profile (the reference palette's categorical
slots in fixed order, the same as the single-profile figures); kinds that don't
count (IEMs and speakers, by default) are also dashed, so identity is never color alone. Every line is in the legend."""

import html
import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from flatiron.fit.compare import EarComparison, Member, compare_ear, spans  # noqa: E402
from flatiron.fit.compare import HETEROGENEITY_P  # noqa: E402
from flatiron.model.grid import interp_matrix  # noqa: E402
from flatiron.report.plots import EAR_NAMES, THEMES, _style  # noqa: E402

SERIES = {"light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
          "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]}
MAX_PROFILES = len(SERIES["light"])


REF_STYLES = [(0, (1, 2)), (0, (5, 2, 1, 2)), (0, (6, 3)), (0, (1, 1, 1, 4))]


def compare(members: list[Member], ears, counted_kinds, broad_spread_db: float | None = None,
            references=None) -> dict[str, EarComparison]:
    return {e: compare_ear(e, members, counted_kinds, broad_spread_db, references) for e in ears}


def ear_figure(c: EarComparison, members: list[Member], theme: str) -> bytes:
    """The raw curves, with no reference subtracted (as with -r none): the profiles are
    compared to each other, not to a reference, which is only drawn beside them (`-r`).
    Then their narrow parts, overlaid, and where the broad and the narrow parts differ."""
    t = THEMES[theme]
    colors = dict(zip([m.label for m in members], SERIES[theme]))
    common = c.grid
    fig, axes = plt.subplots(4, 1, figsize=(8.6, 9.0), sharex=True, facecolor=t["surface"],
                             gridspec_kw={"hspace": 0.32, "height_ratios": [8, 6, 0.5, 0.5]})
    for ax in axes:
        _style(ax, t, common[0], common[-1])

    ax = axes[0]
    ax.set_ylabel("dB", color=t["muted"], fontsize=8)
    curves = {}
    for mem in members:
        f = mem.fits[c.ear]
        curves[mem.label] = interp_matrix(f.grid, common) @ f.L - c.offsets[mem.label]
    shift = np.mean([curves[m.label] for m in members if c.counted[m.label]])
    for mem in members:
        f = mem.fits[c.ear]
        d = curves[mem.label] - shift
        sd = interp_matrix(f.grid, common) @ f.sd
        col = colors[mem.label]
        ax.fill_between(common, d - sd, d + sd, color=col, alpha=0.14, linewidth=0)
        ax.plot(common, d, color=col, linewidth=1.6, linestyle="-" if c.counted[mem.label] else "--", label=mem.label)
    for i, (label, ref) in enumerate(c.references.items()):
        ax.plot(common, ref - shift, color=t["ink2"], linewidth=1.4, linestyle=REF_STYLES[i % len(REF_STYLES)],
                label=label)
    ax.set_title("Measured response", color=t["ink"], fontsize=10, loc="left")
    ax.fill_between([], [], [], color=t["muted"], alpha=0.25, linewidth=0, label="±1 SD")
    ax.legend(loc="upper right", fontsize=8, frameon=False, labelcolor=t["ink2"],
              ncol=min(len(members) + len(c.references) + 1, 5))

    ax = axes[1]
    ax.set_ylabel("dB", color=t["muted"], fontsize=8)
    for mem in members:
        n, sd = c.narrow[mem.label]
        col = colors[mem.label]
        ax.fill_between(common, n - sd, n + sd, color=col, alpha=0.12, linewidth=0)
        ax.plot(common, n, color=col, linewidth=1.6, linestyle="-" if c.counted[mem.label] else "--")
    ax.set_title("Narrow parts", color=t["ink"], fontsize=10, loc="left")

    for ax, where, title in ((axes[2], c.differ_broad, f"Broad tonal disagreements (> {c.broad_spread_db:g} dB)"),
                             (axes[3], c.differ_narrow, f"Narrow feature disagreements (p < {HETEROGENEITY_P:g})")):
        ax.fill_between(common, 0, 1, where=where, color=t["muted"], linewidth=0, step="mid")
        ax.set_ylim(0, 1)
        ax.set_yticks([])
        ax.grid(False)
        ax.set_title(title, color=t["ink2"], fontsize=8, loc="left")
    axes[-1].set_xlabel("frequency (Hz)", color=t["muted"], fontsize=8)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=t["surface"], bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _hz(f: float) -> str:
    return f"{round(f / 1000, 1):g}"


def _ranges(c: EarComparison, mask) -> str:
    """Where mask holds, as kHz ranges."""
    out = [f"{_hz(a)}–{_hz(b)} kHz" if _hz(b) != _hz(a) else f"{_hz(a)} kHz" for a, b in spans(c.grid, mask)]
    return ", ".join(out) or "nowhere"


def _spread(c: EarComparison) -> str:
    return f"Spread between profiles: {c.tau_hp:.2f} dB"


def _uncounted(c: EarComparison, members: list[Member]) -> list[Member]:
    return [m for m in members if not c.counted[m.label]]


def print_comparison(comparisons: dict[str, EarComparison], members: list[Member]) -> None:
    c = next(iter(comparisons.values()))
    print("Comparing " + ", ".join(m.label + ("" if c.counted[m.label] else f" ({m.kind}, not counted)")
                                   for m in members))
    if c.fallback:
        print("Fewer than two headphones, so every profile counts.")
    if c.references:
        print("Drawn beside them: " + ", ".join(c.references))
    for e, c in comparisons.items():
        print(f"\n{EAR_NAMES.get(e, e)} ear\n  {_spread(c)}\n"
              f"  Broad tonal disagreements (> {c.broad_spread_db:g} dB): {_ranges(c, c.differ_broad)}\n"
              f"  Narrow feature disagreements: {_ranges(c, c.differ_narrow)}")


def write_comparison(path: Path, comparisons: dict[str, EarComparison], members: list[Member]) -> Path:
    from flatiron.report.html import CSS, _columns, _png

    names = ", ".join(m.label for m in members)
    first = next(iter(comparisons.values()))
    kinds = sorted({m.kind for m in _uncounted(first, members)})
    out = [f"<h1>Flatiron comparison · {html.escape(names)}</h1>"]
    if kinds:
        out.append(f'<p class="meta">Dashed lines are {" and ".join(k.replace("iem", "IEM") for k in kinds)} '
                   "profiles and aren't counted in spread calculations.</p>")
    if first.fallback:
        out.append('<p class="meta">Fewer than two headphones, so every profile counts.</p>')
    if first.references:
        out.append(f'<p class="meta">Dotted: {html.escape(", ".join(first.references))}, aligned with the '
                   "profiles' median; the curves aren't measured against it.</p>")
    cols = []
    for e, c in comparisons.items():
        light = _png(ear_figure(c, members, "light"))
        dark = _png(ear_figure(c, members, "dark"))
        cols.append(f"<h2>{EAR_NAMES.get(e, e)} ear</h2>"
                    f'<div class="card"><picture><source srcset="{dark}" media="(prefers-color-scheme: dark)">'
                    f'<img src="{light}" alt="{EAR_NAMES.get(e, e)} ear: aligned curves, narrow parts and where they '
                    'differ"></picture></div>'
                    f'<p class="meta">{_spread(c)}</p>')
    out.append(_columns(cols))
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
