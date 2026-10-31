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
    s.set_defaults(func=cmd_level)

    s = sub.add_parser("probe", help="manual probe: play a band at a chosen frequency and ear")
    s.add_argument("--device", type=int)
    s.add_argument("--freq", type=float, default=4000.0)
    s.add_argument("--ear", choices=EARS, default="L")
    s.add_argument("--type", choices=KINDS, default="noise")
    s.add_argument("--level", type=float, default=-40.0, help="dBFS RMS")
    s.add_argument("--duration", type=float, default=0.4)
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("simulate", help="fit a simulated listener and report recovery")
    s.add_argument("--trials", type=int, default=600, help="per ear")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--sigma0", type=float, default=1.0)
    s.add_argument("--lapse", type=float, default=0.03)
    s.add_argument("--design", choices=("adaptive", "uniform"), default="adaptive")
    s.set_defaults(func=cmd_simulate)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
