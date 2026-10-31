"""Command-line entry point."""

import argparse
import sys

import numpy as np

from flatiron import EARS
from flatiron.audio.limiter import DEFAULT_LEVEL_DB
from flatiron.audio.stimulus import KINDS, StimulusSpec, make_stimulus

STEP_OCT = 1 / 24


def _profile(args, create: bool = False):
    """The profile named by -p, or the only one there is. None (after
    printing why) if that doesn't identify one."""
    from flatiron.session.store import Profile

    if args.profile:
        prof = Profile.open(args.profile) if create else Profile.existing(args.profile)
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


PROBE_HELP = """commands:
  <number>     set frequency in Hz (e.g. 6150)
  + / -        step up / down 1/24 octave (++ / -- for 1/6 octave)
  l / r        switch ear
  t <kind>     stimulus: noise, warble, tone
  v <dB>       level in dBFS RMS (e.g. v -36)
  Enter        replay
  q            quit"""


def cmd_probe(args):
    from flatiron.audio.player import StreamPlayer

    with StreamPlayer(args.device) as player:
        _probe_loop(args, player)


def _probe_loop(args, player):
    from flatiron.audio.limiter import limit, monaural, db_to_amp

    f, ear, kind, level = args.freq, args.ear, args.type, args.level
    print(PROBE_HELP)
    cmd = ""
    while True:
        if cmd == "q":
            return
        try:
            if cmd in ("+", "-", "++", "--"):
                f *= 2 ** ((1 if cmd[0] == "+" else -1) * STEP_OCT * (4 if len(cmd) == 2 else 1))
            elif cmd in ("l", "r"):
                ear = cmd.upper()
            elif cmd.startswith("t "):
                kind = cmd[2:].strip()
                StimulusSpec(kind=kind)
            elif cmd.startswith("v "):
                level = float(cmd[2:])
            elif cmd:
                f = float(cmd)
            f = float(np.clip(f, 100, 20_000))
            x = make_stimulus(f, StimulusSpec(kind=kind, duration=args.duration))
            res = limit(x * db_to_amp(level))
            note = f"  (limited by {-res.gain_db:.1f} dB)" if res.gain_db else ""
            print(f"{ear}  {f:8.1f} Hz  {kind}  {level:g} dBFS{note}")
            player.start(monaural(res.signal, ear))
            player.wait()
        except ValueError as e:
            print(f"? {e}")
        try:
            cmd = input("> ").strip().lower()
        except EOFError:
            return


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
    from flatiron.ui.app import FlatironApp

    profile = profile or _profile(args, create=True)
    if profile is None:
        return 1
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
    app = FlatironApp(lambda: TrialRunner(profile, settings(), device=device), _player(args), sweep=sweep)
    app.run()
    print(VOLUME_REMINDER)
    return 0


def cmd_run(args):
    return _launch(args, sweep=args.sweep)


def cmd_verify(args):
    from flatiron.audio.preview import PreviewEQ
    from flatiron.fit.run import leftovers

    prof = _profile(args)
    if prof is None:
        return 1
    fit = prof.read().get("fit")
    if not fit:
        print(f"No fit to verify yet. Run: flatiron fit -p {prof.read()['label']!r}")
        return 1
    eq = PreviewEQ.from_fit(fit)
    print(f"Verifying fit {eq.fit_hash}: Flatiron applies the correction to every burst itself.")
    print("Make sure it isn't also applied by the system (don't play through a Flatiron PipeWire sink or an")
    print("LSP instance with this correction), or it will be applied twice.")
    rc = _launch(args, sweep=False, profile=prof, preview=eq, max_trials=args.trials)
    if rc or not any(s.preview for s in prof.sessions()):
        return rc
    print("\nChecking what's left with the correction applied (all trials, verify included)...")
    pf, left = leftovers(prof)
    for e, feats in left.items():
        who = f"{e} ({pf.fits[e].n_trials} trials)"
        if not feats:
            print(f"  {who}: nothing significant left. The correction holds.")
            continue
        print(f"  {who}: still significant: " + ", ".join(
            f"{x.fc:.0f} Hz ({-x.peak_db:+.1f} dB, {x.kind})" for x in sorted(feats, key=lambda x: x.fc)))
    if any(left.values()):
        print("Add filters for these, keeping the current ones: flatiron fit --increment")
    return 0


def cmd_audition(args):
    from flatiron.audio.preview import PreviewEQ
    from flatiron.ui.audition import AuditionApp, load_source

    prof = _profile(args)
    if prof is None:
        return 1
    fit = prof.read().get("fit")
    if not fit:
        print(f"No fit to audition yet. Run: flatiron fit -p {prof.read()['label']!r}")
        return 1
    try:
        sources = [load_source(x) for x in (args.source or ["pink", "sweep"])]
    except (FileNotFoundError, ValueError) as e:
        print(e)
        return 1
    eq = PreviewEQ.from_fit(fit)
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
        print(f"{info['label']}  ({p.slug})")
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


def cmd_fit(args):
    from flatiron.fit.pipeline import CorrectionSettings
    from flatiron.fit.run import fit_profile

    prof = _profile(args)
    if prof is None:
        return 1
    try:
        settings = CorrectionSettings(
            max_width_oct=args.width, reference=args.reference, phon=args.phon, k=args.k,
            k_broad=args.k_broad, broad_strength=args.strength / 100, bands=args.bands,
            max_boost_narrow_db=args.boost_narrow, max_boost_broad_db=args.boost_broad,
            max_cut_db=args.max_cut, f_lo=args.f_lo, f_hi=args.f_hi, consistency=not args.no_consistency,
            preserve_tonality=args.preserve_tonality)
        fixed = None
        if args.increment:
            from flatiron.fit.run import saved_filters

            fixed = saved_filters(prof)
            if fixed is None:
                print("--increment needs a saved fit to add to.")
                return 1
        print("Fitting (perceptual fits are cached, so refits with other settings are quick)...")
        pf = fit_profile(prof, settings, args.sessions.split(",") if args.sessions else None, fixed=fixed)
    except (ValueError, FileNotFoundError) as e:
        print(e)
        return 1
    corr = pf.correction
    for e, c in corr.ears.items():
        f = pf.fits[e]
        print(f"\n{e} ear: {f.n_trials} trials, noise {f.sigma0:.2f} dB, lapse {f.lapse:.1%}, "
              f"time-order bias {f.bias_db:+.2f} ± {f.bias_sd:.2f} dB (+ = first interval heard louder)")
        comp = {id(x) for x in c.compensation}
        kept = {id(x) for x in c.fixed}
        for flt in c.filters:
            note = ("  (kept)" if id(flt) in kept else
                    "  (tonality compensation)" if id(flt) in comp else "")
            print(f"  PK  Fc {flt.fc:7.0f} Hz  Gain {flt.gain_db:+5.1f} dB  Q {flt.q:5.2f}{note}")
        if not c.filters:
            print("  no correction needed")
        for kind in ("narrow", "broad"):
            acc = [x for x in c.features if x.accepted and x.kind == kind]
            rej = [x for x in c.features if not x.accepted and x.reason.startswith("inconsistent")]
            if acc:
                print(f"  {kind} features: " + ", ".join(f"{x.fc:.0f} Hz ({-x.peak_db:+.1f} dB)" for x in acc))
            if rej and kind == "narrow":
                print("  dropped as inconsistent across reseats: " + ", ".join(f"{x.fc:.0f} Hz" for x in rej))
        for a, b, m in c.left_out:
            print(f"  left out (band budget): {a:.0f}-{b:.0f} Hz, up to {m:.1f} dB")
    print(f"\npreamp {corr.preamp_db:+.1f} dB (both channels)")
    if settings.reference == "iso226":
        print("reference: ISO 226 free field (the diffuse-field table isn't sourced yet)")
    if args.open or not args.no_report:
        import webbrowser

        from flatiron.report.html import write_report

        path = write_report(prof, pf)
        print(f"report: {path}")
        if args.open:
            webbrowser.open(path.as_uri())
    return 0


def cmd_export(args):
    from pathlib import Path

    from flatiron.apo import parse_apo
    from flatiron.export import apo, easyeffects, lsp, pipewire
    from flatiron.export.plan import build_plan, response_db

    prof = _profile(args)
    if prof is None:
        return 1
    fit = prof.read().get("fit")
    if not fit:
        print(f"No fit yet. Run: flatiron fit -p {prof.read()['label']!r}")
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
    plan = build_plan(fit, pref)
    if args.preamp != "safe":
        try:
            plan.preamp_db = float(args.preamp)
        except ValueError:
            print(f"--preamp takes 'safe' or a gain in dB, not {args.preamp!r}")
            return 1
        peak = max(float(response_db(fs).max()) for fs in plan.channels.values()) + plan.preamp_db
        if peak > 0:
            print(f"Note: with this preamp the EQ boosts up to {peak:+.1f} dB at its peak, so loud "
                  "material in that band can clip.")
    ext = {"apo": ".txt", "lsp": ".cfg", "easyeffects": ".json", "pipewire": ".conf"}[args.target]
    out = Path(args.output) if args.output else Path(
        f"{prof.slug}-{plan.fit_hash}{'-merged' if plan.merged else ''}{ext}")
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
            out.write_text(pipewire.write(plan, prof.read()["label"], prof.slug))
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
    print(f"{'Merged' if plan.merged else 'Correction-only'} export, bands {bands}, preamp {plan.preamp_db:+.2f} dB")
    if not plan.merged:
        print("Stack it after any EQ you already use (or in its own instance).")
    if args.target == "pipewire":
        print("Install: copy it to ~/.config/pipewire/pipewire.conf.d/ and restart PipeWire "
              "(systemctl --user restart pipewire), then pick the Flatiron output.")
    for p in written:
        print(f"  {p}")
    return 0


def cmd_simulate(args):
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

    s = sub.add_parser("probe", help="manual probe: play a band at a chosen frequency and ear")
    s.add_argument("-d", "--device", type=int)
    s.add_argument("-f", "--freq", type=float, default=4000.0)
    s.add_argument("-e", "--ear", choices=EARS, default="L")
    s.add_argument("--type", choices=KINDS, default="noise")
    s.add_argument("--level", type=float, default=DEFAULT_LEVEL_DB, help="dBFS RMS")
    s.add_argument("--duration", type=float, default=0.4)
    s.set_defaults(func=cmd_probe)

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
    s.add_argument("-s", "--sweep", action="store_true", help="start with the coarse sweep (phase 0)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("sweep", help="coarse sweep (phase 0), then trials")
    session_args(s)
    s.set_defaults(func=lambda a: _launch(a, sweep=True))

    s = sub.add_parser("verify", help="short session with the saved correction applied to every burst")
    session_args(s)
    s.add_argument("--trials", type=int, default=150, help="answers in the session (default 150)")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("audition", help="listen with the saved correction on and off")
    s.add_argument("-p", "--profile", help="default: the only profile")
    s.add_argument("-d", "--device", type=int)
    s.add_argument("-n", "--dry-run", action="store_true", help="no audio")
    s.add_argument("--source", action="append", metavar="pink|sweep|FILE.wav",
                   help="what to play (repeatable; default: pink noise and the slow sweep)")
    s.set_defaults(func=cmd_audition)

    s = sub.add_parser("profiles", help="list headphone profiles")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("sessions", help="list a profile's sessions, or delete one")
    s.add_argument("-p", "--profile", help="default: the only profile")
    s.add_argument("--delete", metavar="SESSION_ID", help="delete a session (e.g. one left open by a crash)")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("fit", help="fit correction filters from a profile's sessions")
    s.add_argument("-p", "--profile", help="default: the only profile")
    s.add_argument("--open", action="store_true", help="open the report in a browser")
    s.add_argument("-w", "--width", type=float, default=1.0, help="max feature width W in octaves (1/3 to 2)")
    s.add_argument("-r", "--reference", default="iso226", help="iso226, diffuse, none, or a CSV of freq_hz,db")
    s.add_argument("--phon", type=float, default=60.0, help="listening level assumed by the reference")
    s.add_argument("-k", "--k", type=float, default=2.0, help="significance for narrow features (SDs)")
    s.add_argument("--k-broad", type=float, default=2.5, help="significance for broad features (SDs)")
    s.add_argument("-s", "--strength", type=float, default=100, help="broad-feature strength, 0 to 100%%")
    s.add_argument("-t", "--preserve-tonality", action="store_true",
                   help="correct narrow features only, and keep the original broad shape "
                        "(adds broad compensation; broad tonal changes are left to you)")
    s.add_argument("-b", "--bands", type=int, default=20, help="correction bands per channel (max 32)")
    s.add_argument("--boost-narrow", type=float, default=4.0, help="max boost for narrow features (dB)")
    s.add_argument("--boost-broad", type=float, default=6.0, help="max boost for broad features (dB)")
    s.add_argument("--max-cut", type=float, default=12.0, help="max cut (dB)")
    s.add_argument("--f-lo", type=float, default=1000.0, help="lower correction limit (Hz)")
    s.add_argument("--f-hi", type=float, default=13000.0, help="upper correction limit (Hz)")
    s.add_argument("--sessions", help="comma-separated session ids to use (default: all)")
    s.add_argument("--no-consistency", action="store_true", help="skip the reseat consistency check")
    s.add_argument("--no-report", action="store_true", help="don't write report.html")
    s.add_argument("--increment", action="store_true",
                   help="keep the saved fit's filters and add filters only for what's still significant "
                        "with them applied (e.g. after a verify session)")
    s.set_defaults(func=cmd_fit)

    s = sub.add_parser("export", help="write the last fit's correction for an EQ host")
    s.add_argument("-p", "--profile", help="default: the only profile")
    s.add_argument("-t", "--target", choices=("apo", "lsp", "easyeffects", "pipewire"), required=True,
                   help="apo: Equalizer APO / AutoEQ text per channel (also PipeWire param_eq files); "
                        "lsp: LSP Parametric Equalizer x32 LeftRight settings; "
                        "easyeffects: EasyEffects preset, the format Equaliser (macOS) imports; "
                        "pipewire: filter-chain drop-in for ~/.config/pipewire/pipewire.conf.d/")
    s.add_argument("-m", "--merge", metavar="APO_TXT", help="preference preset to merge in front, both channels")
    s.add_argument("--preamp", default="safe", metavar="safe|DB",
                   help="safe (default): enough headroom that no boost can clip; or a gain in dB, "
                        "e.g. 0 to keep the level of the uncorrected sound for A/B comparison")
    s.add_argument("--check", action="store_true",
                   help="pipewire: measure the filters through a private filter-chain first "
                        "(virtual sink/source, a few seconds; nothing plays on your devices)")
    s.add_argument("--merge-left", metavar="APO_TXT")
    s.add_argument("--merge-right", metavar="APO_TXT")
    s.add_argument("-o", "--output", metavar="FILE",
                   help="output file (apo: FILE_L.txt and FILE_R.txt); default: <profile>-<fit>.cfg/.txt "
                        "in the current directory")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("simulate", help="fit a simulated listener and report recovery")
    s.add_argument("--trials", type=int, default=600, help="per ear")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--sigma0", type=float, default=1.0)
    s.add_argument("--lapse", type=float, default=0.03)
    s.add_argument("--design", choices=("adaptive", "uniform"), default="adaptive")
    s.set_defaults(func=cmd_simulate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
