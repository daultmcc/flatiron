"""Static HTML report for a profile fit (figures embedded, no external files)."""

import base64
import html
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flatiron.fit.features import BROAD, NARROW
from flatiron.fit.run import ProfileFit
from flatiron.report.plots import EAR_NAMES, ear_figure
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
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; } h2 { font-size: 1.15rem; margin-top: 2.5rem; }
h3 { font-size: 1rem; margin: 1.5rem 0 .5rem; color: var(--ink2); }
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
    return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"


def veto_line(f) -> str:
    """What a feature's veto saw, in a line."""
    v = f.veto
    seen = ", ".join(f"{c.label} {c.height_db:+.1f} dB at {c.fc:,.0f} Hz" if c.present else f"{c.label} nothing"
                     for c in v.candidates)
    if v.median is None:
        return f"{seen}; median: nothing there"
    return f"{seen}; median: {v.median.height_db:+.1f} dB at {v.median.fc:,.0f} Hz" + (" (elsewhere)" if v.moved else "")


def _features_table(feats, veto=False):
    rows = [(f"{f.fc:,.0f} Hz", f"{f.width_oct:.2f}", f"{f.peak_db:+.1f}", f"{f.z:.1f}",
             "" if f.level_sd is None else f"{f.level_sd:.2f}",
             "" if f.seat_spread is None else f"{f.seat_spread:.2f}",
             *(("" if f.veto is None else veto_line(f),
                "" if f.veto is None else f"{f.veto.correction_db:+.1f}",) if veto else ()),
             ("yes" + (", inside a broad region" if f.inside_broad else "")
              + (f"; {f.reason}" if f.reason else "")) if f.accepted else f"no: {f.reason}")
            for f in sorted(feats, key=lambda f: f.fc)]
    head = ["centre", "width (oct)", "residual dB", "z", "level SD", "seating spread"]
    numeric = tuple(range(1, len(head)))
    if veto:
        head += ["veto", "corrected dB"]
    return _table(head + ["corrected"], rows, numeric)


def consensus_notes(pf: ProfileFit) -> list[str]:
    st = pf.correction.settings
    out = []
    if st.consensus:
        c = next(iter(pf.correction.ears.values())).consensus
        how = f"smoothed over {c.smooth_oct:g} octave" if c.smooth_oct else "not smoothed"
        out.append(f"Reference: the raw curves of {', '.join(c.members)}, {how}.")
        if len(c.members) == 1:
            out.append("That's a single headphone's curve, not a consensus.")
        elif len(c.members) == 2:
            out.append("With two profiles it's their weighted mean, not a median, so one's peak reaches the "
                       "reference at about half height.")
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


def write_report(profile: Profile, pf: ProfileFit, path: Path | None = None) -> Path:
    corr = pf.correction
    st = corr.settings
    label = profile.read()["label"]
    now = datetime.now()
    path = path or profile.dir / "cache" / "report.html"  # overwritten each time
    path.parent.mkdir(parents=True, exist_ok=True)

    notes = []
    if st.reference == "iso226":
        notes.append("Reference: ISO 226 free-field equal-loudness contour. The design's default (diffuse "
                     "field) needs the ISO 389-7 table, which hasn't been sourced yet; corrections near "
                     "3 to 5 kHz depend on this choice.")
    if pf.pooled:
        notes.append(f"Pooled: includes the trials of {', '.join(pf.pooled)}.")
    notes += consensus_notes(pf)
    if st.method == "smooth":
        notes.append(f"Experimental smooth method: the baseline is the curve smoothed over {st.smooth_oct:g} "
                     "octave, and every residual is corrected. The features listed are only what's "
                     "significant; noise gets corrected too.")
    out = [f"<h1>Flatiron fit · {html.escape(label)}</h1>",
           f'<p class="meta">{now:%Y-%m-%d %H:%M} · {len(pf.sessions)} sessions · '
           + " · ".join(f"{EAR_NAMES[e]} {f.n_trials} trials" for e, f in pf.fits.items())
           + f" · preamp {corr.preamp_db:+.1f} dB (both channels)</p>"]
    out += [f'<p class="note">{html.escape(n)}</p>' for n in notes]

    for e, c in corr.ears.items():
        f = pf.fits[e]
        out.append(f"<h2>{EAR_NAMES[e]} ear</h2>")
        light = _png(ear_figure(c, c.session_R, "light", st.f_lo, st.f_hi))
        dark = _png(ear_figure(c, c.session_R, "dark", st.f_lo, st.f_hi))
        out.append(f'<div class="card"><picture><source srcset="{dark}" media="(prefers-color-scheme: dark)">'
                   f'<img src="{light}" alt="{EAR_NAMES[e]} ear: measured level, residual and correction">'
                   f"</picture></div>")
        out.append(f'<p class="meta">Listener noise σ₀ {f.sigma0:.2f} dB (×{f.treble_noise:.2f} above 10 kHz), lapse rate {f.lapse:.1%}, '
                   f"time-order bias {f.bias_db:+.2f} ± {f.bias_sd:.2f} dB (the first interval sounds "
                   f"{'louder' if f.bias_db >= 0 else 'quieter'}; fitted, so it doesn't distort the curve). "
                   f"Seating spread {f.tau_lo:.1f} dB below 5 kHz, {f.tau_hi:.1f} dB above 8 kHz "
                   + (f"(estimated from {f.n_groups} sessions)" if f.tau_fitted
                      else "(the prior: estimating it needs two sessions with 60+ trials for this ear)")
                   + f". Filter fit: χ² per point {c.fit.chi2:.2f}"
                   + (f"; {c.sessions_checked} sessions in the seating step" if c.sessions_checked else "")
                   + ".</p>")
        out.append('<p class="muted">In the feature tables, level SD says whether a feature is real; seating '
                   "spread says how far off the correction will be on a typical seating.</p>")
        if c.left_out:
            spans = ", ".join(f"{a:,.0f}–{b:,.0f} Hz (up to {m:.1f} dB)" for a, b, m in c.left_out)
            out.append(f'<p class="note">The band budget ({st.bands}) ran out. Left uncorrected: '
                       f"{html.escape(spans)}.</p>")
        out.append("<h3>Filters</h3>")
        if c.filters:
            comp = {id(x) for x in c.compensation}
            kept = {id(x) for x in c.fixed}
            rows = [(f"{flt.fc:,.0f}", f"{flt.gain_db:+.1f}", f"{flt.q:.2f}",
                     "checkpoint" if id(flt) in kept else
                     "tonality compensation" if id(flt) in comp else f"{ctb:.2f}")
                    for flt, ctb in zip(c.filters, c.fit.contributions)]
            out.append(_table(["Fc (Hz)", "gain (dB)", "Q", "χ² if removed"], rows, (0, 1, 2)))
        else:
            out.append('<p class="muted">No correction needed: nothing significant left after the reference '
                       "and baseline.</p>")
        for kind, title in ((NARROW, "Narrow features"), (BROAD, "Broad features (ear-gain region: "
                                                                  "depends on the reference)")):
            feats = [x for x in c.features if x.kind == kind and (x.accepted or abs(x.z) >= 1.5)]
            out.append(f"<h3>{title}</h3>")
            out.append(_features_table(feats, bool(st.veto) and st.veto_mode == "feature") if feats else '<p class="muted">None.</p>')

    out.append("<h2>Settings</h2>")
    out.append(_table(["setting", "value"], [(k, v) for k, v in asdict(st).items()]))
    doc = (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
           f"<title>Flatiron fit · {html.escape(label)}</title><style>{CSS}</style></head>"
           f"<body><main>{''.join(out)}</main></body></html>")
    path.write_text(doc)
    return path
