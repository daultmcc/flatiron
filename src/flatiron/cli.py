"""Command-line entry point."""

import argparse
import sys

import numpy as np

from flatiron import EARS, FS
from flatiron.audio.limiter import DEFAULT_LEVEL_DB
from flatiron.audio.stimulus import KINDS, StimulusSpec, make_stimulus

STEP_OCT = 1 / 24


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


def _launch(args, sweep: bool):
    from flatiron.session.runner import TrialRunner
    from flatiron.session.store import Profile
    from flatiron.ui.app import FlatironApp

    profile = Profile.open(args.profile)
    _settings(args, profile)  # validate options before the UI starts
    device = {"index": args.device, "dry_run": args.dry_run}
    if not args.dry_run:
        from flatiron.audio.devices import list_output_devices

        devs = {d.index: d for d in list_output_devices()}
        d = devs.get(args.device) or next((d for d in devs.values() if d.is_default), None)
        if d:
            device.update(name=d.name, hostapi=d.hostapi, warnings=d.warnings)
            for w in d.warnings:
                print(f"! {d.name}: {w}")
    app = FlatironApp(lambda: TrialRunner(profile, _settings(args, profile), device=device),
                      _player(args), sweep=sweep)
    app.run()
    print(VOLUME_REMINDER)
    return 0


def cmd_run(args):
    return _launch(args, sweep=args.sweep)


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
    from flatiron.session.store import Profile

    prof = Profile.existing(args.profile)
    if prof is None:
        print(f"No profile {args.profile!r}. See: flatiron profiles")
        return 1
    sessions = prof.sessions()
    if args.delete:
        match = [s for s in sessions if s.id == args.delete]
        if not match:
            print(f"No session {args.delete!r} in {args.profile!r}.")
            return 1
        s = match[0]
        if _confirm(f"Delete session {s.id} ({len(s.trials)} trials)? This can't be undone.", args.yes):
            prof.delete(s)
            print("Deleted.")
        else:
            print("Not deleted.")
        return 0
    if not sessions:
        print(f"No sessions for {args.profile!r} yet.")
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
              f"{'' if s.ended else '  (not closed)'}")
    trials, _ = prof.history(sessions)
    print(f"total: L {len(trials['L'])}, R {len(trials['R'])}")


def cmd_fit(args):
    from flatiron.fit.pipeline import CorrectionSettings
    from flatiron.fit.run import fit_profile
    from flatiron.session.store import Profile

    prof = Profile.existing(args.profile)
    if prof is None:
        print(f"No profile {args.profile!r}. See: flatiron profiles")
        return 1
    try:
        settings = CorrectionSettings(
            max_width_oct=args.width, reference=args.reference, phon=args.phon, k=args.k,
            k_broad=args.k_broad, broad_strength=args.strength / 100, bands=args.bands,
            max_boost_narrow_db=args.boost_narrow, max_boost_broad_db=args.boost_broad,
            max_cut_db=args.max_cut, f_lo=args.f_lo, f_hi=args.f_hi, consistency=not args.no_consistency)
        print("Fitting (perceptual fits are cached, so refits with other settings are quick)...")
        pf = fit_profile(prof, settings, args.sessions.split(",") if args.sessions else None)
    except (ValueError, FileNotFoundError) as e:
        print(e)
        return 1
    corr = pf.correction
    for e, c in corr.ears.items():
        f = pf.fits[e]
        print(f"\n{e} ear: {f.n_trials} trials, noise {f.sigma0:.2f} dB, lapse {f.lapse:.1%}, "
              f"time-order bias {f.bias_db:+.2f} ± {f.bias_sd:.2f} dB (+ = first interval heard louder)")
        for flt in c.filters:
            print(f"  PK  Fc {flt.fc:7.0f} Hz  Gain {flt.gain_db:+5.1f} dB  Q {flt.q:5.2f}")
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
    if not args.no_report:
        from flatiron.report.html import write_report

        print(f"report: {write_report(prof, pf)}")
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
    s.add_argument("--device", type=int)
    s.add_argument("--freq", type=float, default=4000.0)
    s.add_argument("--ear", choices=EARS, default="L")
    s.add_argument("--type", choices=KINDS, default="noise")
    s.add_argument("--level", type=float, default=DEFAULT_LEVEL_DB, help="dBFS RMS")
    s.add_argument("--duration", type=float, default=0.4)
    s.set_defaults(func=cmd_probe)

    def session_args(s):
        s.add_argument("--profile", required=True, help="headphone label, e.g. 'HD 600'")
        s.add_argument("--device", type=int)
        s.add_argument("--minutes", type=float, default=12.0, help="session length, at most 15")
        s.add_argument("--stimulus", choices=KINDS, default="noise")
        s.add_argument("--bands-per-octave", type=int, choices=(12, 24), default=12,
                       help="noise bandwidth: 1/12 or 1/24 octave")
        s.add_argument("--active-eq", metavar="APO_TXT",
                       help="system EQ in effect while measuring (Equalizer APO / AutoEQ text)")
        s.add_argument("--eq-note", help="free-text note on the EQ context")
        s.add_argument("--dry-run", action="store_true", help="no audio; for trying the UI")

    s = sub.add_parser("run", help="run forced-choice sessions")
    session_args(s)
    s.add_argument("--sweep", action="store_true", help="start with the coarse sweep (phase 0)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("sweep", help="coarse sweep (phase 0), then trials")
    session_args(s)
    s.set_defaults(func=lambda a: _launch(a, sweep=True))

    s = sub.add_parser("profiles", help="list headphone profiles")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("sessions", help="list a profile's sessions, or delete one")
    s.add_argument("--profile", required=True)
    s.add_argument("--delete", metavar="SESSION_ID", help="delete a session (e.g. one left open by a crash)")
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("fit", help="fit correction filters from a profile's sessions")
    s.add_argument("--profile", required=True)
    s.add_argument("--width", type=float, default=1.0, help="max feature width W in octaves (1/3 to 2)")
    s.add_argument("--reference", default="iso226", help="iso226, diffuse, none, or a CSV of freq_hz,db")
    s.add_argument("--phon", type=float, default=60.0, help="listening level assumed by the reference")
    s.add_argument("--k", type=float, default=2.0, help="significance for narrow features (SDs)")
    s.add_argument("--k-broad", type=float, default=2.5, help="significance for broad features (SDs)")
    s.add_argument("--strength", type=float, default=100, help="broad-feature strength, 0 to 100%%")
    s.add_argument("--bands", type=int, default=10, help="correction bands per channel")
    s.add_argument("--boost-narrow", type=float, default=4.0, help="max boost for narrow features (dB)")
    s.add_argument("--boost-broad", type=float, default=6.0, help="max boost for broad features (dB)")
    s.add_argument("--max-cut", type=float, default=12.0, help="max cut (dB)")
    s.add_argument("--f-lo", type=float, default=1000.0, help="lower correction limit (Hz)")
    s.add_argument("--f-hi", type=float, default=13000.0, help="upper correction limit (Hz)")
    s.add_argument("--sessions", help="comma-separated session ids to use (default: all)")
    s.add_argument("--no-consistency", action="store_true", help="skip the reseat consistency check")
    s.add_argument("--no-report", action="store_true")
    s.set_defaults(func=cmd_fit)

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
