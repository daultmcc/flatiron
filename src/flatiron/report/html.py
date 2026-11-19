"""Static HTML report for a profile fit (figures embedded, no external files)."""

import base64
import html
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np

from flatiron.fit.features import BROAD, NARROW
from flatiron.fit.run import ProfileFit
from flatiron.report.thresholds import threshold_view
from flatiron.report.plots import EAR_NAMES, correction_figure, ear_figure, reference_figure, threshold_figure
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


REASONS = {"most seatings show little of it": "few seatings show it",
           "outside the correction range": "out of range",
           "broad: kept as tonality (correct it with --broad-strength)": "kept as tonality",
           "part of a broad region (searched inside it instead)": "part of a broad region"}


def reason_parts(f) -> list[str]:
    """Qualifiers on a corrected feature, in a few words each."""
    return (["inside a broad region"] if f.inside_broad else []) + \
        ([f"{f.weight:.0%} by significance"] if f.weight < 1 else []) + \
        ([f"{f.scale:.0%} for the median seating"] if f.reason and f.reason.startswith("corrected to the median")
         else [f.reason] if f.reason else [])


def short_reason(f) -> str:
    """Why a feature wasn't corrected, in a few words."""
    r = f.reason or ""
    return "not significant" if r.startswith("no two adjacent points") else REASONS.get(r, r)


def _reason(f) -> str:
    """What became of a feature, in a few words."""
    if not f.accepted:
        return "no: " + short_reason(f)
    parts = reason_parts(f)
    return "yes" + (f" ({', '.join(parts)})" if parts else "")


def _features_table(feats, veto=False, speaker=False):
    rows = [(f"{f.fc:,.0f}", f"{f.width_oct:.2f}", f"{f.peak_db:+.1f}", f"{f.z:.1f}",
             "" if f.level_sd is None else f"{f.level_sd:.2f}",
             "" if f.seat_spread is None else f"{f.seat_spread:.2f}",
             *(("" if f.veto is None else veto_line(f),
                "" if f.veto is None else f"{f.veto.correction_db:+.1f}",) if veto else ()),
             _reason(f))
            for f in sorted(feats, key=lambda f: f.fc)]
    head = ["Fc (Hz)", "width (oct)", "level (dB)", "z", "level SD", "position spread" if speaker else "seating spread"]
    numeric = tuple(range(0, len(head)))
    if veto:
        head += ["veto", "corrected (dB)"]
    return _table(head + ["significant" if speaker else "corrected"], rows, numeric)


KEPT_AT_HZ = (2000.0, 4000.0, 7000.0, 11000.0)
VETO_WITH_CONSENSUS = "Warning: the veto is on with a consensus reference, so it mostly just makes the correction smaller."


ANCHORED_TEXT = {"diffuse": "diffuse field at threshold (ISO 226 + ISO 389-7), plus your loudness-threshold gap",
                 "iso226": "free field at threshold (ISO 226), plus your loudness-threshold gap"}


def reference_description(st, pf: ProfileFit | None = None) -> str:
    anchored = pf is not None and st.threshold_fit and pf.gap is not None
    if st.consensus and pf is not None:
        c = next(iter(pf.correction.ears.values())).consensus
        sp = c.shares.speakers if c.shares is not None else []
        broad = ("; broad shape from the diffuse field at threshold, plus your loudness-threshold gap"
                 if anchored and st.consensus_broad == "diffuse" else "")
        return f"Reference: consensus of {', '.join(c.members)}" + (f", with {', '.join(sp)}" if sp else "") + broad
    if st.consensus:
        return f"Reference: consensus of {', '.join(st.consensus)}"
    if anchored and st.reference in ANCHORED_TEXT:
        return "Reference: " + ANCHORED_TEXT[st.reference]
    return "Reference: " + {"diffuse": f"diffuse field (ISO 226 + ISO 389-7) at {st.phon:g} phon",
                            "iso226": f"free field (ISO 226) at {st.phon:g} phon",
                            "none": "none"}.get(st.reference, st.reference)


def reference_notes(pf: ProfileFit, kept: bool = False) -> list[str]:
    """A consensus's values and caveats; kept: also the share kept at a few
    frequencies (the HTML page draws it instead)."""
    st = pf.correction.settings
    out = []
    if not st.consensus:
        return out
    ears = pf.correction.ears
    c = next(iter(ears.values())).consensus
    if kept:
        parts = []
        for e, ec in ears.items():
            x = ec.consensus
            at = [(f, float(np.interp(np.log2(f), np.log2(x.grid), x.share.k))) for f in KEPT_AT_HZ
                  if x.grid[0] <= f <= x.grid[-1]]
            parts.append(f"{e} " + ", ".join(f"{k:.2f} @ {f / 1000:g}k" for f, k in at))
        out.append("Kept share of narrow features: " + "; ".join(parts))
    sh = c.shares
    if sh is not None and sh.speakers:
        out.append(f"Speaker colouration: {sh.colour_db:.2f} dB ({sh.colour_how}); "
                   f"headphones-only share SD: {sh.gain_sd:.2f} ({sh.gain_sd_how})")
        if len(sh.speakers) == 1:
            out.append("Warning: with one speaker, its own colouration can't be told from your ears'.")
    if len(c.members) == 1:
        out.append("Warning: a single headphone's curve, not a consensus.")
    elif len(c.members) == 2:
        out.append("Warning: with two headphones the consensus is their weighted mean, not a median.")
    return out


def method_notes(pf: ProfileFit) -> list[str]:
    st = pf.correction.settings
    out = []
    if st.consensus and st.veto:
        out.append(VETO_WITH_CONSENSUS)
    if st.veto:
        who = ", ".join(st.veto)
        out.append(f"Veto: pointwise, against {who}" if st.veto_mode == "pointwise" else
                   f"Veto: each feature, against {who} (adding at most {st.veto_import_db:g} dB)")
        if len(st.veto) == 1:
            out.append("Warning: with one other profile, the veto's median is just the average.")
    if st.method == "match":
        out.append("Method: match (experimental): the whole curve is corrected; features listed are only what's "
                   "significant.")
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


def _spread(name: str, f, how: str) -> str:
    return (f"{name}: {f.tau_lo:.1f} @ 5k, {f.tau_mid:.1f} @ 8k, {f.tau_hi:.1f} @ 12k dB" + (f" ({how})" if how else ""))


def fitted_values(f, speaker: bool) -> list[str]:
    """The fitted listener values for an ear, one per line."""
    return [f"Listener noise: {f.sigma0:.2f} dB (×{f.treble_noise:.2f} above 10 kHz)",
            f"Lapse rate: {f.lapse:.1%}" + ("" if f.noise_trials else " (prior)"),
            f"Time-order bias: {f.bias_db:+.2f} ± {f.bias_sd:.2f} dB",
            _spread("Position spread", f, "assumed") if speaker else
            _spread("Seating spread", f, "" if f.tau_fitted else "prior")]


def listener_prefix(profile: Profile) -> str:
    """'Listener: Bob · ' when there are several listeners, else nothing."""
    from flatiron.session.store import listener_of, several_listeners

    x = listener_of(profile)
    return f"Listener: {x.label} · " if several_listeners(x.data) else ""


def interaural_note(pf: ProfileFit) -> str | None:
    """The two-ear fit's cross-ear, centring and threshold terms, in one line."""
    info = pf.fits["L"].interaural if pf.fits else None
    if not info:
        return None
    lo, hi = info["ref_hz"]
    thr = info.get("threshold_fit")
    src = [f"{n} {what}" for n, what in ((info["cross_trials"], "cross-ear"), (info["centring_trials"], "centring"),
                                         (thr["trials"] if thr else 0, "threshold"))
           if n] + (["thresholds"] if info["threshold"] else [])
    out = (f"Channel balance (left − right, {lo / 1000:g}–{hi / 1000:g} kHz): {info['c_db']:+.1f} ± "
           f"{info['c_sd']:.1f} dB, from {', '.join(src)}.")
    if info["b_db"] is not None:
        out += (f" Your cross-ear bias, left out of it: {info['b_db']:+.1f} ± {info['b_sd']:.1f} dB "
                f"(from {info['b_profiles']} profile{'' if info['b_profiles'] == 1 else 's'}).")
    return out


NO_GAP_NOTE = ("Warning: threshold fit on, but no profile has 150 threshold trials yet, so there's no estimate of "
               "your loudness-threshold gap: {what}.")


def top_notes(pf: ProfileFit, speaker: bool) -> list[str]:
    st = pf.correction.settings
    out = ["Speaker profile: evidence about your ears, so no correction."] if speaker else []
    if not speaker and pf.anchor_missing():
        if st.consensus:
            if st.consensus_broad == "diffuse":
                out.append(NO_GAP_NOTE.format(what="the consensus keeps its median broad shape"))
        elif st.reference in ANCHORED_TEXT:
            out.append(NO_GAP_NOTE.format(what=f"the {st.reference} reference isn't anchored at threshold"))
    note = interaural_note(pf)
    if note:
        out.append(note)
    out += method_notes(pf)
    if st.method == "features" and st.baseline == "joint":
        out.append("Baseline: joint (experimental).")
    if st.method == "features" and not st.shared_baseline:
        out.append("Baseline: one per ear.")
    if st.method == "smooth":
        out.append(f"Method: smooth over {st.smooth_oct:g} octave (experimental): every residual is corrected; "
                   "features listed are only what's significant.")
    return out


def write_report(profile: Profile, pf: ProfileFit, path: Path | None = None) -> Path:
    corr = pf.correction
    st = corr.settings
    label = profile.read()["label"]
    now = datetime.now()
    path = path or profile.dir / "cache" / "report.html"  # overwritten each time
    path.parent.mkdir(parents=True, exist_ok=True)
    speaker = profile.kind == "speaker"
    ears = list(corr.ears.items())
    ear_head = lambda e: f"<h3>{EAR_NAMES[e]} ear</h3>"

    out = [f"<h1>Flatiron fit · {html.escape(label)}</h1>",
           f'<p class="meta">{html.escape(listener_prefix(profile))}{now:%Y-%m-%d %H:%M} · {len(pf.sessions)} {"block" if speaker else "session"}{"" if len(pf.sessions) == 1 else "s"}'
           + (f" (pooled with {html.escape(', '.join(pf.pooled))})" if pf.pooled else "") + " · "
           + " · ".join(f"{EAR_NAMES[e]} {f.n_trials} trials" for e, f in pf.fits.items())
           + ("" if speaker else f" · preamp {corr.preamp_db:+.1f} dB") + "</p>"]
    out += [f'<p class="note">{html.escape(n)}</p>' for n in top_notes(pf, speaker)]

    out.append("<h2>Measured response</h2>")
    out.append(_columns([
        ear_head(e)
        + _picture(ear_figure(c, c.session_R, "light", st.f_lo, st.f_hi, speaker),
                   ear_figure(c, c.session_R, "dark", st.f_lo, st.f_hi, speaker),
                   f"{EAR_NAMES[e]} ear: measured level and residual")
        + '<p class="meta">' + "<br>".join(html.escape(v) for v in fitted_values(pf.fits[e], speaker)) + "</p>"
        for e, c in ears]))

    view = None if speaker else threshold_view(profile, pf)
    if view is not None:
        out.append("<h2>Thresholds</h2>")
        out += [f'<p class="meta">{html.escape(n)}</p>' for n in view.notes()]
        out.append(_columns([ear_head(e) + _picture(threshold_figure(view, e, "light"), threshold_figure(view, e, "dark"),
                                                    f"{EAR_NAMES[e]} ear: loudness and threshold")
                             for e, _ in ears]))

    out.append("<h2>Features</h2>")
    cols = []
    for e, c in ears:
        col = [ear_head(e)]
        for kind, title in ((NARROW, "Narrow"), (BROAD, "Broad")):
            feats = [x for x in c.features if x.kind == kind and (x.accepted or abs(x.z) >= 1.5)]
            col.append(f"<h4>{title}</h4>")
            col.append(_features_table(feats, not speaker and bool(st.veto) and st.veto_mode == "feature",
                                       speaker) if feats else '<p class="muted">None.</p>')
        unsure = ambiguous_features(c)
        if unsure:
            where = ", ".join(f"{x.fc:,.0f} Hz" for x in unsure)
            col.append(f'<p class="note">Baseline uncertain under {html.escape(where)}: part may be tonality.</p>')
        cols.append("".join(col))
    out.append(_columns(cols))

    if not speaker:
        out.append("<h2>Correction</h2>")
        cols = []
        for e, c in ears:
            col = [ear_head(e), _picture(correction_figure(c, "light", st.f_lo, st.f_hi),
                                         correction_figure(c, "dark", st.f_lo, st.f_hi),
                                         f"{EAR_NAMES[e]} ear: the correction")]
            if c.filters:
                comp = {id(x) for x in c.compensation}
                kept = {id(x) for x in c.fixed}
                rows = [(f"{flt.fc:,.0f}", f"{flt.gain_db:+.1f}", f"{flt.q:.2f}", f"{ctb:.2f}",
                         "checkpoint" if id(flt) in kept else "tonality" if id(flt) in comp else "")
                        for flt, ctb in zip(c.filters, c.fit.contributions)]
                col.append(_table(["Fc (Hz)", "gain (dB)", "Q", "χ² if removed", ""], rows, (0, 1, 2, 3)))
            else:
                col.append('<p class="muted">No correction needed.</p>')
            col.append(f'<p class="meta">Filter fit: χ² per point {c.fit.chi2:.2f}</p>')
            if c.left_out:
                spans = ", ".join(f"{a:,.0f}–{b:,.0f} Hz (up to {m:.1f} dB)" for a, b, m in c.left_out)
                col.append(f'<p class="note">Out of bands ({st.bands}), left uncorrected: {html.escape(spans)}.</p>')
            cols.append("".join(col))
        out.append(_columns(cols))

    out.append("<h2>Reference</h2>")
    out += [f'<p class="meta">{html.escape(n)}</p>' for n in [reference_description(st, pf)] + reference_notes(pf)]
    out.append(_columns([ear_head(e) + _picture(reference_figure(c, "light"), reference_figure(c, "dark"),
                                                f"{EAR_NAMES[e]} ear: the reference") for e, c in ears]))

    out.append("<h2>Settings</h2>")
    out.append(_table(["setting", "value"], [(k, f"{v:.4g}" if isinstance(v, float) else
                                                      ", ".join(v) if isinstance(v, tuple) else v)
                                                     for k, v in asdict(st).items()]))
    doc = (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
           f"<title>Flatiron fit · {html.escape(label)}</title><style>{CSS}</style></head>"
           f"<body><main>{''.join(out)}</main></body></html>")
    path.write_text(doc)
    return path
