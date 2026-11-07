"""Command-line entry point."""

import argparse
import sys

import numpy as np

from flatiron import EARS
from flatiron.audio.biquad import PeakingFilter
from flatiron.audio.stimulus import KINDS, StimulusSpec


def _profile(args, create: bool = False):
    """The profile named by -p, or the only one there is. None (after
    printing why) if that doesn't identify one."""
    from flatiron.session.store import Profile

    if args.profile:
        try:
            prof = Profile.open(args.profile) if create else Profile.existing(args.profile)
        except ValueError as e:
            print(e)
            return None
        if prof is None:
            print(f"No profile {args.profile!r}. See: flatiron profiles")
        return prof
    profiles = Profile.all()
    if len(profiles) == 1:
        return profiles[0]
    if not profiles:
        print("No profiles yet. Start one with: flatiron run -p 'Headphone name'")
    else:
        print("Several profiles; pick one with -p: " + ", ".join(p.read()["label"] for p in profiles))
    return None


def cmd_devices(args):
    from flatiron.audio.devices import list_output_devices

    for d in list_output_devices():
        mark = "*" if d.is_default else " "
        print(f"{mark}{d.index:3d}  {d.name}  [{d.hostapi}, {d.channels} ch, {d.default_samplerate:g} Hz]")
        for w in d.warnings:
            print(f"        ! {w}")
    print("\n* = default. Pass --device <index> to other commands; a wired output at 48 kHz is recommended.")


VOLUME_REMINDER = ("Reminder: if you changed your system volume for Flatiron, turn it back down "
                   "before playing anything else.")


def _player(args):
    from flatiron.audio.player import NullPlayer, StreamPlayer

    return NullPlayer() if getattr(args, "dry_run", False) else StreamPlayer(args.device)


def _current(prof):
    """The profile's fit as of now (computed on demand), or None after
    printing why there isn't one."""
    from flatiron.fit.run import NotEnoughTrials, current_fit

    try:
        return current_fit(prof, on_fit=lambda: print("Fitting the new trials (a few seconds)...", flush=True))
    except NotEnoughTrials as e:
        print(f"Can't fit {prof.read()['label']!r} yet: {e}. Run some sessions first: flatiron run")
    except (ValueError, FileNotFoundError) as e:
        print(e)
    return None


def cmd_tone(args):
    from flatiron.audio.limiter import DEFAULT_LEVEL_DB
    from flatiron.audio.preview import PreviewEQ
    from flatiron.ui.tone import ToneApp

    prof, eq = None, None
    if args.profile:
        prof = _profile(args)
        if prof is None:
            return 1
        pf = _current(prof)
        if pf is not None:
            eq = PreviewEQ.from_fit(pf.summary())
            print("Make sure the correction isn't also applied by the system (a Flatiron PipeWire sink or LSP),")
            print("or Q's 'on' state gets it twice.")
    level = prof.last_level() if prof is not None else None
    ear = {"L": "L", "R": "R", "both": "LR"}[args.ear]
    app = ToneApp(args.freq, ear, DEFAULT_LEVEL_DB if level is None else level, eq=eq, profile=prof,
                  device=args.device, dry_run=args.dry_run)
    app.run()
    print(VOLUME_REMINDER)
    return 0


def _settings(args, profile):
    from flatiron.apo import parse_apo
    from flatiron.session.runner import RunSettings

    eq, text, note = None, None, args.eq_note or "none"
    if args.active_eq:
        text = open(args.active_eq).read()
        eq = parse_apo(text)
        note = args.eq_note or f"APO file {args.active_eq}"
    spec = StimulusSpec(kind=args.stimulus, bandwidth_oct=1 / args.bands_per_octave)
    return RunSettings(stimulus=spec, session_minutes=args.minutes,
                       active_eq=eq, eq_note=note, eq_text=text)


def _launch(args, sweep: bool, profile=None, **extra):
    """extra: RunSettings overrides (verify: preview, max_trials)."""
    from dataclasses import replace

    from flatiron.session.runner import TrialRunner
    from flatiron.session.sweep import SweepTiming
    from flatiron.ui.app import FlatironApp

    profile = profile or _profile(args, create=True)
    if profile is None:
        return 1
    timing, sweep_eq = SweepTiming(), None
    if sweep:
        seconds = getattr(args, "sweep_seconds", None) or timing.duration
        if not 10 <= seconds <= 90:
            print("--sweep-seconds must be 10 to 90 (each direction)")
            return 1
        timing = SweepTiming(duration=seconds)
        if profile.sessions():
            from flatiron.audio.preview import PreviewEQ
            from flatiron.fit.run import NotEnoughTrials, current_fit

            try:
                pf = current_fit(profile, on_fit=lambda: print("Fitting, to apply the current correction "
                                                               "during the sweep...", flush=True))
                sweep_eq = PreviewEQ.from_fit(pf.summary())
                print("The sweep plays through the current correction (Q in the sweep turns it off). Make sure")
                print("it isn't also applied by the system, or it's applied twice.")
            except NotEnoughTrials:
                pass
            except (ValueError, FileNotFoundError) as e:
                print(f"Can't apply the correction during the sweep: {e}")
    settings = lambda: replace(_settings(args, profile), **extra)
    settings()  # validate options before the UI starts
    device = {"index": args.device, "dry_run": args.dry_run}
    if not args.dry_run:
        from flatiron.audio.devices import list_output_devices

        devs = {d.index: d for d in list_output_devices()}
        d = devs.get(args.device) or next((d for d in devs.values() if d.is_default), None)
        if d:
            device.update(name=d.name, hostapi=d.hostapi, warnings=d.warnings)
            for w in d.warnings:
                print(f"! {d.name}: {w}")
    app = FlatironApp(lambda: TrialRunner(profile, settings(), device=device), _player(args), sweep=sweep,
                      sweep_timing=timing, sweep_eq=sweep_eq)
    app.run()
    print(VOLUME_REMINDER)
    return 0


def cmd_run(args):
    return _launch(args, sweep=args.sweep)


def cmd_verify(args):
    from flatiron.audio.preview import PreviewEQ
    from flatiron.fit.run import checkpoint, leftovers

    prof = _profile(args)
    if prof is None:
        return 1
    pf = _current(prof)
    if pf is None:
        return 1
    eq = PreviewEQ.from_fit(pf.summary())
    print(f"Verifying fit {eq.fit_hash}: Flatiron applies the correction to every burst itself.")
    print("Make sure it isn't also applied by the system (don't play through a Flatiron PipeWire sink or an")
    print("LSP instance with this correction), or it will be applied twice.")
    if checkpoint(prof) is None:
        print("No checkpoint: afterwards the fit is redone with these trials included, and its filters may")
        print("move. To keep these filters and only add to them, quit and run: flatiron config --checkpoint")
    n_before = len(prof.sessions())
    rc = _launch(args, sweep=False, profile=prof, preview=eq, max_trials=args.trials)
    if rc or len(prof.sessions()) == n_before:
        return rc
    print("\nChecking what's left with the correction applied (all trials, verify included)...")
    applied = {e: [PeakingFilter(f.fc, f.gain_db, f.q) for f in fs] for e, fs in eq.channels.items()}
    pf, left = leftovers(prof, applied)
    for e, feats in left.items():
        who = f"{e} ({pf.fits[e].n_trials} trials)"
        if not feats:
            print(f"  {who}: nothing significant left. The correction holds.")
            continue
        print(f"  {who}: still significant: " + ", ".join(
            f"{x.fc:.0f} Hz ({-x.peak_db:+.1f} dB, {x.kind})" for x in sorted(feats, key=lambda x: x.fc)))
    if any(left.values()):
        print("The fit now includes these trials" + (", and adds filters on top of the checkpoint"
                                                     if checkpoint(prof) else "") + ". See: flatiron report")
    return 0


def cmd_audition(args):
    from flatiron.audio.preview import PreviewEQ
    from flatiron.ui.audition import AuditionApp, load_source

    prof = _profile(args)
    if prof is None:
        return 1
    try:
        sources = [load_source(x) for x in (args.source or ["pink", "sweep"])]
    except (FileNotFoundError, ValueError) as e:
        print(e)
        return 1
    pf = _current(prof)
    if pf is None:
        return 1
    eq = PreviewEQ.from_fit(pf.summary())
    print("Make sure the correction isn't also applied by the system (a Flatiron PipeWire sink or LSP),")
    print("or the 'on' state gets it twice.")
    AuditionApp(sources, eq, _player(args), f"{prof.read()['label']} · fit {eq.fit_hash}").run()
    print(VOLUME_REMINDER)
    return 0


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_profiles(args):
    from flatiron.session.store import Profile

    action = getattr(args, "action", None)
    if action == "new":
        try:
            if Profile.existing(args.name) is not None:
                print(f"There's already a profile {args.name!r}.")
                return 1
            prof = Profile.open(args.name)
        except ValueError as e:
            print(e)
            return 1
        prof.update(kind=args.kind)
        print(f"Created {args.name!r} ({args.kind}).")
        return 0
    if action == "set":
        try:
            prof = Profile.existing(args.name)
        except ValueError:
            prof = None
        if prof is None:
            print(f"No profile {args.name!r}. See: flatiron profiles")
            return 1
        if args.kind:
            prof.update(kind=args.kind)
        if args.label:
            prof.update(label=args.label)
        return 0
    profiles = Profile.all()
    if not profiles:
        print("No profiles yet. Start one with: flatiron run --profile 'Headphone name'")
        return
    for p in profiles:
        info = p.read()
        sessions = p.sessions()
        n = {e: sum(t.ear == e for s in sessions for t in s.trials) for e in EARS}
        level = p.last_level(sessions)
        level_s = f"last level {level:+.0f} dBFS" if level is not None else "no sessions yet"
        last = sessions[-1].started[:10] if sessions else "-"
        open_s = sum(s.ended is None for s in sessions)
        kind = info.get("kind", "headphone")
        print(f"{info['label']}  ({p.slug})" + ("" if kind == "headphone" else f"  [{kind}]"))
        print(f"    {len(sessions)} sessions, L {n['L']} / R {n['R']} trials, last {last}, {level_s}"
              + (f", {open_s} not closed" if open_s else ""))


def cmd_sessions(args):
    prof = _profile(args)
    if prof is None:
        return 1
    sessions = prof.sessions()
    if args.delete:
        match = [s for s in sessions if s.id == args.delete]
        if not match:
            print(f"No session {args.delete!r} in {prof.read()['label']!r}.")
            return 1
        s = match[0]
        if _confirm(f"Delete session {s.id} ({len(s.trials)} trials)? This can't be undone.", args.yes):
            prof.delete(s)
            print("Deleted.")
        else:
            print("Not deleted.")
        return 0
    if not sessions:
        print(f"No sessions for {prof.read()['label']!r} yet.")
        return
    for s in sessions:
        n = {e: sum(t.ear == e for t in s.trials) for e in EARS}
        catch = [t for t in s.trials if t.catch]
        acc = f"{np.mean([(t.offset > 0) == t.a_louder for t in catch]):.0%}" if catch else "-"
        live = [t for t in s.trials if not t.undone]
        fast = sum(t.fast for t in live)
        first = f"{np.mean([t.response == 'first' for t in live]):.0%}" if live else "-"
        mins = s.trials[-1].t / 60 if s.trials else 0
        print(f"{s.id}  L {n['L']:4d}  R {n['R']:4d}  {mins:4.1f} min  catch {acc:>4}  "
              f"fast {fast:3d}  chose first {first:>4}  EQ: {s.eq_context.get('note')}"
              f"{'  [verify ' + s.preview.get('fit_hash', '') + ']' if s.preview else ''}"
              f"{'' if s.ended else '  (not closed)'}")
    trials, _ = prof.history(sessions)
    print(f"total: L {len(trials['L'])}, R {len(trials['R'])}")


# Fit settings `config` can change: flags, CorrectionSettings field, argparse
# keywords, and conversions between the flag's units and the field's.
PERCENT = (lambda v: v / 100, lambda v: v * 100)
NOT = (lambda v: not v, lambda v: not v)
NAMES = (lambda v: () if v == "none" else tuple(x.strip() for x in v.split(",") if x.strip()),
         lambda v: ",".join(v) or "none")
FIT_OPTIONS = [
    (("-w", "--width"), "max_width_oct", dict(type=float, help="max feature width W in octaves (1/3 to 2)")),
    (("-r", "--reference"), "reference",
     dict(help="iso226, diffuse, none, a CSV of freq_hz,db, or consensus:A,B,C for the raw curves of those "
               "profiles (this one only if listed)")),
    (("--consensus-smooth",), "consensus_smooth_oct",
     dict(type=float, help="smoothing (FWHM) of a consensus reference in octaves, up to 2; 0: none. The reference "
                           "then keeps the shape and leaves features narrower than this to detection and --veto")),
    (("--veto",), "veto",
     dict(metavar="PROFILE[,PROFILE]|none",
          help="correct each feature toward the median of this profile's and those profiles' features there"), NAMES),
    (("--veto-mode",), "veto_mode",
     dict(choices=("feature", "pointwise"),
          help="feature (default): toward the median feature; pointwise: frequency by frequency, by what exceeds "
               "the median of the residuals, never past zero")),
    (("--veto-import",), "veto_import_db",
     dict(type=float, help="where the median feature is somewhere else, the most the veto may add (dB); 0: nothing")),
    (("--veto-softness",), "veto_softness",
     dict(type=float, help="above 0, the feature veto weighs its candidates instead of picking one, with no sharp "
                           "edges; 0: all or nothing")),
    (("--phon",), "phon", dict(type=float, help="listening level assumed by the reference")),
    (("-k", "--k"), "k", dict(type=float, help="significance for narrow features (SDs)")),
    (("--k-broad",), "k_broad", dict(type=float, help="significance for broad features (SDs)")),
    (("--soft-width",), "soft_width",
     dict(type=float, help="partly correct features within this many SDs of the significance threshold (0: all or nothing)")),
    (("--broad",), "preserve_tonality",
     dict(action=argparse.BooleanOptionalAction,
          help="correct broad features too, toward the fixed target (off, the default: correct narrow "
               "features only and keep the original broad shape, leaving broad tonal changes to you)"), NOT),
    (("-s", "--broad-strength"), "broad_strength",
     dict(type=float, help="how much of each broad feature to correct, 0 to 100%% (needs --broad)"), PERCENT),
    (("-b", "--bands"), "bands", dict(type=int, help="correction bands per channel (max 128; LSP and EasyEffects hold 32)")),
    (("--fit-tolerance",), "fit_tol_db",
     dict(type=float, help="how close the filters must come to the target, in dB (0.1 to 6)")),
    (("--boost-narrow",), "max_boost_narrow_db", dict(type=float, help="max boost for narrow features (dB)")),
    (("--boost-broad",), "max_boost_broad_db", dict(type=float, help="max boost for broad features (dB)")),
    (("--max-cut",), "max_cut_db", dict(type=float, help="max cut (dB)")),
    (("--f-lo",), "f_lo", dict(type=float, help="lower correction limit (Hz)")),
    (("--f-hi",), "f_hi", dict(type=float, help="upper correction limit (Hz)")),
    (("--consistency",), "consistency",
     dict(action=argparse.BooleanOptionalAction,
          help="correct each feature to its median seating's level, and drop it if most seatings show little of it")),
    (("--method",), "method",
     dict(choices=("features", "smooth", "match"),
          help="features (default): correct the significant features found against the baseline; "
               "smooth (experimental): the baseline is the curve smoothed over --smooth-width, and every "
               "residual is corrected, with no feature detection (more correction, noise included); "
               "match (experimental): correct the whole curve toward the reference, tonality included")),
    (("--smooth-width",), "smooth_oct",
     dict(type=float, help="smoothing width (FWHM) in octaves for --method smooth, 1/8 to 1")),
]


def _option_name(flags) -> str:
    return flags[-1].lstrip("-")


def _show(field_name, value) -> str:
    for flags, f, kw, *conv in FIT_OPTIONS:
        if f == field_name:
            if conv:
                value = conv[0][1](value)
            if isinstance(value, bool):
                return "on" if value else "off"
            return f"{value:g}" if isinstance(value, float) else str(value)
    return str(value)


def cmd_config(args):
    from dataclasses import asdict, replace

    from flatiron.fit.pipeline import CorrectionSettings
    from flatiron.fit.reference import CONSENSUS_PREFIX, reference
    from flatiron.fit.run import checkpoint, fit_sessions, profile_session_ids, profile_settings, \
        set_checkpoint, set_profile_settings
    from flatiron.session.store import Profile

    prof = _profile(args)
    if prof is None:
        return 1
    label = prof.read()["label"]
    old = profile_settings(prof)
    names = {_option_name(flags): f for flags, f, *_ in FIT_OPTIONS}
    changes = {}
    if args.reset is not None:
        unknown = [n for n in args.reset if n not in names]
        if unknown:
            print(f"Unknown setting(s) {', '.join(unknown)}; they are: {', '.join(names)}")
            return 1
        default = CorrectionSettings()
        for n in args.reset or names:
            changes[names[n]] = getattr(default, names[n])
    for flags, f, kw, *conv in FIT_OPTIONS:
        v = getattr(args, f)
        if v is not None:
            changes[f] = conv[0][0](v) if conv else v
    def slugs(listed, what):
        out = []
        for name in listed:
            try:
                other = Profile.existing(name)
            except ValueError:
                other = None
            if other is None:
                print(f"No profile {name!r} for the {what} (nothing changed). See: flatiron profiles")
                return None
            out.append(other.slug)
        return tuple(dict.fromkeys(out))

    if str(changes.get("reference", "")).startswith(CONSENSUS_PREFIX):
        members = slugs(NAMES[0](changes["reference"][len(CONSENSUS_PREFIX):]), "consensus")
        if members is None:
            return 1
        if not members:
            print("A consensus needs at least one profile (nothing changed).")
            return 1
        changes["reference"] = CONSENSUS_PREFIX + ",".join(members)
    elif "reference" in changes and changes["reference"] not in ("iso226", "diffuse", "none"):
        from pathlib import Path

        changes["reference"] = str(Path(changes["reference"]).expanduser().resolve())
    if changes.get("veto"):
        changes["veto"] = slugs(changes["veto"], "veto")
        if changes["veto"] is None:
            return 1
    try:
        new = replace(old, **changes)
        if not new.consensus:
            reference(new.reference, [1000.0], new.phon)
    except (ValueError, FileNotFoundError) as e:
        print(e)
        return 1
    if args.broad_strength is not None and new.preserve_tonality:
        print("--broad-strength only applies with broad-feature correction: add --broad (nothing changed).")
        return 1
    if args.smooth_oct is not None and new.method != "smooth":
        print("--smooth-width only applies with --method smooth (nothing changed).")
        return 1
    if (args.veto_mode is not None or args.veto_import_db is not None or args.veto_softness is not None) and not new.veto:
        print("--veto-mode, --veto-import and --veto-softness only apply with --veto (nothing changed).")
        return 1
    if args.veto_import_db is not None and new.veto_mode != "feature":
        print("--veto-import only applies with --veto-mode feature (nothing changed).")
        return 1
    if args.consensus_smooth_oct is not None and not new.consensus:
        print("--consensus-smooth only applies with a consensus reference (nothing changed).")
        return 1
    if "reference" in changes or "veto" in changes:
        # Checked here too, so a bad list never gets saved. Only when one's being
        # set: --pool none has to work on a profile pooling has put in conflict.
        from flatiron.fit.run import consensus_members, veto_members

        try:
            consensus_members(prof, new)
            if new.veto and not veto_members(prof, new):
                raise ValueError("--veto needs at least one other profile.")
        except ValueError as e:
            print(f"{e} (nothing changed)")
            return 1
    pool = None
    if args.pool:
        pool = [] if args.pool == "none" else [x.strip() for x in args.pool.split(",") if x.strip()]
        for i, name in enumerate(pool):
            try:
                other = Profile.existing(name)
            except ValueError:
                other = None
            if other is None:
                print(f"No profile {name!r} to pool (nothing changed). See: flatiron profiles")
                return 1
            if other.slug == prof.slug:
                print("A profile can't be pooled into itself (nothing changed).")
                return 1
            pool[i] = other.slug
    if changes:
        set_profile_settings(prof, new)
    if "reference" in changes and new.consensus and new.reference != old.reference:
        print("Consensus of " + ", ".join(Profile(s).read()["label"] for s in new.consensus) + ".")
        if len(new.consensus) == 1:
            print("Note: that's a single headphone's curve, not a consensus.")
        elif len(new.consensus) == 2:
            print("Note: with two profiles it's their weighted mean, not a median, so one's peak reaches the "
                  "reference at about half height.")
    if pool is not None:
        prof.update(pool=pool or None)
        if pool:
            print(f"Pooled: fits include the trials of {', '.join(Profile(s).read()['label'] for s in pool)} "
                  "(their sessions stay in their own profiles).")
        else:
            print("Not pooled: fits use this profile's own trials.")
    if args.sessions:
        if args.sessions == "all":
            prof.update(sessions_used=None)
        else:
            ids = args.sessions.split(",")
            missing = set(ids) - {s.id for s in fit_sessions(prof)}
            if missing:
                print(f"No such session(s): {', '.join(sorted(missing))}. See: flatiron sessions")
                return 1
            prof.update(sessions_used=ids)
    if args.reset_checkpoint:
        if checkpoint(prof) is None:
            print("There's no checkpoint to reset.")
        else:
            set_checkpoint(prof, None)
            print("Checkpoint reset: the fit chooses all its filters again.")
    if args.checkpoint:
        pf = _current(prof)
        if pf is None:
            return 1
        set_checkpoint(prof, pf)
        n = " / ".join(f"{e} {len(c.filters)}" for e, c in pf.correction.ears.items())
        print(f"Checkpoint: the current filters ({n}) stay as they are from now on; fits only add to them.")
    for f, v in changes.items():
        if v != getattr(old, f):
            print(f"{_option_name(next(fl for fl, ff, *_ in FIT_OPTIONS if ff == f))}: "
                  f"{_show(f, getattr(old, f))} -> {_show(f, v)}")
    if changes or args.sessions or args.checkpoint or args.reset_checkpoint or pool is not None:
        return 0
    # Nothing to change: show the settings.
    st, default = asdict(profile_settings(prof)), asdict(CorrectionSettings())
    print(f"Fit settings for {label!r} (* = changed from the default):")
    for flags, f, *_ in FIT_OPTIONS:
        mark = "*" if st[f] != default[f] else " "
        dflt = "" if mark == " " else f"   (default {_show(f, default[f])})"
        unused = ("   (unused without --broad)" if f == "broad_strength" and st["preserve_tonality"] else
                  "   (unused without --method smooth)" if f == "smooth_oct" and st["method"] != "smooth" else
                  "   (unused without a consensus reference)" if f == "consensus_smooth_oct"
                  and not profile_settings(prof).consensus else
                  "   (unused without --veto)" if f in ("veto_mode", "veto_import_db", "veto_softness") and not st["veto"] else "")
        print(f"  {mark} {_option_name(flags):18} {_show(f, st[f])}{dflt}{unused}")
    ids = profile_session_ids(prof)
    print(f"  sessions: {'all' if not ids else ', '.join(ids)}")
    pooled = prof.read().get("pool") or []
    print(f"  pooled with: {', '.join(pooled) if pooled else 'none'}")
    cp = prof.read().get("checkpoint")
    if cp:
        n = " / ".join(f"{e} {len(v)}" for e, v in cp["filters"].items())
        print(f"  checkpoint: {n} filters fixed since {cp['made'][:16].replace('T', ' ')} "
              "(drop it with --reset-checkpoint)")
    else:
        print("  checkpoint: none")
    return 0


def _print_fit(pf):
    from flatiron.report.html import consensus_notes, veto_line

    corr = pf.correction
    if pf.pooled:
        print(f"Pooled with {', '.join(pf.pooled)}: {len(pf.sessions)} sessions in all.")
    for note in consensus_notes(pf):
        print(note)
    if corr.settings.method == "smooth":
        print(f"Method: smooth (experimental): every residual against a {corr.settings.smooth_oct:g}-octave "
              "smooth is corrected; the features below are only what's significant.")
    for e, c in corr.ears.items():
        f = pf.fits[e]
        print(f"\n{e} ear: {f.n_trials} trials, noise {f.sigma0:.2f} dB (×{f.treble_noise:.2f} above 10 kHz), lapse {f.lapse:.1%}, "
              f"time-order bias {f.bias_db:+.2f} ± {f.bias_sd:.2f} dB (+ = first interval heard louder)")
        print(f"  seating spread {f.tau_lo:.1f} dB below 5 kHz, {f.tau_hi:.1f} dB above 8 kHz "
              f"({f'estimated from {f.n_groups} sessions' if f.tau_fitted else 'the prior: needs two sessions of 60+ trials'})")
        comp = {id(x) for x in c.compensation}
        kept = {id(x) for x in c.fixed}
        for flt in c.filters:
            note = ("  (checkpoint)" if id(flt) in kept else
                    "  (tonality compensation)" if id(flt) in comp else "")
            print(f"  PK  Fc {flt.fc:7.0f} Hz  Gain {flt.gain_db:+5.1f} dB  Q {flt.q:5.2f}{note}")
        if not c.filters:
            print("  no correction needed")
        for kind in ("narrow", "broad"):
            acc = sorted((x for x in c.features if x.accepted and x.kind == kind), key=lambda x: x.fc)
            rej = [x for x in c.features if not x.accepted and x.reason == "most seatings show little of it"]
            if acc:
                print(f"  {kind} features: " + ", ".join(f"{x.fc:.0f} Hz ({_correction_of(x):+.1f} dB"
                                                         + (", inside a broad region" if x.inside_broad else "")
                                                         + (f", {x.weight:.0%} by significance" if x.weight < 1 else "")
                                                         + ")" for x in acc))
            if rej and kind == "narrow":
                print("  dropped, most seatings show little of it: " + ", ".join(f"{x.fc:.0f} Hz" for x in rej))
            scaled = [x for x in acc if x.reason]
            if scaled:
                print(f"  {kind}, corrected to the median seating's level: "
                      + ", ".join(f"{x.fc:.0f} Hz ({x.scale:.0%})" for x in scaled))
        for x in sorted((x for x in c.features if x.veto is not None), key=lambda x: x.fc):
            print(f"  veto {x.fc:.0f} Hz: {_correction_of(x):+.1f} dB ({veto_line(x)})"
                  + ("" if x.accepted else f": {x.reason}"))
        for a, b, m in c.left_out:
            print(f"  left out (band budget): {a:.0f}-{b:.0f} Hz, up to {m:.1f} dB")
    print(f"\npreamp {corr.preamp_db:+.1f} dB (both channels)")
    if corr.settings.reference == "iso226":
        print("reference: ISO 226 free field (the diffuse-field table isn't sourced yet)")
    print("settings: flatiron config")


def _correction_of(f) -> float:
    if f.veto is not None:
        return 0.0 if not f.accepted else f.veto.correction_db
    return -f.peak_db * f.weight if f.correction_db is None else f.correction_db


def cmd_compare(args):
    from dataclasses import replace

    from flatiron import EARS as ALL_EARS
    from flatiron.fit.compare import Member
    from flatiron.fit.consensus import KINDS_VOTING
    from flatiron.fit.pipeline import CorrectionSettings
    from flatiron.fit.reference import reference
    from flatiron.fit.run import NotEnoughTrials, perceptual_fits, profile_session_ids, profile_settings
    from flatiron.paths import data_dir
    from flatiron.report.compare import MAX_PROFILES, compare, print_comparison, write_comparison
    from flatiron.session.store import Profile

    if len(args.compare) < 2:
        print("--compare needs at least two profiles.")
        return 1
    if len(args.compare) > MAX_PROFILES:
        print(f"--compare takes at most {MAX_PROFILES} profiles at once.")
        return 1
    profiles = []
    for name in args.compare:
        try:
            p = Profile.existing(name)
        except ValueError:
            p = None
        if p is None:
            print(f"No profile {name!r}. See: flatiron profiles")
            return 1
        profiles.append(p)

    from itertools import combinations

    from flatiron.fit.run import shares_trials

    for a, b in combinations(profiles, 2):
        if shares_trials(a, b):
            print(f"Warning: {a.read()['label']} and {b.read()['label']} share trials through pooling, so they'll "
                  "agree more than separate measurements would.")
    own = [profile_settings(p) for p in profiles]

    def common(what, value):
        vals = {getattr(s, what) for s in own}
        return value if value is not None else (vals.pop() if len(vals) == 1 else None)

    # A comparison needs a fixed reference; a consensus would mean something different per profile.
    if any(s.consensus for s in own) and args.reference is None:
        print("A consensus reference isn't used for comparisons: this uses iso226 (pick another with -r).")
    own = [replace(s, reference=CorrectionSettings().reference) if s.consensus else s for s in own]
    ref, phon = common("reference", args.reference), common("phon", None)
    if ref is None:
        print("These profiles use different references: pick one with -r (iso226, diffuse, none or a CSV).")
        return 1
    try:
        st = CorrectionSettings(max_width_oct=args.width or 1.0, reference=ref, phon=phon or CorrectionSettings().phon,
                                k=common("k", None) or CorrectionSettings().k)
        reference(st.reference, [1000.0], st.phon)
    except (ValueError, FileNotFoundError) as e:
        print(e)
        return 1
    ears = ALL_EARS if args.ear == "both" else (args.ear,)
    voting = tuple(KINDS_VOTING) + tuple(args.include_kind or ())
    announce = [lambda: print("Fitting the new trials (a few seconds)...", flush=True)]

    def on_fit():
        if announce:
            announce.pop()()

    try:
        members = [Member(p.read()["label"], p.kind, perceptual_fits(p, profile_session_ids(p), on_fit)[0])
                   for p in profiles]
        comparisons = compare(members, st, ears, voting)
    except (NotEnoughTrials, ValueError) as e:
        print(e)
        return 1
    print_comparison(comparisons, members)
    if args.html:
        import webbrowser

        path = write_comparison(data_dir() / "cache" / "comparison.html", comparisons, members, st)
        print(f"report: {path}")
        webbrowser.open(path.as_uri())
    return 0


def cmd_report(args):
    if args.compare:
        return cmd_compare(args)
    for opt in ("width", "reference", "include_kind"):
        if getattr(args, opt):
            print(f"--{opt.replace('_', '-')} only applies with --compare.")
            return 1
    if args.ear != "both":
        print("--ear only applies with --compare.")
        return 1
    prof = _profile(args)
    if prof is None:
        return 1
    pf = _current(prof)
    if pf is None:
        return 1
    _print_fit(pf)
    if args.csv:
        from flatiron.report.compare import write_csv

        write_csv(args.csv, pf)
        print(f"csv: {args.csv}")
    if args.html:
        import webbrowser

        from flatiron.report.html import write_report

        path = write_report(prof, pf)
        print(f"report: {path}")
        webbrowser.open(path.as_uri())
    return 0


def cmd_export(args):
    from pathlib import Path

    from flatiron.apo import parse_apo
    from flatiron.export import apo, easyeffects, lsp, pipewire, tincan
    from flatiron.export.plan import build_plan, response_db

    prof = _profile(args)
    if prof is None:
        return 1
    pref = None
    if args.merge or args.merge_left or args.merge_right:
        both = args.merge
        left, right = args.merge_left or both, args.merge_right or both
        if not (left and right):
            print("Merging needs a preference preset for both channels (--merge, or --merge-left and --merge-right).")
            return 1
        try:
            pref = {"L": parse_apo(Path(left).read_text()), "R": parse_apo(Path(right).read_text())}
        except ValueError as e:
            print(e)
            return 1
    pf = _current(prof)
    if pf is None:
        return 1
    plan = build_plan(pf.summary(), pref)
    if args.target == "tincan" and args.preamp is not None:
        print("TinCan presets carry no preamp: TinCan sets one for all loaded presets "
              "(match bypass, safe or manual, in its main window). Leave out --preamp.")
        return 1
    if args.preamp not in (None, "safe"):
        try:
            plan.preamp_db = float(args.preamp)
        except ValueError:
            print(f"--preamp takes 'safe' or a gain in dB, not {args.preamp!r}")
            return 1
        peak = max(float(response_db(fs).max()) for fs in plan.channels.values()) + plan.preamp_db
        if peak > 0:
            print(f"Note: with this preamp the EQ boosts up to {peak:+.1f} dB at its peak, so loud "
                  "material in that band can clip.")
    ext = {"apo": ".txt", "lsp": ".cfg", "easyeffects": ".json", "pipewire": ".conf", "tincan": ".toml"}[args.target]
    label = prof.read()["label"]
    preset_name = f"{label} Flatiron {plan.fit_hash}{' merged' if plan.merged else ''}"
    if args.output:
        out = Path(args.output)
    elif args.target == "tincan":
        out = tincan.presets_dir() / f"{tincan.slug(preset_name)}.toml"
        if not out.parent.is_dir():
            print(f"Can't find TinCan's preset folder ({out.parent}): start TinCan once to create it, "
                  "or write the preset elsewhere with -o and import it in TinCan's Manage presets.")
            return 1
    else:
        out = Path(f"{prof.slug}-{plan.fit_hash}{'-merged' if plan.merged else ''}{ext}")
    if not out.suffix:
        out = out.with_suffix(ext)
    if not out.parent.is_dir():
        print(f"Can't write {out}: the folder {out.parent} doesn't exist.")
        return 1
    written = []
    try:
        if args.target == "pipewire":
            if args.check:
                import tempfile

                from flatiron.export import verify

                if not verify.pipewire_available():
                    print("Can't check: PipeWire isn't running (or pw-play/pw-record are missing).")
                    return 1
                with tempfile.TemporaryDirectory() as tmp:
                    err = verify.check_pipewire(plan, tmp)
                worst = max(err.values())
                print(f"Measured through a private PipeWire filter-chain: max error {worst:.2f} dB (tolerance 0.3)")
                if worst > 0.3:
                    print("Not writing it: PipeWire doesn't do what Flatiron predicts.")
                    return 1
            out.write_text(pipewire.write(plan, label, prof.slug))
            written.append(out)
        elif args.target == "tincan":
            out.write_text(tincan.write(plan, preset_name))
            written.append(out)
        elif args.target == "apo":
            # One file per channel: name_L.txt, name_R.txt.
            for e, text in apo.write(plan).items():
                path = out.with_name(f"{out.stem}_{e}{out.suffix}")
                path.write_text(text)
                written.append(path)
        else:
            from flatiron.export import verify

            if args.target == "lsp":
                text, check = lsp.write(plan), verify.check_lsp
            else:
                text, check = easyeffects.write(plan), verify.check_easyeffects
            if verify.available():
                err = check(text, plan)
                worst = max(err.values())
                print(f"Checked against LSP's own filters: max error {worst:.3f} dB (tolerance 0.3)")
                if worst > 0.3:
                    print("Not writing it: the export doesn't match Flatiron's prediction.")
                    return 1
            out.write_text(text)
            written.append(out)
    except ValueError as e:
        print(e)
        return 1
    bands = " / ".join(f"{e} {len(fs)}" for e, fs in plan.channels.items())
    preamp = "" if args.target == "tincan" else f", preamp {plan.preamp_db:+.2f} dB"
    print(f"{'Merged' if plan.merged else 'Correction-only'} export, bands {bands}{preamp}")
    if args.target == "tincan":
        print(f"TinCan preset \"{preset_name}\"" + (": add it from the main window's preset list."
                                                     if not args.output else ": import it in Manage presets."))
    elif not plan.merged:
        print("Stack it after any EQ you already use (or in its own instance).")
    if args.target == "pipewire":
        print("Install: copy it to ~/.config/pipewire/pipewire.conf.d/ and restart PipeWire "
              "(systemctl --user restart pipewire), then pick the Flatiron output.")
    for p in written:
        print(f"  {p}")
    return 0


def _simulate_consensus(args):
    from flatiron.sim.consensus import CONFIGS, check_compare, check_corrections, consensus_coverage, headphone_fits

    fits = headphone_fits(args.seed, trials_per_session=max(args.trials // 3, 1))
    cmp = check_compare(fits)
    print(f"Comparison (criterion 1): {'ok' if cmp.ok else 'NOT MET'}; missed {cmp.missed or 'nothing'}, flagged on "
          f"shared features {cmp.on_shared or 'nothing'}, {cmp.false_flags} other false flags")
    for config in CONFIGS:
        print(f"\n{config.label}:")
        for c in check_corrections(fits, config):
            print(f"  {c.name} (criterion 2): {'ok' if c.ok else 'NOT MET'}")
            for fc, gain, h in c.own:
                print(f"    its own {fc:6.0f} Hz {gain:+.1f} dB: corrected {h:+.1f} dB")
            for fc, h in c.shared:
                print(f"    shared  {fc:6.0f} Hz: corrected {h:+.1f} dB")
            print(f"    largest correction elsewhere {c.worst_false_db:.1f} dB")
    print(f"\nConsensus SD, reported / observed (criterion 10): {consensus_coverage(fits):.2f}")


def _simulate_seating(args):
    from flatiron.sim.seating import coverage, moving_resonance, speed, tau_recovery

    for tau in (0.0, 2.0, 3.0):
        f = tau_recovery((tau, tau), args.seed)
        print(f"True spread {tau:g} dB (criterion 9): estimated {f.tau_lo:.2f} below 5 kHz, {f.tau_hi:.2f} above 8 kHz")
    for n in (1, 3, 8):
        cov, rms = coverage((0.5, 1.5), n, args.seed)
        print(f"{n} sessions (criterion 10): 1 SD covers the error {cov:.0%} of the time, RMS error {rms:.2f} dB")
    for label, present, gain in (("two seatings of three", [True, True, False], 9.0),
                                 ("one seating of three", [True, False, False], 9.0),
                                 ("one session, twice the prior's spread", [True], 3.0)):
        r = moving_resonance(present, args.seed, gain_db=gain)
        print(f"{gain:g} dB resonance in {label} (criterion 11): corrected {r.corrected_db:+.1f} dB, "
              f"share {r.scale:.0%}")
    s = speed()
    print(f"Speed (criterion 12): {s['refit_ms']:.0f} ms per answer, block end {s['block_s']:.1f} s, "
          f"full fit {s['full_s']:.1f} s")


def cmd_simulate(args):
    if args.scenario == "consensus":
        return _simulate_consensus(args)
    if args.scenario == "seating":
        return _simulate_seating(args)
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.sim.listener import SimulatedListener, design_example
    from flatiron.sim.run import feature_depth, run, score, EVAL_GRID

    lis = SimulatedListener({e: design_example(e) for e in EARS}, sigma0=args.sigma0,
                            lapse=args.lapse, seed=args.seed)
    cfg = SchedulerConfig(adaptive=args.design == "adaptive")
    sch, fits = run(lis, args.trials, cfg, seed=args.seed + 1)
    for ear in EARS:
        fit, sc = fits[ear], score(lis, ear, fits[ear])
        n_catch = sum(1 for a, b in zip(sch.ears[ear].f_a, sch.ears[ear].f_b) if a == b)
        print(f"ear {ear}: {args.trials} trials ({n_catch} catch, {args.design}), "
              f"{fit.grid.size} grid points, converged={fit.converged}")
        print(f"  sigma0 {fit.sigma0:.2f} (true {args.sigma0}), d0 {fit.d0:.2f} (true 0.5), "
              f"treble noise ×{fit.treble_noise:.2f} (true ×1), "
              f"lapse {fit.lapse:.3f} (true {args.lapse})")
        print(f"  RMS error {sc.rmse:.2f} dB, max {sc.max_err:.2f} dB on a 1/96-oct grid, "
              f"within 2 SD {sc.coverage:.0%}")
        L, sd = fit.at(EVAL_GRID)
        truth = lis.ears[ear].level(EVAL_GRID)
        for feat in lis.ears[ear].features:
            print(f"  {feat.fc:7.0f} Hz  depth vs flanks: true {feature_depth(EVAL_GRID, truth, feat.fc):+6.2f}"
                  f"  fit {feature_depth(EVAL_GRID, L, feat.fc):+6.2f}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="flatiron", description="By-ear headphone resonance EQ.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("devices", help="list output devices and flag problems")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("tone", help="tone generator: glide through frequencies by hand, mark peaks and dips")
    s.add_argument("-p", "--profile", help="apply this profile's correction (Q toggles it) and save marks to it")
    s.add_argument("-d", "--device", type=int)
    s.add_argument("-f", "--freq", type=float, default=1000.0, help="starting frequency (Hz)")
    s.add_argument("-e", "--ear", choices=("L", "R", "both"), default="L")
    s.add_argument("-n", "--dry-run", action="store_true", help="no audio")
    s.set_defaults(func=cmd_tone)

    def session_args(s):
        s.add_argument("-p", "--profile", help="headphone label, e.g. 'HD 600' (default: the only profile)")
        s.add_argument("-d", "--device", type=int)
        s.add_argument("-m", "--minutes", type=float, default=12.0, help="session length, at most 15")
        s.add_argument("--stimulus", choices=KINDS, default="noise")
        s.add_argument("--bands-per-octave", type=int, choices=(12, 24), default=12,
                       help="noise bandwidth: 1/12 or 1/24 octave")
        s.add_argument("--active-eq", metavar="APO_TXT",
                       help="system EQ in effect while measuring (Equalizer APO / AutoEQ text)")
        s.add_argument("--eq-note", help="free-text note on the EQ context")
        s.add_argument("-n", "--dry-run", action="store_true", help="no audio; for trying the UI")

    s = sub.add_parser("run", help="run forced-choice sessions")
    session_args(s)
    s.add_argument("-s", "--sweep", action="store_true",
                   help="start with the coarse sweep, through the current correction if there is one")
    s.add_argument("--sweep-seconds", type=float, help="sweep length each way (default 30)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("verify", help="short session with the current correction applied to every burst")
    session_args(s)
    s.add_argument("--trials", type=int, default=150, help="answers in the session (default 150)")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("audition", help="listen with the current correction on and off")
    s.add_argument("-p", "--profile", help="default: the only profile")
    s.add_argument("-d", "--device", type=int)
    s.add_argument("-n", "--dry-run", action="store_true", help="no audio")
    s.add_argument("--source", action="append", metavar="pink|sweep|FILE.wav",
                   help="what to play (repeatable; default: pink noise and the slow sweep)")
    s.set_defaults(func=cmd_audition)

    s = sub.add_parser("profiles", help="list headphone profiles, add one, or change one's kind or label")
    s.set_defaults(func=cmd_profiles)
    actions = s.add_subparsers(dest="action")
    n = actions.add_parser("new", help="create a profile")
    n.add_argument("name")
    n.add_argument("--kind", choices=("headphone", "iem"), default="headphone",
                   help="IEMs are shown in comparisons but don't count toward a consensus")
    n = actions.add_parser("set", help="change a profile's kind or label (no trials affected)")
    n.add_argument("name")
    n.add_argument("--kind", choices=("headphone", "iem"))
    n.add_argument("--label")

    s = sub.add_parser("sessions", help="list a profile's sessions, or delete one")
    s.add_argument("-p", "--profile", help="default: the only profile")
    s.add_argument("--delete", metavar="SESSION_ID", help="delete a session (e.g. one left open by a crash)")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("config", help="show or change a profile's fit settings; checkpoint its filters")
    s.add_argument("-p", "--profile", help="default: the only profile")
    for flags, f, kw, *_ in FIT_OPTIONS:
        s.add_argument(*flags, dest=f, default=None, **kw)
    s.add_argument("--sessions", help="comma-separated session ids to fit, or 'all' (the default)")
    s.add_argument("--pool", metavar="PROFILE[,PROFILE]|none",
                   help="include other profiles' trials in this one's fits (the same headphones measured "
                        "with another stimulus, say); none: stop")
    s.add_argument("--reset", nargs="*", metavar="SETTING",
                   help="back to the default: the named settings (e.g. width broad-strength), or all of them")
    s.add_argument("--checkpoint", action="store_true",
                   help="keep the current filters fixed from now on; later fits only add filters "
                        "(e.g. before a verify session)")
    s.add_argument("--reset-checkpoint", action="store_true", help="drop the checkpoint")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("report", help="the current fit: filters and features (and an HTML report with -H)")
    s.add_argument("-p", "--profile", help="default: the only profile")
    s.add_argument("-H", "--html", action="store_true", help="also open the HTML report (figures) in a browser")
    s.add_argument("--csv", metavar="FILE", help="dump the level, reference, baseline and residual with SDs, per ear")
    s.add_argument("--compare", nargs="+", metavar="PROFILE",
                   help="compare profiles per ear: their curves, soft median, and where one stands out")
    s.add_argument("-w", "--width", type=float, help="--compare: max feature width W in octaves (default 1)")
    s.add_argument("-r", "--reference", help="--compare: reference (default: the one the profiles share)")
    s.add_argument("--ear", choices=("L", "R", "both"), default="both", help="--compare: which ear")
    s.add_argument("--include-kind", action="append", choices=("iem",),
                   help="--compare: let this kind count toward the consensus (it's drawn dashed either way)")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("export", help="write the current fit's correction for an EQ host")
    s.add_argument("-p", "--profile", help="default: the only profile")
    s.add_argument("-t", "--target", choices=("apo", "lsp", "easyeffects", "pipewire", "tincan"), required=True,
                   help="apo: Equalizer APO / AutoEQ text per channel (also PipeWire param_eq files); "
                        "lsp: LSP Parametric Equalizer x32 LeftRight settings; "
                        "easyeffects: EasyEffects preset, the format Equaliser (macOS) imports; "
                        "pipewire: filter-chain drop-in for ~/.config/pipewire/pipewire.conf.d/; "
                        "tincan: TinCan preset, saved straight into TinCan's presets")
    s.add_argument("-m", "--merge", metavar="APO_TXT", help="preference preset to merge in front, both channels")
    s.add_argument("--preamp", metavar="safe|DB",
                   help="safe (default): enough headroom that no boost can clip; or a gain in dB, "
                        "e.g. 0 to keep the level of the uncorrected sound for A/B comparison "
                        "(not for tincan, which sets its own)")
    s.add_argument("--check", action="store_true",
                   help="pipewire: measure the filters through a private filter-chain first "
                        "(virtual sink/source, a few seconds; nothing plays on your devices)")
    s.add_argument("--merge-left", metavar="APO_TXT")
    s.add_argument("--merge-right", metavar="APO_TXT")
    s.add_argument("-o", "--output", metavar="FILE",
                   help="output file (apo: FILE_L.txt and FILE_R.txt); default: <profile>-<fit>.cfg/.txt "
                        "in the current directory; tincan: TinCan's preset folder")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("simulate", help="fit a simulated listener and report recovery")
    s.add_argument("--trials", type=int, default=600, help="per ear")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--sigma0", type=float, default=1.0)
    s.add_argument("--lapse", type=float, default=0.03)
    s.add_argument("--design", choices=("adaptive", "uniform"), default="adaptive")
    s.add_argument("--scenario", choices=("consensus", "seating"),
                   help="consensus: three headphones on one listener; seating: sessions that differ")
    s.set_defaults(func=cmd_simulate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
