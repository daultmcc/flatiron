"""Static HTML report for a profile fit (figures embedded, no external files)."""

import base64
import html
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np

from flatiron.fit.features import BROAD, NARROW
from flatiron.fit.run import ProfileFit
from flatiron.report.plots import EAR_NAMES, ear_figure, reference_figure
from flatiron.session.store import Profile

CSS = """
:root { color-scheme: light dark; --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink2: #52514e;
        --muted: #898781; --hair: #e1e0d9; --warn-bg: #fff4e0; }
@media (prefers-color-scheme: dark) {
  :root { --surface: #1a1a19; --page: #0d0d0d; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
          --hair: #2c2c2a; --warn-bg: #2e2412; }
}
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--page); color: var(--ink);
       margin: 0; padding: 2rem; line-height: 1.45; }
main { max-width: 124rem; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin-top: 2.5rem; padding-bottom: .3rem; border-bottom: 1px solid var(--hair); }
h3 { font-size: 1rem; margin: 1.5rem 0 .5rem; }
h4 { font-size: .95rem; margin: 1.25rem 0 .5rem; color: var(--ink2); }
.cols { display: grid; gap: 0 2rem; align-items: start; }
.cols > div { min-width: 0; }
.wide { overflow-x: auto; }
@media (max-width: 70rem) { .cols { grid-template-columns: 1fr !important; } }
.meta { color: var(--ink2); font-size: .9rem; }
.card { background: var(--surface); border: 1px solid var(--hair); border-radius: 8px; padding: 1rem 1.25rem; }
img { max-width: 100%; height: auto; display: block; }
table { border-collapse: collapse; font-size: .88rem; margin: .25rem 0 1rem; }
th, td { text-align: left; padding: .3rem .9rem .3rem 0; border-bottom: 1px solid var(--hair); }
th { color: var(--muted); font-weight: 600; }
td.num { font-variant-numeric: tabular-nums; text-align: right; padding-right: 1.4rem; }
.note { background: var(--warn-bg); border-radius: 6px; padding: .6rem .9rem; margin: .5rem 0; font-size: .9rem; }
.muted { color: var(--muted); }
"""


def _png(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _table(head, rows, num_cols=()):
    h = "".join(f"<th>{html.escape(c)}</th>" for c in head)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            f'<td class="num">{html.escape(str(v))}</td>' if i in num_cols else f"<td>{html.escape(str(v))}</td>"
            for i, v in enumerate(r)) + "</tr>"
    return f'<div class="wide"><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def _columns(cols: list[str]) -> str:
    """Side by side, one column each (the ears), stacked on a narrow window."""
    return (f'<div class="cols" style="grid-template-columns: repeat({len(cols)}, minmax(0, 1fr))">'
            + "".join(f"<div>{c}</div>" for c in cols) + "</div>")


def _picture(data_light: bytes, data_dark: bytes, alt: str) -> str:
    return (f'<div class="card"><picture><source srcset="{_png(data_dark)}" media="(prefers-color-scheme: dark)">'
            f'<img src="{_png(data_light)}" alt="{html.escape(alt)}"></picture></div>')


def veto_line(f) -> str:
    """What a feature's veto saw, in a line."""
    v = f.veto
    seen = ", ".join(f"{c.label} {c.height_db:+.1f} dB at {c.fc:,.0f} Hz" if c.present else f"{c.label} nothing"
                     for c in v.candidates)
    if v.weighed:
        mix = ", ".join(f"{c.label if c.present else 'nothing'} {w:.0%}" for c, w in zip(v.weighed, v.weights) if w >= 0.005)
        return f"{seen}; weighed: {mix}"
    if v.median is None:
        return f"{seen}; median: nothing there"
    return f"{seen}; median: {v.median.height_db:+.1f} dB at {v.median.fc:,.0f} Hz" + (" (elsewhere)" if v.moved else "")


def _features_table(feats, veto=False, speaker=False):
    rows = [(f"{f.fc:,.0f} Hz", f"{f.width_oct:.2f}", f"{f.peak_db:+.1f}", f"{f.z:.1f}",
             "" if f.level_sd is None else f"{f.level_sd:.2f}",
             "" if f.seat_spread is None else f"{f.seat_spread:.2f}",
             *(("" if f.veto is None else veto_line(f),
                "" if f.veto is None else f"{f.veto.correction_db:+.1f}",) if veto else ()),
             ("yes" + (", inside a broad region" if f.inside_broad else "")
              + (f", {f.weight:.0%} by significance" if f.weight < 1 else "")
              + (f"; {f.reason}" if f.reason else "")) if f.accepted else f"no: {f.reason}")
            for f in sorted(feats, key=lambda f: f.fc)]
    head = ["centre", "width (oct)", "residual dB", "z", "level SD", "position spread" if speaker else "seating spread"]
    numeric = tuple(range(1, len(head)))
    if veto:
        head += ["veto", "corrected dB"]
    return _table(head + ["significant" if speaker else "corrected"], rows, numeric)


KEPT_AT_HZ = (2000.0, 4000.0, 7000.0, 11000.0)
VETO_WITH_CONSENSUS = ("Warning: the veto is on as well as a consensus reference. The consensus already keeps what the "
                       "headphones share, so the veto mostly makes the correction more conservative; it's meant for "
                       "when a consensus reference isn't available or trusted.")


def consensus_notes(pf: ProfileFit) -> list[str]:
    """For the terminal: the reference's notes, then the method's."""
    return reference_notes(pf) + method_notes(pf)


def reference_description(st) -> str | None:
    """A fixed reference in a sentence (a consensus is described by reference_notes)."""
    if st.consensus:
        return None
    if st.reference == "diffuse":
        return (f"Reference: the ISO 226 frontal free-field equal-loudness contour at {st.phon:g} phon, plus the "
                "ISO 389-7 diffuse-field difference (the default).")
    if st.reference == "iso226":
        return (f"Reference: the ISO 226 frontal free-field equal-loudness contour at {st.phon:g} phon. The "
                "default, diffuse field (ISO 389-7), differs most at 2 to 4 and 6 to 12 kHz.")
    if st.reference == "none":
        return "Reference: none. The measured curve is taken as it is, ear gain and all."
    return f"Reference: the curve in {st.reference}."


def reference_notes(pf: ProfileFit) -> list[str]:
    st = pf.correction.settings
    out = []
    if st.consensus:
        ears = pf.correction.ears
        c = next(iter(ears.values())).consensus
        out.append(f"Reference: the consensus of {', '.join(c.members)}: the soft median of their broad parts, plus "
                   "the share of their narrow parts' soft median believed to be the listener's.")
        kept = []
        for e, ec in ears.items():
            x = ec.consensus
            at = [(f, float(np.interp(np.log2(f), np.log2(x.grid), x.share.k))) for f in KEPT_AT_HZ
                  if x.grid[0] <= f <= x.grid[-1]]
            kept.append(f"{e} " + ", ".join(f"{k:.2f} at {f / 1000:g} kHz" for f, k in at))
        out.append("Narrow structure kept: " + "; ".join(kept) + ".")
        sh = c.shares
        if sh is not None and sh.speakers:
            out.append(f"Speakers ({', '.join(sh.speakers)}) decide how much is kept, with their own colouration "
                       f"taken as {sh.colour_db:.2f} dB ({sh.colour_how}) and the headphones-only share's SD as "
                       f"{sh.gain_sd:.2f} ({sh.gain_sd_how}).")
            if len(sh.speakers) == 1:
                out.append("With one speaker, a colouration of its own that happens to match a headphone feature "
                           "can't be told from the listener's.")
        if len(c.members) == 1:
            out.append("That's a single headphone's curve, not a consensus.")
        elif len(c.members) == 2:
            out.append("With two headphones it's their weighted mean, not a median, so one's peak reaches the "
                       "reference at about half height.")
    return out


def method_notes(pf: ProfileFit) -> list[str]:
    st = pf.correction.settings
    out = []
    if st.consensus and st.veto:
        out.append(VETO_WITH_CONSENSUS)
    if st.veto:
        who = ", ".join(st.veto)
        if st.veto_mode == "pointwise":
            out.append(f"Veto: frequency by frequency, only what exceeds the median of this profile and {who} is "
                       "corrected, and never past zero.")
        else:
            out.append(f"Veto: each feature is corrected toward the median feature of this profile and {who}, "
                       f"adding at most {st.veto_import_db:g} dB where that feature is somewhere else.")
        if len(st.veto) == 1:
            out.append("With one other profile there's no majority: the median is their average.")
    if st.method == "match":
        out.append("Experimental match method: the whole curve is corrected toward the reference, tonality "
                   "included, with no significance test. The features listed are only what's significant.")
    return out


# A corrected feature under a joint baseline this uncertain (posterior SD, dB)
# is flagged: the data can't settle how much of it is tonality.
AMBIGUOUS_BASELINE_SD_DB = 1.0


def ambiguous_features(c) -> list:
    """Corrected features where the joint baseline's SD exceeds the threshold."""
    if c.baseline_sd is None:
        return []
    return [x for x in c.features
            if x.accepted and np.max(c.baseline_sd[x.lo:x.hi + 1]) > AMBIGUOUS_BASELINE_SD_DB]


def write_report(profile: Profile, pf: ProfileFit, path: Path | None = None) -> Path:
    corr = pf.correction
    st = corr.settings
    label = profile.read()["label"]
    now = datetime.now()
    path = path or profile.dir / "cache" / "report.html"  # overwritten each time
    path.parent.mkdir(parents=True, exist_ok=True)
    speaker = profile.kind == "speaker"
    ears = list(corr.ears.items())

    notes = []
    if speaker:
        notes.append("Speaker profile: the curve is averaged over the positions measured, and it's evidence "
                     "about your ears, so there's no correction. The position spread is assumed, not estimated.")
    if pf.pooled:
        notes.append(f"Pooled: includes the trials of {', '.join(pf.pooled)}.")
    notes += method_notes(pf)
    if st.method == "features" and st.baseline == "joint":
        notes.append("Experimental joint baseline: the baseline is the posterior mean of the tonality under a "
                     "model of tonality plus features, drawn with its SD. Where that band is wide, the data "
                     "can't tell tonality from feature, and the baseline sits between the two readings.")
    if st.method == "smooth":
        notes.append(f"Experimental smooth method: the baseline is the curve smoothed over {st.smooth_oct:g} "
                     "octave, and every residual is corrected. The features listed are only what's "
                     "significant; noise gets corrected too.")
    out = [f"<h1>Flatiron fit · {html.escape(label)}</h1>",
           f'<p class="meta">{now:%Y-%m-%d %H:%M} · {len(pf.sessions)} {"blocks" if speaker else "sessions"} · '
           + " · ".join(f"{EAR_NAMES[e]} {f.n_trials} trials" for e, f in pf.fits.items())
           + ("" if speaker else f" · preamp {corr.preamp_db:+.1f} dB (both channels)") + "</p>"]
    out += [f'<p class="note">{html.escape(n)}</p>' for n in notes]

    out.append("<h2>Measured response</h2>")
    cols = []
    for e, c in ears:
        f = pf.fits[e]
        cols.append(
            f"<h3>{EAR_NAMES[e]} ear</h3>"
            + _picture(ear_figure(c, c.session_R, "light", st.f_lo, st.f_hi, speaker),
                       ear_figure(c, c.session_R, "dark", st.f_lo, st.f_hi, speaker),
                       f"{EAR_NAMES[e]} ear: measured level, residual and correction")
            + f'<p class="meta">Listener noise σ₀ {f.sigma0:.2f} dB (×{f.treble_noise:.2f} above 10 kHz), lapse '
            f"rate {f.lapse:.1%} "
            + (f"(shared: {f.noise_trials} trials in this ear across profiles), " if f.noise_trials
               else "(the prior: estimating it needs a profile with 200 trials in this ear), ")
            + f"time-order bias {f.bias_db:+.2f} ± {f.bias_sd:.2f} dB (the first interval sounds "
            f"{'louder' if f.bias_db >= 0 else 'quieter'}; fitted, so it doesn't distort the curve). "
            + (f"Position spread {f.tau_lo:.1f}, {f.tau_mid:.1f} and {f.tau_hi:.1f} dB at 5, 8 and 12 kHz "
               f"(assumed), {f.n_groups} positions." if speaker else
               f"Seating spread {f.tau_lo:.1f}, {f.tau_mid:.1f} and {f.tau_hi:.1f} dB at 5, 8 and 12 kHz "
               + (f"(estimated from {f.n_groups} sessions)." if f.tau_fitted
                  else "(the prior: estimating it needs two sessions with 60+ trials for this ear)."))
            + "</p>")
    out.append(_columns(cols))

    out.append("<h2>Features</h2>")
    out.append(f'<p class="muted">Level SD says whether a feature is real; {"position" if speaker else "seating"} '
               "spread says how far off the correction will be on a typical "
               f"{'position' if speaker else 'seating'}.</p>")
    cols = []
    for e, c in ears:
        col = [f"<h3>{EAR_NAMES[e]} ear</h3>"]
        if not speaker:
            col.append("<h4>Filters</h4>")
            if c.filters:
                comp = {id(x) for x in c.compensation}
                kept = {id(x) for x in c.fixed}
                rows = [(f"{flt.fc:,.0f}", f"{flt.gain_db:+.1f}", f"{flt.q:.2f}",
                         "checkpoint" if id(flt) in kept else
                         "tonality compensation" if id(flt) in comp else f"{ctb:.2f}")
                        for flt, ctb in zip(c.filters, c.fit.contributions)]
                col.append(_table(["Fc (Hz)", "gain (dB)", "Q", "χ² if removed"], rows, (0, 1, 2)))
            else:
                col.append('<p class="muted">No correction needed: nothing significant left after the reference '
                           "and baseline.</p>")
            col.append(f'<p class="meta">Filter fit: χ² per point {c.fit.chi2:.2f}'
                       + (f"; {c.sessions_checked} sessions in the seating step" if c.sessions_checked else "")
                       + ".</p>")
            if c.left_out:
                spans = ", ".join(f"{a:,.0f}–{b:,.0f} Hz (up to {m:.1f} dB)" for a, b, m in c.left_out)
                col.append(f'<p class="note">The band budget ({st.bands}) ran out. Left uncorrected: '
                           f"{html.escape(spans)}.</p>")
            unsure = ambiguous_features(c)
            if unsure:
                where = ", ".join(f"{x.fc:,.0f} Hz" for x in unsure)
                col.append(f'<p class="note">The baseline is uncertain (SD over {AMBIGUOUS_BASELINE_SD_DB:g} dB) '
                           f"under the feature(s) at {html.escape(where)}: part of each may be tonality rather than "
                           "a feature, or the other way round.</p>")
        broad_title = "Broad features" if speaker else "Broad features (ear-gain region: depends on the reference)"
        for kind, title in ((NARROW, "Narrow features"), (BROAD, broad_title)):
            feats = [x for x in c.features if x.kind == kind and (x.accepted or abs(x.z) >= 1.5)]
            col.append(f"<h4>{title}</h4>")
            col.append(_features_table(feats, not speaker and bool(st.veto) and st.veto_mode == "feature",
                                       speaker) if feats else '<p class="muted">None.</p>')
        cols.append("".join(col))
    out.append(_columns(cols))

    out.append("<h2>Reference</h2>")
    desc = reference_description(st)
    out += [f'<p class="meta">{html.escape(n)}</p>' for n in ([desc] if desc else []) + reference_notes(pf)]
    ref = "consensus" if st.consensus else Path(st.reference).name
    out.append(_columns([f"<h3>{EAR_NAMES[e]} ear</h3>"
                         + _picture(reference_figure(c, ref, "light"), reference_figure(c, ref, "dark"),
                                    f"{EAR_NAMES[e]} ear: the reference") for e, c in ears]))

    out.append("<h2>Settings</h2>")
    out.append(_table(["setting", "value"], [(k, v) for k, v in asdict(st).items()]))
    doc = (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
           f"<title>Flatiron fit · {html.escape(label)}</title><style>{CSS}</style></head>"
           f"<body><main>{''.join(out)}</main></body></html>")
    path.write_text(doc)
    return path
