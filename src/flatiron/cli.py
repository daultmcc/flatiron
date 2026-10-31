"""Command-line entry point."""

import argparse
import sys

import numpy as np

from flatiron import EARS, FS
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


def cmd_level(args):
    from flatiron.audio import player
    from flatiron.audio.limiter import limit, monaural, db_to_amp

    x = make_stimulus(args.freq, StimulusSpec(duration=0.4))
    gap = np.zeros(int(0.1 * FS))
    burst = limit(np.concatenate([x * db_to_amp(args.level), gap])).signal
    buf = monaural(burst, "L") + monaural(burst, "R")
    print(f"Looping a {args.freq:g} Hz band at {args.level:g} dBFS in both ears.")
    print("Raise the system volume to a comfortable, moderate level, then press Enter.")
    print("Keep the volume fixed for every session from now on.")
    player.play_loop(buf, device=args.device)
    try:
        input()
    finally:
        player.stop()
    if args.profile:
        from flatiron.session.store import Profile, now_iso

        note = input("Note the volume setting for your records (e.g. 'PipeWire 62%'), or Enter to skip: ")
        Profile.open(args.profile).update(level={"dbfs": args.level, "freq": args.freq,
                                                 "volume_note": note.strip(), "set": now_iso()})
        print(f"Saved to profile {args.profile!r}. Keep the volume fixed for every session.")


PROBE_HELP = """commands:
  <number>     set frequency in Hz (e.g. 6150)
  + / -        step up / down 1/24 octave (++ / -- for 1/6 octave)
  l / r        switch ear
  t <kind>     stimulus: noise, warble, tone
  v <dB>       level in dBFS RMS (e.g. v -36)
  Enter        replay
  q            quit"""


def cmd_probe(args):
    from flatiron.audio import player
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
            player.play(monaural(res.signal, ear), device=args.device)
        except ValueError as e:
            print(f"? {e}")
        try:
            cmd = input("> ").strip().lower()
        except EOFError:
            return


def _settings(args, profile):
    from flatiron.apo import parse_apo
    from flatiron.session.runner import RunSettings

    level = args.level
    if level is None:
        stored = profile.read().get("level")
        level = stored["dbfs"] if stored else -40.0
    eq, text, note = None, None, args.eq_note or "none"
    if args.active_eq:
        text = open(args.active_eq).read()
        eq = parse_apo(text)
        note = args.eq_note or f"APO file {args.active_eq}"
    spec = StimulusSpec(kind=args.stimulus, bandwidth_oct=1 / args.bands_per_octave)
    return RunSettings(stimulus=spec, level_dbfs=level, session_minutes=args.minutes,
                       active_eq=eq, eq_note=note, eq_text=text)


def _launch(args, sweep: bool):
    from flatiron.audio.player import NullPlayer, SoundDevicePlayer
    from flatiron.session.runner import TrialRunner
    from flatiron.session.store import Profile
    from flatiron.ui.app import FlatironApp

    profile = Profile.open(args.profile)
    if profile.read().get("level") is None and not args.dry_run:
        print(f"No level set for {args.profile!r} yet. Run: flatiron level --profile {args.profile!r}")
        return 1
    settings = _settings(args, profile)
    device = {"index": args.device, "dry_run": args.dry_run}
    if not args.dry_run:
        from flatiron.audio.devices import list_output_devices

        devs = {d.index: d for d in list_output_devices()}
        d = devs.get(args.device) or next((d for d in devs.values() if d.is_default), None)
        if d:
            device.update(name=d.name, hostapi=d.hostapi, warnings=d.warnings)
            for w in d.warnings:
                print(f"! {d.name}: {w}")
    player = NullPlayer() if args.dry_run else SoundDevicePlayer(args.device)
    app = FlatironApp(lambda: TrialRunner(profile, settings, device=device), player, sweep=sweep)
    app.run()
    return 0


def cmd_run(args):
    return _launch(args, sweep=args.sweep)


def cmd_sessions(args):
    from flatiron.session.store import Profile

    prof = Profile.open(args.profile)
    sessions = prof.sessions()
    if not sessions:
        print(f"No sessions for {args.profile!r} yet.")
        return
    for s in sessions:
        n = {e: sum(t.ear == e for t in s.trials) for e in EARS}
        catch = [t for t in s.trials if t.catch]
        acc = f"{np.mean([(t.offset > 0) == t.a_louder for t in catch]):.0%}" if catch else "-"
        fast = sum(t.fast for t in s.trials)
        mins = s.trials[-1].t / 60 if s.trials else 0
        print(f"{s.id}  L {n['L']:4d}  R {n['R']:4d}  {mins:4.1f} min  catch {acc:>4}  "
              f"fast {fast:3d}  EQ: {s.eq_context.get('note')}{'' if s.ended else '  (not closed)'}")
    trials, _ = prof.history(sessions)
    print(f"total: L {len(trials['L'])}, R {len(trials['R'])}")


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

    s = sub.add_parser("level", help="loop a reference band while setting system volume")
    s.add_argument("--device", type=int)
    s.add_argument("--freq", type=float, default=4000.0)
    s.add_argument("--level", type=float, default=-40.0, help="dBFS RMS")
    s.add_argument("--profile", help="save the level to this headphone profile")
    s.set_defaults(func=cmd_level)

    s = sub.add_parser("probe", help="manual probe: play a band at a chosen frequency and ear")
    s.add_argument("--device", type=int)
    s.add_argument("--freq", type=float, default=4000.0)
    s.add_argument("--ear", choices=EARS, default="L")
    s.add_argument("--type", choices=KINDS, default="noise")
    s.add_argument("--level", type=float, default=-40.0, help="dBFS RMS")
    s.add_argument("--duration", type=float, default=0.4)
    s.set_defaults(func=cmd_probe)

    def session_args(s):
        s.add_argument("--profile", required=True, help="headphone label, e.g. 'HD 600'")
        s.add_argument("--device", type=int)
        s.add_argument("--level", type=float, help="dBFS RMS (default: the profile's level)")
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

    s = sub.add_parser("sessions", help="list a profile's sessions")
    s.add_argument("--profile", required=True)
    s.set_defaults(func=cmd_sessions)

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
