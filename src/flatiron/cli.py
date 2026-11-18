"""Command-line entry point."""

import argparse
import os
import sys

import numpy as np

from flatiron import EARS
from flatiron.audio.biquad import PeakingFilter
from flatiron.audio.stimulus import KINDS, StimulusSpec


def _profile(args, create: bool = False):
    """The profile named by the PROFILE argument (LISTENER/PROFILE, or a bare
    name for the current listener's), or the listener's only one. create
    (run --new): make a new one, which must be named and mustn't exist yet.
    None (after printing why) if that doesn't identify one."""
    from flatiron.session.store import Profile, current_listener, split_name

    name = args.profile or ""
    if create and not name.split("/")[-1]:
        print("--new needs a name for the new profile: flatiron run 'Headphone name' --new")
        return None
    try:
        listener, pname = split_name(name) if name else (current_listener(), "")
        if pname:
            prof = Profile.existing(pname, listener.root)
            if create and prof is not None:
                print(f"There's already a profile {name!r}: leave out --new to add sessions to it.")
                return None
            if create:
                prof = Profile.open(pname, listener.root)
        else:
            prof = None
    except ValueError as e:
        print(e)
        return None
    if pname:
        if prof is None:
            hint = (f" Start it with: flatiron run {name!r} --new" if args.cmd == "run"
                    else " See: flatiron profiles")
            print(f"No profile {name!r}.{hint}")
        return prof
    profiles = Profile.all(listener.root)
    if len(profiles) == 1:
        return profiles[0]
    if not profiles:
        print("No profiles yet. Start one with: flatiron run 'Headphone name' --new")
    else:
        print(f"Several profiles; name one (flatiron {args.cmd} NAME): "
              + ", ".join(p.read()["label"] for p in profiles))
    return None


def _device(text: str | None) -> int | None:
    """An output device by index or by part of its name (see `flatiron devices`)."""
    if text is None or text.strip().isdigit():
        return None if text is None else int(text)
    from flatiron.audio.devices import list_output_devices

    devs = list_output_devices()
    want = text.strip().lower()
    hits = [d for d in devs if d.name.lower() == want] or [d for d in devs if want in d.name.lower()]
    if len(hits) == 1:
        return hits[0].index
    if not hits:
        raise ValueError(f"No output device matches {text!r}. See: flatiron devices")
    raise ValueError(f"{text!r} matches several outputs ({', '.join(f'{d.index}: {d.name}' for d in hits)}); "
                     "give more of the name, or its index")


def _stimulus(text: str) -> StimulusSpec:
    """noise[:N] (1/N-octave band, default 12), warble[:N] (a 1/N-octave swing, default 12) or tone."""
    kind, _, n = text.strip().partition(":")
    if kind not in KINDS:
        raise argparse.ArgumentTypeError(f"unknown stimulus {kind!r}: noise[:N], warble[:N] or tone")
    if kind == "tone":
        if n:
            raise argparse.ArgumentTypeError("tone takes no bandwidth")
        return StimulusSpec(kind="tone")
    try:
        per_octave = int(n) if n else 12
    except ValueError:
        raise argparse.ArgumentTypeError(f"{kind}:N takes a whole number of bands per octave, not {n!r}") from None
    if per_octave < 1:
        raise argparse.ArgumentTypeError(f"{kind}:N needs N of at least 1")
    if kind == "noise":
        return StimulusSpec(kind="noise", bandwidth_oct=1 / per_octave)
    return StimulusSpec(kind="warble", warble_depth_oct=1 / (2 * per_octave))


def _auto_float(text: str):
    if text.strip().lower() == "auto":
        return "auto"
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number or auto: {text!r}") from None


def _consensus_speakers(st, root) -> list[str]:
    """The speakers listed in a consensus reference."""
    from flatiron.fit.consensus import KINDS_GATING
    from flatiron.session.store import Profile

    return [s for s in st.consensus if Profile(s, root).path.exists() and Profile(s, root).kind in KINDS_GATING]


def _hz(text: str) -> float:
    """A frequency in Hz, or in kHz with a k: 13000, 13k, 12.5k."""
    t = text.strip().lower().removesuffix("hz")
    try:
        return float(t[:-1]) * 1000 if t.endswith("k") else float(t)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a frequency: {text!r} (e.g. 13000 or 13k)") from None


def cmd_devices(args):
    from flatiron.audio.devices import list_output_devices

    for d in list_output_devices():
        mark = "*" if d.is_default else " "
        print(f"{mark}{d.index:3d}  {d.name}  [{d.hostapi}, {d.channels} ch, {d.default_samplerate:g} Hz]")
        for w in d.warnings:
            print(f"        ! {w}")
    print("\n* = default. Pass --device <index> to other commands; a wired output at 48 kHz is recommended.")


DOUBLE_EQ = "Turn off any system EQ with this correction (PipeWire sink, LSP), or it's applied twice."


def _player(args):
    from flatiron.audio.player import NullPlayer, StreamPlayer

    return NullPlayer() if getattr(args, "dry_run", False) else StreamPlayer(args.device)


def _current(prof):
    """The profile's fit as of now (computed on demand), or None after
    printing why there isn't one."""
    from flatiron.fit.run import NotEnoughTrials, current_fit

    announce = [lambda: print("Fitting the new trials (a few seconds)...", flush=True)]

    def on_fit():  # once, however many profiles (consensus members, voters) need refitting
        if announce:
            announce.pop()()

    try:
        return current_fit(prof, on_fit=on_fit)
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
        if prof is None or not _not_speaker(prof, "apply"):
            return 1
        pf = _current(prof)
        if pf is not None:
            eq = PreviewEQ.from_fit(pf.summary())
            print(DOUBLE_EQ)
    level = prof.last_level() if prof is not None else None
    ear = {"L": "L", "R": "R", "both": "LR"}[args.ear]
    app = ToneApp(args.freq, ear, DEFAULT_LEVEL_DB if level is None else level, eq=eq, profile=prof,
                  device=args.device, dry_run=args.dry_run)
    app.run()
    return 0


def _settings(args, profile):
    from flatiron.apo import parse_apo
    from flatiron.session.runner import RunSettings

    eq, text, note = None, None, args.eq_note or "none"
    if args.active_eq:
        text = open(args.active_eq).read()
        eq = parse_apo(text)
        note = args.eq_note or f"APO file {args.active_eq}"
    return RunSettings(stimulus=args.stimulus, session_minutes=args.minutes,
                       active_eq=eq, eq_note=note, eq_text=text, match=args.match,
                       match_boost_db=args.max_match_boost, debug=getattr(args, "debug", False))


def _launch(args, sweep: bool, profile=None, **extra):
    """extra: RunSettings overrides (verify: preview, max_trials)."""
    from dataclasses import replace

    from flatiron.session.runner import TrialRunner
    from flatiron.session.sweep import SweepTiming
    from flatiron.ui.app import FlatironApp

    profile = profile or _profile(args, create=getattr(args, "new", False))
    if profile is None:
        return 1
    timing, sweep_eq = SweepTiming(), None
    if sweep:
        seconds = getattr(args, "sweep_seconds", None) or timing.duration
        if not 10 <= seconds <= 90:
            print("--sweep-seconds must be 10 to 90 (each direction)")
            return 1
        from flatiron.session.sweep import sweep_range

        lo, hi = sweep_range(profile.measured)
        timing = SweepTiming(f_lo=lo, f_hi=hi, duration=seconds)
        if profile.sessions():
            from flatiron.audio.preview import PreviewEQ
            from flatiron.fit.run import NotEnoughTrials, current_fit

            try:
                pf = current_fit(profile, on_fit=lambda: print("Fitting, to apply the current correction "
                                                               "during the sweep...", flush=True))
                sweep_eq = PreviewEQ.from_fit(pf.summary())
                print("The sweep plays through the current correction. " + DOUBLE_EQ)
            except NotEnoughTrials:
                pass
            except (ValueError, FileNotFoundError) as e:
                print(f"Can't apply the correction during the sweep: {e}")
    extra_now = lambda: {k: (v() if callable(v) else v) for k, v in extra.items()}  # speaker: the next position
    settings = lambda: replace(_settings(args, profile), **extra_now())
    try:
        settings()  # validate options before the UI starts
    except ValueError as e:
        print(e)
        return 1
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
    return 0


def _not_speaker(prof, what: str) -> bool:
    """False (after saying why) for a speaker profile: it's evidence about the
    listener's ears, not something to correct."""
    if prof.kind == "speaker":
        print(f"{prof.read()['label']!r} is a speaker profile: it has no correction to {what}. "
              "See: flatiron report, flatiron compare")
        return False
    return True


def _launch_speaker(args, prof):
    """Speaker blocks (DESIGN.md, "Speaker sessions"), one after another down the position plan."""
    from flatiron.fit.run import listener_noise
    from flatiron.session.speaker import DEFAULT_BLOCK, MAX_BLOCK, MIN_BLOCK, next_block, progress

    if args.sweep:
        print("There's no sweep for speaker profiles.")
        return 1
    block = DEFAULT_BLOCK if args.block is None else args.block
    if not MIN_BLOCK <= block <= MAX_BLOCK:
        print(f"--block must be {MIN_BLOCK} to {MAX_BLOCK} trials")
        return 1
    done, total = progress(prof.sessions())
    if next_block(prof) is None:
        print(f"All {total} blocks of the position plan are done. See: flatiron report")
        return 0
    noise = listener_noise(prof.dir.parent.parent)
    if any(n.n_trials == 0 for n in noise.values()):
        print("Note: no headphone profile has 200 trials per ear yet, so these blocks use a default noise.")
    print(f"{done} of {total} blocks done.")
    channel = prof.read()["channel"]

    def speaker():
        nxt = next_block(prof)
        return {"open_ear": nxt.ear, "channel": channel, "position": nxt.to_json(), "block": block}

    return _launch(args, sweep=False, profile=prof, speaker=speaker, max_trials=block)


def cmd_run(args):
    prof = _profile(args, create=args.new)
    if prof is None:
        return 1
    if prof.kind == "speaker":
        return _launch_speaker(args, prof)
    if args.block is not None:
        print("--block is for speaker profiles.")
        return 1
    return _launch(args, sweep=args.sweep, profile=prof)


def cmd_verify(args):
    from flatiron.audio.preview import PreviewEQ
    from flatiron.fit.run import checkpoint, leftovers

    prof = _profile(args)
    if prof is None:
        return 1
    if not _not_speaker(prof, "verify"):
        return 1
    pf = _current(prof)
    if pf is None:
        return 1
    eq = PreviewEQ.from_fit(pf.summary())
    print(f"Verifying fit {eq.fit_hash}. " + DOUBLE_EQ)
    if checkpoint(prof) is None:
        print("No checkpoint, so the filters may move afterwards. To keep them: flatiron config --checkpoint")
    n_before = len(prof.sessions())
    rc = _launch(args, sweep=False, profile=prof, preview=eq, max_trials=args.trials)
    if rc or len(prof.sessions()) == n_before:
        return rc
    print("\nChecking what's left with the correction applied...")
    applied = {e: [PeakingFilter(f.fc, f.gain_db, f.q) for f in fs] for e, fs in eq.channels.items()}
    pf, left = leftovers(prof, applied)
    for e, feats in left.items():
        who = f"{e} ({pf.fits[e].n_trials} trials)"
        if not feats:
            print(f"  {who}: nothing significant left")
            continue
        print(f"  {who}: left: " + ", ".join(
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
    if not _not_speaker(prof, "audition"):
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
    print(DOUBLE_EQ)
    AuditionApp(sources, eq, _player(args), f"{prof.read()['label']} · fit {eq.fit_hash}").run()
    return 0


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _profile_listing(p, indent=""):
    info = p.read()
    sessions = p.sessions()
    n = {e: sum(t.ear == e for s in sessions for t in s.trials) for e in EARS}
    level = p.last_level(sessions)
    level_s = f"last level {level:+.0f} dBFS" if level is not None else "no sessions yet"
    last = sessions[-1].started[:10] if sessions else "-"
    open_s = sum(s.ended is None for s in sessions)
    kind = info.get("kind", "headphone")
    extra = ""
    if kind == "speaker":
        from flatiron.session.speaker import progress

        done, total = progress(sessions)
        kind, extra = f"speaker, channel {info.get('channel')}", f", {done} of {total} blocks"
    print(f"{indent}{info['label']}  ({p.slug})" + ("" if kind == "headphone" else f"  [{kind}]"))
    print(f"{indent}    {len(sessions)} sessions{extra}, L {n['L']} / R {n['R']} trials, last {last}, {level_s}"
          + (f", {open_s} not closed" if open_s else ""))


def _named(name: str):
    """(listener, profile or None, profile part) for LISTENER/PROFILE or a bare name; None after printing why."""
    from flatiron.session.store import Profile, split_name

    try:
        listener, pname = split_name(name)
        return listener, (Profile.existing(pname, listener.root) if pname else None), pname
    except ValueError as e:
        print(e)
        return None


def _references_to(prof):
    """Other profiles of the same listener that use this one (consensus, veto,
    pooling), and the ones this one uses."""
    from flatiron.fit.run import profile_settings
    from flatiron.session.store import Profile

    root = prof.dir.parent.parent

    def uses(p):
        st = profile_settings(p)
        return set(st.consensus or ()) | set(st.veto or ()) | set(p.read().get("pool") or ())

    users = [p.read()["label"] for p in Profile.all(root) if p.slug != prof.slug and prof.slug in uses(p)]
    used = sorted(uses(prof) - {prof.slug})
    return users, used


def _rename_references(root, old: str, new: str) -> list[str]:
    """Point every reference to profile slug `old` among the listener's
    profiles (consensus, veto, pooling, session selection) at `new`. The
    labels of the other profiles changed."""
    import copy

    from flatiron.fit.pipeline import CONSENSUS_PREFIX
    from flatiron.session.store import Profile

    sub = lambda xs: [new if x == old else x for x in xs]
    changed = []
    for p in Profile.all(root):
        d = p.read()
        e = copy.deepcopy(d)
        st = e.get("settings") or {}
        ref = st.get("reference")
        if isinstance(ref, str) and ref.startswith(CONSENSUS_PREFIX):
            st["reference"] = CONSENSUS_PREFIX + ",".join(sub(ref[len(CONSENSUS_PREFIX):].split(",")))
        for holder, k in ((st, "veto"), (e, "pool"), (e, "sessions_used")):
            if holder.get(k):
                holder[k] = sub(holder[k])
        if e != d:
            p.update(**e)
            if p.slug != new:
                changed.append(d["label"])
    return changed


def _trial_count(profiles) -> int:
    return sum(len(s.trials) for p in profiles for s in p.sessions())


def cmd_profiles(args):
    import shutil

    from flatiron.session.store import RESERVED, Listener, Profile, current_listener, slugify

    action = getattr(args, "action", None)
    if action in ("new", "set", "move", "rename", "delete"):
        found = _named(args.name)
        if found is None:
            return 1
        listener, prof, pname = found
        if not pname:
            print("Name the profile (LISTENER/PROFILE or PROFILE).")
            return 1
    if action == "new":
        if prof is not None:
            print(f"There's already a profile {args.name!r}.")
            return 1
        if args.kind == "speaker" and not args.channel:
            print("A speaker profile needs --channel L or R: the output channel that drives the speaker "
                  "(only one plays at a time).")
            return 1
        if args.channel and args.kind != "speaker":
            print("--channel is only for speaker profiles (--kind speaker).")
            return 1
        try:
            prof = Profile.open(pname, listener.root)
        except ValueError as e:
            print(e)
            return 1
        prof.update(kind=args.kind, **({"channel": args.channel} if args.channel else {}))
        print(f"Created {args.name!r} ({args.kind}" + (f", channel {args.channel}" if args.channel else "") + ").")
        if args.kind == "speaker":
            print(f"Measure it with: flatiron run {args.name!r} (blocks of one ear and one position each)")
        return 0
    if action in ("set", "move", "rename", "delete") and prof is None:
        print(f"No profile {args.name!r}. See: flatiron profiles")
        return 1
    if action == "set":
        if args.kind and args.kind != prof.kind and "speaker" in (args.kind, prof.kind) and prof.sessions():
            print("A profile with sessions can't become a speaker profile or stop being one: their sessions "
                  "mean different things. Start a new profile instead.")
            return 1
        if args.kind == "speaker" and not prof.read().get("channel"):
            print("A speaker profile needs a channel: create one with flatiron profiles new NAME --kind speaker "
                  "--channel L|R")
            return 1
        if args.kind:
            prof.update(kind=args.kind)
        if args.label:
            prof.update(label=args.label)
        return 0
    if action == "rename":
        try:
            slug = slugify(args.new)
        except ValueError as e:
            print(e)
            return 1
        old = prof.slug
        if slug != old:
            if slug in RESERVED:
                print(f"{args.new!r} can't be a profile name: `config --veto none` uses it.")
                return 1
            if Profile(slug, listener.root).dir.exists():
                print(f"There's already a profile {slug!r}.")
                return 1
            prof.dir.rename(Profile(slug, listener.root).dir)
            prof = Profile(slug, listener.root)
            for sess in prof.sessions():
                sess.profile = slug
                prof.save(sess)
        old_label = prof.read()["label"]
        prof.update(label=args.new, slug=slug)
        users = _rename_references(listener.root, old, slug) if slug != old else []
        print(f"Renamed {old_label!r} to {args.new!r}" + (f" ({slug})" if slug != args.new else "") + "."
              + (f" Updated {', '.join(users)}, which refer to it." if users else ""))
        return 0
    if action == "delete":
        users, _ = _references_to(prof)
        if users:
            print(f"Not deleted: {', '.join(users)} use{'s' if len(users) == 1 else ''} it (consensus, veto or "
                  "pooled sessions). Change their settings first.")
            return 1
        n_s, n_t = len(prof.sessions()), _trial_count([prof])
        if _confirm(f"Delete profile {prof.read()['label']!r} of {listener.slug!r} ({n_s} sessions, {n_t} trials)? "
                    "This can't be undone.", args.yes):
            shutil.rmtree(prof.dir)
            print("Deleted. The listener's noise is refitted the next time it's needed.")
        else:
            print("Not deleted.")
        return 0
    if action == "move":
        to = Listener.find(args.to)
        if to is None:
            print(f"No listener {args.to!r}. See: flatiron listeners")
            return 1
        if to.root == listener.root:
            print(f"{args.name!r} already belongs to {to.slug!r}.")
            return 1
        if Profile(prof.slug, to.root).path.exists():
            print(f"{to.slug!r} already has a profile {prof.slug!r}: rename one first (flatiron profiles rename).")
            return 1
        users, used = _references_to(prof)
        if users or used:
            if users:
                print(f"Not moved: {', '.join(users)} use{'s' if len(users) == 1 else ''} it (consensus, veto or "
                      "pooled sessions). Change their settings first.")
            if used:
                print(f"Not moved: it uses {', '.join(used)} (consensus, veto or pooled sessions), which stay "
                      f"with {listener.slug!r}. Reset those settings first (flatiron config NAME --reset ...).")
            return 1
        to.ensure()
        (to.root / "profiles").mkdir(parents=True, exist_ok=True)
        prof.dir.rename(Profile(prof.slug, to.root).dir)
        print(f"Moved {args.name!r} to {to.slug!r}. Both listeners' noise "
              "is refitted the next time it's needed.")
        return 0
    everyone = Listener.all()
    if len(everyone) > 1:
        for x in everyone:
            print(f"{x.slug}/  ({x.label})")
            ps = x.profiles()
            for p in ps:
                _profile_listing(p, "    ")
            if not ps:
                print("    no profiles yet")
        return
    profiles = Profile.all(current_listener().root)
    if not profiles:
        print("No profiles yet. Start one with: flatiron run 'Headphone name' --new")
        return
    for p in profiles:
        _profile_listing(p)


def cmd_listeners(args):
    from flatiron.fit.run import listener_noise
    from flatiron.session.store import LISTENER_ENV, Listener, slugify
    from flatiron.model.grid import MEASURE_HI, MEASURE_LO

    action = getattr(args, "action", None)
    if action == "new":
        try:
            x = Listener.create(args.name)
        except ValueError as e:
            print(e)
            return 1
        print(f"Created listener {x.slug!r}. Name its profiles as {x.slug}/PROFILE, e.g. "
              f"flatiron run '{x.slug}/Headphone name' --new")
        return 0
    if action == "rename":
        x = Listener.find(args.name)
        if x is None:
            print(f"No listener {args.name!r}. See: flatiron listeners")
            return 1
        try:
            new = x.rename(args.new)
        except ValueError as e:
            print(e)
            return 1
        print(f"Renamed {x.slug!r} to {new.slug!r}" + (f" ({args.new})" if args.new != new.slug else "") + ".")
        env = os.environ.get(LISTENER_ENV)
        if env and slugify(env) == x.slug and new.slug != x.slug:
            print(f"Note: {LISTENER_ENV} still names {env!r}.")
        return 0
    if action == "delete":
        import shutil

        x = Listener.find(args.name)
        if x is None:
            print(f"No listener {args.name!r}. See: flatiron listeners")
            return 1
        profiles = x.profiles()
        if _confirm(f"Delete listener {x.slug!r} ({x.label}) with {len(profiles)} profiles and "
                    f"{_trial_count(profiles)} trials? This can't be undone.", args.yes):
            shutil.rmtree(x.root)
            print("Deleted.")
            env = os.environ.get(LISTENER_ENV)
            if env and slugify(env) == x.slug:
                print(f"Note: {LISTENER_ENV} still names {env!r}.")
        else:
            print("Not deleted.")
        return 0
    everyone = Listener.all()
    if not everyone:
        print("No listeners yet: the first profile you create starts the default one. "
              "flatiron listeners new NAME adds another.")
        return 0
    env = os.environ.get(LISTENER_ENV)
    for x in everyone:
        profiles = x.profiles()
        trials = {e: 0 for e in EARS}
        for p in profiles:
            for s in p.sessions():
                for e in EARS:
                    trials[e] += sum(t.ear == e and not t.undone for t in s.trials)
        mark = "  (FLATIRON_LISTENER)" if env and Listener.find(env) and Listener.find(env).slug == x.slug else ""
        print(f"{x.slug}  ({x.label}){mark}")
        print(f"    {len(profiles)} profiles, L {trials['L']} / R {trials['R']} trials")
        lo, hi = x.measured_default
        if (lo, hi) != (MEASURE_LO, MEASURE_HI):
            print(f"    measured range {KHZ[1](lo)} to {KHZ[1](hi)} (config {x.slug} --default-min-measured, "
                  "--default-max-measured)")
        noise = listener_noise(x.root, fit=False)
        for e in EARS:
            n = noise.get(e)
            if n is None:
                print(f"    {e} noise: not fitted yet (it is the next time a fit is needed)")
            elif n.n_trials:
                print(f"    {e} noise: {n.sigma0:.2f} dB (×{n.treble_noise:.2f} above 10 kHz), lapse {n.lapse:.1%}, "
                      f"from {n.n_trials} trials")
            else:
                print(f"    {e} noise: the prior (needs a profile with 200 trials in this ear)")
    return 0


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
              f"{'  [' + s.speaker['open_ear'] + ' ear open, ' + s.speaker['position']['label'] + ']' if s.speaker else ''}"
              f"{'' if s.ended else '  (not closed)'}")
    trials, _ = prof.history(sessions)
    print(f"total: L {len(trials['L'])}, R {len(trials['R'])}")


# Fit settings `config` can change: flags, CorrectionSettings field, argparse
# keywords, and conversions between the flag's units and the field's.
PERCENT = (lambda v: v / 100, lambda v: v * 100)
NAMES = (lambda v: () if v == "none" else tuple(x.strip() for x in v.split(",") if x.strip()),
         lambda v: ",".join(v) or "none")
KHZ = (lambda v: v, lambda v: f"{v / 1000:g}k")
AUTO = (lambda v: None if v == "auto" else v, lambda v: "auto" if v is None else v)
CONSENSUS_FIELDS = ("consensus_prior", "consensus_coupling_db", "speaker_window_oct", "speaker_colour_db",
                    "consensus_gain_sd")
SPEAKER_FIELDS = ("speaker_window_oct", "speaker_colour_db", "consensus_gain_sd")

FIT_OPTIONS = [
    (("-w", "--width"), "max_width_oct", dict(type=float, help="max feature width W in octaves (1/3 to 2)")),
    (("-r", "--reference"), "reference",
     dict(help="diffuse (default: ISO 226 + ISO 389-7 diffuse field), iso226 (frontal free field), none, a CSV of freq_hz,db, or consensus:A,B,C for the raw curves of those "
               "profiles (this one only if listed)")),
    (("--consensus-prior",), "consensus_prior",
     dict(type=float, help="with a consensus reference: scale on the prior SD of your own narrow structure (0.5 dB "
                           "below 3 kHz rising to 3 dB above 6 kHz), 0 to 10. Larger keeps more of what the "
                           "headphones share in the reference; 0 keeps none of it")),
    (("--coupling",), "consensus_coupling_db",
     dict(type=float, help="with a consensus reference: SD of narrow structure every headphone's cup adds, which "
                           "headphones alone can't tell from yours (dB, 0 to 6). Larger keeps less of what they share")),
    (("--speaker-window",), "speaker_window_oct",
     dict(type=float, help="with speakers in the consensus: width (FWHM, octaves, 0.1 to 1) over which a speaker's "
                           "narrow structure is compared with the headphones'")),
    (("--speaker-colouration",), "speaker_colour_db",
     dict(type=_auto_float, metavar="DB|auto",
          help="with speakers in the consensus: SD of a speaker's own narrow colouration (dB, 0 to 6). auto (the "
               "default): estimated from two or more speakers, else 1 dB"), AUTO),
    (("--gain-sd",), "consensus_gain_sd",
     dict(type=_auto_float, metavar="SD|auto",
          help="with speakers in the consensus: how far the headphones-only share may be off, as an SD (0.05 to 2). "
               "auto (the default): estimated from the speakers"), AUTO),
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
    (("--balance",), "balance",
     dict(type=float, help="strength of the prior that the ears are alike, as a multiple of the default SDs "
                           "(1.5 dB below 5 kHz, 3 above 8 kHz): larger is looser; 0: each ear on its own")),
    (("--phon",), "phon", dict(type=float, help="listening level assumed by the reference")),
    (("-k", "--k-narrow"), "k", dict(type=float, help="significance for narrow features (SDs)")),
    (("--k-broad",), "k_broad", dict(type=float, help="significance for broad features (SDs)")),
    (("--soft-margin",), "soft_width",
     dict(type=float, help="partly correct features within this many SDs of the significance threshold "
                           "(0 to 2; 0: all or nothing)")),
    (("--broad-strength",), "broad_strength",
     dict(type=float, help="how much of each broad feature to correct toward the fixed target, 0 to 100%%. "
                           "0 (the default): correct narrow features only and keep the original broad shape, "
                           "leaving broad tonal changes to you"), PERCENT),
    (("-b", "--bands"), "bands", dict(type=int, help="correction bands per channel (max 128; LSP and EasyEffects hold 32)")),
    (("--fit-tolerance",), "fit_tol_db",
     dict(type=float, help="how close the filters must come to the target, in dB (0.1 to 6)")),
    (("--max-boost-narrow",), "max_boost_narrow_db", dict(type=float, help="max boost for narrow features (dB)")),
    (("--max-boost-broad",), "max_boost_broad_db", dict(type=float, help="max boost for broad features (dB)")),
    (("--max-cut",), "max_cut_db", dict(type=float, help="max cut (dB)")),
    (("--min-corrected",), "f_lo",
     dict(type=_hz, help="lowest frequency corrected (Hz, or kHz with a k: 1k); never below --min-measured"), KHZ),
    (("--max-corrected",), "f_hi",
     dict(type=_hz, help="highest frequency corrected (Hz, or kHz with a k: 13k); never above --max-measured"),
     KHZ),
    (("--seating",), "consistency",
     dict(action=argparse.BooleanOptionalAction,
          help="correct each feature to its median seating's level, and drop it if most seatings show little of it")),
    (("--method",), "method",
     dict(choices=("features", "smooth", "match"),
          help="features (default): correct the significant features found against the baseline; "
               "smooth (experimental): the baseline is the curve smoothed over --smooth-width, and every "
               "residual is corrected, with no feature detection (more correction, noise included); "
               "match (experimental): correct the whole curve toward the reference, tonality included")),
    (("--baseline",), "baseline",
     dict(choices=("robust", "joint"),
          help="robust (default): the robustly weighted spline; joint (experimental): the posterior mean of the "
               "tonality under a model of tonality plus features, which moves smoothly with the data instead of "
               "tipping between two readings (needs --method features)")),
    (("--shared-baseline",), "shared_baseline",
     dict(action=argparse.BooleanOptionalAction,
          help="one baseline for both ears, each with its own level (default): their broad shape is taken to be the "
               "same. --no-shared-baseline: each ear its own, which also takes up broad differences between the "
               "ears (with --method features)")),
    (("--smooth-width",), "smooth_oct",
     dict(type=float, help="smoothing width (FWHM) in octaves for --method smooth, 1/8 to 1")),
]
# The measured range: a profile setting `config` changes (each end overrides the
# listener's default), but not a fit setting: it decides where trials go.
MEASURED_OPTIONS = (("min-measured", "min_measured", "lo"), ("max-measured", "max_measured", "hi"))
# The listener's own settings (`config LISTENER`): names disjoint from a profile's.
LISTENER_OPTIONS = (("default-min-measured", "default_min_measured", "lo"),
                    ("default-max-measured", "default_max_measured", "hi"))
LISTENER_RESET = tuple(n for n, *_ in LISTENER_OPTIONS)
MIN_MEASURED_OCT = 1.0  # the narrowest measured range
MEASURED_LIMITS = (50.0, 18_000.0)  # the widest measured range (Hz)

# What --reset takes besides the settings above. "all" is every setting and the
# session selection, but not the checkpoint, which has to be named.
RESET_EXTRA = ("sessions", "checkpoint") + tuple(n for n, *_ in MEASURED_OPTIONS)


def _option_name(flags) -> str:
    return flags[-1].lstrip("-")


def _value(st, f):
    """A setting as `config` shows and takes it. Broad strength stands for two
    fields: 0 is preserve_tonality (the default), anything else its strength."""
    if f == "broad_strength":
        return 0.0 if st.preserve_tonality else st.broad_strength * 100
    for flags, ff, kw, *conv in FIT_OPTIONS:
        if ff == f and conv:
            return conv[0][1](getattr(st, f))
    return getattr(st, f)


def _show(st, f) -> str:
    v = _value(st, f)
    if isinstance(v, bool):
        return "on" if v else "off"
    return f"{v:g}" if isinstance(v, float) else str(v)


def _slug_list(names, what, root):
    """Profile slugs for names, all the listener's at `root` (None after
    printing why, if one doesn't exist)."""
    from flatiron.session.store import Profile

    out = []
    for name in names:
        if "/" in name:
            print(f"The {what} takes profiles of the same listener: {name!r} names a listener (nothing changed).")
            return None
        try:
            other = Profile.existing(name, root)
        except ValueError:
            other = None
        if other is None:
            print(f"No profile {name!r} for the {what} (nothing changed). See: flatiron profiles")
            return None
        out.append(other.slug)
    return tuple(dict.fromkeys(out))


def _session_selection(prof, text):
    """--sessions: (pool, sessions_used) from a list of session ids and
    profile names, or None after printing why it can't be used."""
    from flatiron.session.store import Profile

    root = prof.dir.parent.parent
    owner = {s.id: p.slug for p in Profile.all(root) for s in p.sessions()}
    pool, chosen = [], []
    for item in (x.strip() for x in text.split(",") if x.strip()):
        if item in owner:
            slug = owner[item]
            chosen.append(item)
        else:
            slugs = _slug_list([item], "session selection (not a session id either)", root)
            if slugs is None:
                return None
            slug = slugs[0]
            chosen.append(slug)
        if slug != prof.slug and slug not in pool:
            pool.append(slug)
    if not chosen:
        print("--sessions needs session ids or profile names (nothing changed).")
        return None
    # Only whole profiles, this one among them: plain pooling, no selection.
    if prof.slug in chosen and all(c == prof.slug or c in pool for c in chosen):
        return pool, None
    return pool, list(dict.fromkeys(chosen))


def _describe_selection(prof) -> str:
    from flatiron.fit.run import profile_session_ids
    from flatiron.session.store import Profile

    ids = profile_session_ids(prof)
    pool = prof.read().get("pool") or []
    root = prof.dir.parent.parent
    label = lambda slug: Profile(slug, root).read()["label"] if Profile(slug, root).path.exists() else slug
    if not ids:
        own = "all of this profile's"
        return own + (f", and all of {', '.join(label(s) for s in pool)}'s (pooled)" if pool else "")
    from flatiron.fit.run import owned_fit_sessions

    session_ids = {s.id for _, s in owned_fit_sessions(prof)}
    return ", ".join(x if x in session_ids else f"all of {label(x)}'s" for x in ids)


def _range_error(lo: float, hi: float) -> str | None:
    f_min, f_max = MEASURED_LIMITS
    if not f_min <= lo < hi <= f_max:
        return f"The measured range must be within {f_min:g} Hz to {f_max / 1000:g} kHz, low end first"
    if np.log2(hi / lo) < MIN_MEASURED_OCT:
        return f"The measured range must span at least {MIN_MEASURED_OCT:g} octave"
    return None


def _quiet_profile(name: str):
    """The profile `name` resolves to, as _profile would, or None; prints nothing."""
    from flatiron.session.store import Profile, current_listener, split_name

    try:
        listener, pname = split_name(name) if name else (current_listener(), "")
    except ValueError:
        return None
    if pname:
        return Profile.existing(pname, listener.root)
    profiles = Profile.all(listener.root)
    return profiles[0] if len(profiles) == 1 else None


def _quiet_listener(name: str):
    """The listener `name` names (`NAME` or `NAME/`), or the current one if
    there's no name; None if there isn't one. Prints nothing."""
    from flatiron.session.store import Listener, current_listener

    if not name:
        try:
            return current_listener()
        except ValueError:
            return None
    who = name[:-1] if name.endswith("/") else name
    return None if "/" in who else Listener.find(who)


def _margin_warnings(prof, prefix: str = "") -> list[str]:
    """Warnings for a correction range set explicitly too near (or past) the
    measured range's ends: the baseline and taper need room there."""
    from flatiron.fit.pipeline import CORRECTED_MARGIN_OCT, CORRECTED_WARN_OCT
    from flatiron.fit.run import profile_settings

    own = prof.read().get("settings") or {}
    st = profile_settings(prof)
    lo, hi = prof.measured
    out = []
    for f, name, measured, inside in (("f_lo", "min", lo, np.log2(st.f_lo / lo)),
                                      ("f_hi", "max", hi, np.log2(hi / st.f_hi))):
        if f not in own or inside >= CORRECTED_WARN_OCT - 1e-9:
            continue
        where = (f"is only {inside:.2f} octave inside" if inside > 1e-9 else
                 "is at" if inside > -1e-9 else "is outside")
        out.append(f"{prefix}Warning: {name}-corrected {KHZ[1](getattr(st, f))} {where} {name}-measured "
                   f"{KHZ[1](measured)}; keep it at least {CORRECTED_WARN_OCT:g} octave inside, so the baseline and "
                   f"taper have room (the default is {CORRECTED_MARGIN_OCT:g}; --reset {name}-corrected follows it).")
    return out


def _profile_summary(p) -> tuple:
    """What a listener's change can move in a profile: its measured and correction ranges."""
    from flatiron.fit.run import profile_settings

    st = profile_settings(p)
    lo, hi = p.measured
    return {"min-measured": KHZ[1](lo), "max-measured": KHZ[1](hi),
            "min-corrected": KHZ[1](st.f_lo), "max-corrected": KHZ[1](st.f_hi)}


def _config_listener(args, reset):
    from flatiron.model.grid import MEASURE_HI, MEASURE_LO
    from flatiron.session.store import AmbiguousListener, current_listener

    x = _quiet_listener(args.profile or "")
    if x is None:
        if args.profile and _quiet_profile(args.profile) is not None:
            print(f"{args.profile!r} is a profile; --default-min-measured and --default-max-measured are the "
                  "listener's (nothing changed). Name the listener: flatiron config LISTENER --default-max-measured "
                  "12k, or set the profile's own with --min-measured / --max-measured.")
        elif args.profile:
            print(f"No listener {args.profile!r} (nothing changed). See: flatiron listeners")
        else:
            try:
                current_listener()
            except AmbiguousListener as e:
                print(str(e).replace("name the profile as LISTENER/PROFILE", "name the listener") + " (nothing changed)")
        return 1
    old_lo, old_hi = x.measured_default
    if reset is not None:
        every = "all" in reset
        lo = MEASURE_LO if every or "default-min-measured" in reset else old_lo
        hi = MEASURE_HI if every or "default-max-measured" in reset else old_hi
    else:
        lo = old_lo if args.default_min_measured is None else args.default_min_measured
        hi = old_hi if args.default_max_measured is None else args.default_max_measured
    err = _range_error(lo, hi)
    if err:
        print(f"{err} (nothing changed).")
        return 1
    profiles = x.profiles()
    for p in profiles:
        own = p.measured_override
        if _range_error(own.get("lo", lo), own.get("hi", hi)):
            print(f"That would leave {p.read()['label']!r} with no valid measured range: it sets one end itself "
                  f"({', '.join(f'{k} {KHZ[1](v)}' for k, v in own.items())}). Change or reset that first "
                  "(nothing changed).")
            return 1
    before = {p.slug: _profile_summary(p) for p in profiles}
    x.set_measured_default(lo, hi)
    for name, a, b in (("default-min-measured", old_lo, lo), ("default-max-measured", old_hi, hi)):
        if a != b:
            print(f"{name}: {KHZ[1](a)} -> {KHZ[1](b)}")
    for p in profiles:
        after = _profile_summary(p)
        moved = [f"{k} {before[p.slug][k]} -> {v}" for k, v in after.items() if v != before[p.slug][k]]
        if moved:
            print(f"  {p.read()['label']}: " + ", ".join(moved))
        for w in _margin_warnings(p, prefix=f"  {p.read()['label']}: "):
            print(w)
    return 0


def _show_listener(x):
    from flatiron.model.grid import MEASURE_HI, MEASURE_LO

    lo, hi = x.measured_default
    print(f"Listener settings for {x.slug!r} (* = changed from the default):")
    for name, v, dflt in (("default-min-measured", lo, MEASURE_LO), ("default-max-measured", hi, MEASURE_HI)):
        mark = "*" if v != dflt else " "
        print(f"  {mark} {name:20} {KHZ[1](v)}" + (f"   (default {KHZ[1](dflt)})" if mark == "*" else ""))


def cmd_config(args):
    """Profile settings, or, given only listener settings, the listener's."""
    reset = None
    if args.reset is not None:
        reset = [x.strip() for x in args.reset.split(",") if x.strip()]
    listener_given = any(getattr(args, f) is not None for _, f, _ in LISTENER_OPTIONS)
    profile_given = (any(getattr(args, f) is not None for _, f, *_ in FIT_OPTIONS)
                     or any(getattr(args, f) is not None for _, f, _ in MEASURED_OPTIONS)
                     or args.sessions or args.checkpoint)
    names = LISTENER_RESET + ("all",)
    listener_reset = bool(reset) and all(n in names for n in reset) and (
        any(n in LISTENER_RESET for n in reset)
        # --reset all: the listener's when the name is a listener's and not a profile's
        or (args.profile and _quiet_profile(args.profile) is None and _quiet_listener(args.profile) is not None))
    if reset and any(n in LISTENER_RESET for n in reset) and not all(n in names for n in reset):
        print("Listener settings (default-min-measured, default-max-measured) and profile settings can't be reset "
              "together (nothing changed).")
        return 1
    if listener_given or listener_reset:
        if profile_given or (listener_given and reset is not None):
            print("Listener settings (--default-min-measured, --default-max-measured) can't be changed together with "
                  "profile settings or a reset (nothing changed).")
            return 1
        return _config_listener(args, reset)
    if reset is None and not profile_given and _quiet_profile(args.profile or "") is None:
        x = _quiet_listener(args.profile or "")
        if x is not None and (not args.profile or _quiet_listener(args.profile) is not None):
            # A listener named, or no single profile to show: the listener's settings.
            _show_listener(x)
            if not args.profile:
                print("Name a profile for its settings: flatiron config PROFILE")
            return 0
    return _config_profile(args, reset)


def _config_profile(args, reset):
    from dataclasses import replace

    from flatiron.fit.pipeline import CorrectionSettings
    from flatiron.fit.reference import CONSENSUS_PREFIX, reference
    from flatiron.fit.run import (checkpoint, default_settings, profile_settings, set_checkpoint,
                                  set_profile_settings)
    from flatiron.session.store import listener_of

    prof = _profile(args)
    if prof is None:
        return 1
    label = prof.read()["label"]
    old = profile_settings(prof)
    old_measured = prof.measured
    names = {_option_name(flags): f for flags, f, *_ in FIT_OPTIONS}
    measured = {end: getattr(args, f) for _, f, end in MEASURED_OPTIONS}
    changing = (any(getattr(args, f) is not None for _, f, *_ in FIT_OPTIONS) or args.sessions or args.checkpoint
                or any(v is not None for v in measured.values()))

    def report_changes():
        new_lo, new_hi = prof.measured
        for name, a, b in (("min-measured", old_measured[0], new_lo), ("max-measured", old_measured[1], new_hi)):
            if a != b:
                print(f"{name}: {KHZ[1](a)} -> {KHZ[1](b)}")
        final = profile_settings(prof)
        for flags, f, *_ in FIT_OPTIONS:
            if _show(old, f) != _show(final, f):
                print(f"{_option_name(flags)}: {_show(old, f)} -> {_show(final, f)}")
        return final

    if reset is not None:
        if changing:
            print("--reset can't be combined with other changes (nothing changed): reset first, then change.")
            return 1
        unknown = [n for n in reset if n not in names and n not in RESET_EXTRA and n != "all"]
        if unknown or not reset:
            print(f"Unknown setting(s) {', '.join(unknown) or '(none given)'}; they are: all, "
                  f"{', '.join(list(names) + list(RESET_EXTRA))}; and the listener's {', '.join(LISTENER_RESET)}")
            return 1
        listed = reset
        if "all" in listed:
            listed = (list(names) + ["sessions"] + [n for n, *_ in MEASURED_OPTIONS]
                      + (["checkpoint"] if "checkpoint" in listed else []))
        # The measured range first: the correction range's default follows it.
        own = prof.measured_override
        for n, _, end in MEASURED_OPTIONS:
            if n in listed:
                own.pop(end, None)
        lo, hi = listener_of(prof).measured_default
        err = _range_error(own.get("lo", lo), own.get("hi", hi))
        if err:
            print(f"{err} (nothing changed).")
            return 1
        prof.update(measured=own or None)
        default = default_settings(prof)
        new = profile_settings(prof)
        for n in listed:
            if n in names:
                f = names[n]
                new = replace(new, **{f: getattr(default, f)},
                              **({"preserve_tonality": default.preserve_tonality} if f == "broad_strength" else {}))
        set_profile_settings(prof, new)
        report_changes()
        for w in _margin_warnings(prof):
            print(w)
        if "sessions" in listed and (prof.read().get("sessions_used") or prof.read().get("pool")):
            prof.update(sessions_used=None, pool=None)
            print("sessions: all of this profile's (no pooling)")
        if "checkpoint" in listed:
            if checkpoint(prof) is None:
                print("There's no checkpoint to reset.")
            else:
                set_checkpoint(prof, None)
                print("Checkpoint reset: the fit chooses all its filters again.")
        return 0

    changes = {}
    for flags, f, kw, *conv in FIT_OPTIONS:
        v = getattr(args, f)
        if v is None:
            continue
        if f == "broad_strength":
            if v == 0:
                changes.update(preserve_tonality=True, broad_strength=CorrectionSettings().broad_strength)
            else:
                changes.update(preserve_tonality=False, broad_strength=PERCENT[0](v))
        else:
            changes[f] = conv[0][0](v) if conv else v

    if str(changes.get("reference", "")).startswith(CONSENSUS_PREFIX):
        members = _slug_list(NAMES[0](changes["reference"][len(CONSENSUS_PREFIX):]), "consensus", prof.dir.parent.parent)
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
        changes["veto"] = _slug_list(changes["veto"], "veto", prof.dir.parent.parent)
        if changes["veto"] is None:
            return 1
    own = prof.measured_override
    if any(v is not None for v in measured.values()):
        own.update({end: float(v) for end, v in measured.items() if v is not None})
        lo, hi = listener_of(prof).measured_default
        err = _range_error(own.get("lo", lo), own.get("hi", hi))
        if err:
            print(f"{err} (nothing changed).")
            return 1
    try:
        new = replace(old, **changes)
        if not new.consensus:
            reference(new.reference, [1000.0], new.phon)
    except (ValueError, FileNotFoundError) as e:
        print(e)
        return 1
    if new.method == "smooth" and not new.preserve_tonality:
        print("--method smooth doesn't correct broad features: --broad-strength must be 0 (nothing changed).")
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
    given = [f for f in CONSENSUS_FIELDS if getattr(args, f) is not None]
    if given and not new.consensus:
        print("--consensus-prior, --coupling, --speaker-window, --speaker-colouration and --gain-sd only apply with a "
              "consensus reference (nothing changed).")
        return 1
    if any(f in SPEAKER_FIELDS for f in given) and not _consensus_speakers(new, prof.dir.parent.parent):
        print("--speaker-window, --speaker-colouration and --gain-sd only apply with a speaker in the consensus "
              "(nothing changed).")
        return 1
    selection = None
    if args.sessions:
        selection = _session_selection(prof, args.sessions)
        if selection is None:
            return 1
    if "reference" in changes or "veto" in changes:
        # Checked here too, so a bad list never gets saved. Only when one's being
        # set: --reset sessions has to work on a profile pooling has put in conflict.
        from flatiron.fit.run import consensus_members, veto_members

        try:
            consensus_members(prof, new)
            if new.veto and not veto_members(prof, new):
                raise ValueError("--veto needs at least one other profile.")
        except ValueError as e:
            print(f"{e} (nothing changed)")
            return 1
    # The measured range first: a correction range left at its default follows it.
    if own != prof.measured_override:
        prof.update(measured=own or None)
    if changes:
        set_profile_settings(prof, replace(profile_settings(prof), **changes))
    new = report_changes()
    for w in _margin_warnings(prof):
        print(w)
    if "reference" in changes and new.consensus and new.reference != old.reference:
        from flatiron.session.store import Profile

        root = prof.dir.parent.parent
        speakers = _consensus_speakers(new, root)
        heads = [s for s in new.consensus if s not in speakers]
        print("Reference: consensus of " + ", ".join(Profile(s, root).read()["label"] for s in heads)
              + (", with " + ", ".join(Profile(s, root).read()["label"] for s in speakers) if speakers else ""))
        if len(heads) == 1:
            print("Warning: a single headphone's curve, not a consensus.")
        elif len(heads) == 2:
            print("Warning: with two headphones the consensus is their weighted mean, not a median.")
    if new.consensus and new.veto and (not old.consensus or not old.veto):
        from flatiron.report.html import VETO_WITH_CONSENSUS

        print(VETO_WITH_CONSENSUS)
    if selection is not None:
        pool, used = selection
        prof.update(pool=pool or None, sessions_used=used)
        print(f"sessions: {_describe_selection(prof)}")
    if args.checkpoint:
        pf = _current(prof)
        if pf is None:
            return 1
        set_checkpoint(prof, pf)
        n = " / ".join(f"{e} {len(c.filters)}" for e, c in pf.correction.ears.items())
        print(f"Checkpoint: the current filters ({n}) are fixed; later fits only add to them.")
    if changing:
        return 0
    # Nothing to change: show the settings.
    st = profile_settings(prof)
    default = default_settings(prof)
    print(f"Fit settings for {label!r} (* = changed from the default):")
    for flags, f, *_ in FIT_OPTIONS:
        mark = "*" if _show(st, f) != _show(default, f) else " "
        dflt = "" if mark == " " else f"   (default {_show(default, f)})"
        unused = ("   (unused without --method smooth)" if f == "smooth_oct" and st.method != "smooth" else
                  "   (unused without a consensus reference)" if f in CONSENSUS_FIELDS and not st.consensus else
                  "   (unused without a speaker in the consensus)" if f in SPEAKER_FIELDS
                  and not _consensus_speakers(st, prof.dir.parent.parent) else
                  "   (unused without --veto)" if f in ("veto_mode", "veto_import_db", "veto_softness") and not st.veto
                  else "")
        print(f"  {mark} {_option_name(flags):20} {_show(st, f)}{dflt}{unused}")
    own = prof.measured_override
    x = listener_of(prof)
    for (name, _, end), v, dflt in zip(MEASURED_OPTIONS, prof.measured, x.measured_default):
        note = f"   (listener {x.slug!r}: {KHZ[1](dflt)})" if end in own else "   (the listener's)"
        print(f"  {'*' if end in own else ' '} {name:20} {KHZ[1](v)}{note}")
    print(f"  {'*' if prof.read().get('sessions_used') or prof.read().get('pool') else ' '} "
          f"{'sessions':20} {_describe_selection(prof)}")
    cp = prof.read().get("checkpoint")
    if cp:
        n = " / ".join(f"{e} {len(v)}" for e, v in cp["filters"].items())
        print(f"  checkpoint: {n} filters fixed since {cp['made'][:16].replace('T', ' ')} "
              "(drop it with --reset checkpoint)")
    else:
        print("  checkpoint: none")
    for w in _margin_warnings(prof):
        print(w)
    return 0


def _print_fit(prof, pf):
    from flatiron.report.html import (ambiguous_features, fitted_values, listener_prefix, reason_parts,
                                      reference_description, reference_notes, top_notes)
    from flatiron.report.plots import EAR_NAMES

    corr = pf.correction
    pooled = f" (pooled with {', '.join(pf.pooled)})" if pf.pooled else ""
    n = len(pf.sessions)
    print(f"{listener_prefix(prof)}{n} session{'' if n == 1 else 's'}{pooled} · preamp {corr.preamp_db:+.1f} dB")
    for note in [reference_description(corr.settings, pf)] + reference_notes(pf, kept=True) + top_notes(pf, False):
        print(note)
    for e, c in corr.ears.items():
        f = pf.fits[e]
        print(f"\n{EAR_NAMES[e]} ear · {f.n_trials} trials")
        for v in fitted_values(f, False):
            print(f"  {v}")
        comp = {id(x) for x in c.compensation}
        kept = {id(x) for x in c.fixed}
        print("  Filters:" if c.filters else "  Filters: none needed")
        for flt in c.filters:
            note = "  checkpoint" if id(flt) in kept else "  tonality" if id(flt) in comp else ""
            print(f"    PK {flt.fc:6.0f} Hz {flt.gain_db:+5.1f} dB  Q {flt.q:5.2f}{note}")
        for kind in ("narrow", "broad"):
            acc = sorted((x for x in c.features if x.accepted and x.kind == kind), key=lambda x: x.fc)
            if acc:
                print(f"  {kind.capitalize()} features corrected: " + ", ".join(
                    f"{x.fc:.0f} Hz {_correction_of(x):+.1f} dB"
                    + (f" ({', '.join(reason_parts(x))})" if reason_parts(x) else "") for x in acc))
        rej = [x for x in c.features if not x.accepted and x.reason == "most seatings show little of it"]
        if rej:
            print("  Dropped (few seatings show it): " + ", ".join(f"{x.fc:.0f} Hz" for x in rej))
        vetoed = [x for x in c.features if x.veto is not None and not x.accepted]
        if vetoed:
            print("  Left alone by the veto: " + ", ".join(f"{x.fc:.0f} Hz" for x in sorted(vetoed, key=lambda x: x.fc)))
        unsure = ambiguous_features(c)
        if unsure:
            print("  Baseline uncertain under: " + ", ".join(f"{x.fc:.0f} Hz" for x in unsure)
                  + " (part may be tonality)")
        for lo, hi, m in c.left_out:
            print(f"  Out of bands, left uncorrected: {lo:.0f}-{hi:.0f} Hz (up to {m:.1f} dB)")


def _print_speaker(prof, pf):
    """A speaker profile: its direction-averaged curve's features, with no
    correction (DESIGN.md, "Positions as random effects")."""
    from flatiron.report.html import fitted_values, listener_prefix
    from flatiron.report.plots import EAR_NAMES
    from flatiron.session.speaker import progress

    done, total = progress(pf.sessions)
    print(f"{listener_prefix(prof)}Speaker profile (channel {prof.read().get('channel')}): {done} of {total} blocks · no correction")
    for e, c in pf.correction.ears.items():
        f = pf.fits[e]
        print(f"\n{EAR_NAMES[e]} ear · {f.n_trials} trials")
        for v in fitted_values(f, True):
            print(f"  {v}")
        for kind in ("narrow", "broad"):
            acc = sorted((x for x in c.features if x.accepted and x.kind == kind), key=lambda x: x.fc)
            print(f"  {kind.capitalize()} features: " + (", ".join(f"{x.fc:.0f} Hz {x.peak_db:+.1f} dB" for x in acc)
                                                        if acc else "none"))
    print(f"\nTo see it beside headphones: flatiron compare HEADPHONE ... {prof.read()['label']}")


def _correction_of(f) -> float:
    if f.veto is not None:
        return 0.0 if not f.accepted else f.veto.correction_db
    return -f.peak_db * f.weight if f.correction_db is None else f.correction_db


def cmd_compare(args):
    from flatiron import EARS as ALL_EARS
    from flatiron.fit.compare import Member
    from flatiron.fit.consensus import KINDS_VOTING
    from flatiron.fit.run import NotEnoughTrials, perceptual_fits, profile_session_ids
    from flatiron.paths import data_dir
    from flatiron.report.compare import MAX_PROFILES, compare, print_comparison, write_comparison

    if len(args.profiles) < 2:
        print("compare needs at least two profiles.")
        return 1
    if len(args.profiles) > MAX_PROFILES:
        print(f"compare takes at most {MAX_PROFILES} profiles at once.")
        return 1
    profiles = []
    for name in args.profiles:
        found = _named(name)
        if found is None:
            return 1
        if found[1] is None:
            print(f"No profile {name!r}. See: flatiron profiles")
            return 1
        profiles.append(found[1])
    # Profiles from different listeners are named with theirs.
    from flatiron.session.store import listener_of

    mixed = len({listener_of(p).slug for p in profiles}) > 1
    name_of = lambda p: f"{listener_of(p).slug}/{p.read()['label']}" if mixed else p.read()["label"]

    from itertools import combinations

    from flatiron.fit.run import shares_trials

    for a, b in combinations(profiles, 2):
        if shares_trials(a, b):
            print(f"Warning: {name_of(a)} and {name_of(b)} share trials through pooling, so they'll "
                  "agree more than separate measurements would.")
    ears = ALL_EARS if args.ear == "both" else (args.ear,)
    counted = tuple(KINDS_VOTING) + tuple(args.include_kind or ())
    announce = [lambda: print("Fitting the new trials (a few seconds)...", flush=True)]

    def on_fit():
        if announce:
            announce.pop()()

    if args.broad_spread is not None and args.broad_spread < 0:
        print("--broad-spread can't be negative.")
        return 1
    try:
        members = [Member(name_of(p), p.kind, perceptual_fits(p, profile_session_ids(p), on_fit)[0])
                   for p in profiles]
        comparisons = compare(members, ears, counted, args.broad_spread)
    except (NotEnoughTrials, ValueError) as e:
        print(e)
        return 1
    print_comparison(comparisons, members)
    path = write_comparison(data_dir() / "cache" / "comparison.html", comparisons, members)
    print(f"report: {path}")
    if not args.no_html:
        import webbrowser

        webbrowser.open(path.as_uri())
    return 0


def cmd_report(args):
    prof = _profile(args)
    if prof is None:
        return 1
    pf = _current(prof)
    if pf is None:
        return 1
    if prof.kind == "speaker":
        _print_speaker(prof, pf)
    else:
        _print_fit(prof, pf)
    if args.csv:
        from flatiron.report.compare import write_csv

        write_csv(args.csv, pf)
        print(f"csv: {args.csv}")
    from flatiron.report.html import write_report

    path = write_report(prof, pf)
    print(f"report: {path}")
    if not args.no_html:
        import webbrowser

        webbrowser.open(path.as_uri())
    return 0


def _merge_files(specs):
    """--merge: FILE for both channels, or L=FILE and R=FILE. {ear: file}, or
    None after printing why not."""
    files, both = {}, []
    for spec in specs:
        ear, sep, path = spec.partition("=")
        if sep and ear in EARS:
            if ear in files:
                print(f"--merge gives the {ear} channel twice.")
                return None
            files[ear] = path
        else:
            both.append(spec)
    if both and (files or len(both) > 1):
        print("--merge takes one FILE for both channels, or L=FILE and R=FILE.")
        return None
    if both:
        return {e: both[0] for e in EARS}
    if set(files) != set(EARS):
        print("Merging needs a preference preset for both channels: --merge FILE, or --merge L=FILE --merge R=FILE.")
        return None
    return files


def cmd_export(args):
    from pathlib import Path

    from flatiron.apo import parse_apo
    from flatiron.export import apo, easyeffects, lsp, pipewire, tincan
    from flatiron.export.plan import build_plan, response_db

    prof = _profile(args)
    if prof is None or not _not_speaker(prof, "export"):
        return 1
    pref = None
    if args.merge:
        files = _merge_files(args.merge)
        if files is None:
            return 1
        try:
            pref = {e: parse_apo(Path(f).read_text()) for e, f in files.items()}
        except (ValueError, OSError) as e:
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
    from flatiron.session.store import listener_of, several_listeners

    label, slug = prof.read()["label"], prof.slug
    if several_listeners():  # two people's presets for the same headphones mustn't collide
        who = listener_of(prof)
        label, slug = f"{who.label} {label}", f"{who.slug}-{slug}"
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
        out = Path(f"{slug}-{plan.fit_hash}{'-merged' if plan.merged else ''}{ext}")
    if not out.suffix:
        out = out.with_suffix(ext)
    if not out.parent.is_dir():
        print(f"Can't write {out}: the folder {out.parent} doesn't exist.")
        return 1
    written = []
    try:
        if args.target == "pipewire":
            from flatiron.export import verify

            if not verify.pipewire_available():
                print("Not checked: PipeWire isn't running (or pw-play/pw-record are missing).")
            else:
                import tempfile

                print("Checking through a private PipeWire filter-chain (a few seconds; nothing plays on your "
                      "devices)...", flush=True)
                with tempfile.TemporaryDirectory() as tmp:
                    err = verify.check_pipewire(plan, tmp)
                worst = max(err.values())
                print(f"Measured through a private PipeWire filter-chain: max error {worst:.2f} dB (tolerance 0.3)")
                if worst > 0.3:
                    print("Not writing it: PipeWire doesn't do what Flatiron predicts.")
                    return 1
            out.write_text(pipewire.write(plan, label, slug))
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
    from flatiron.sim.consensus import CONFIGS, check_corrections, consensus_coverage, headphone_fits

    fits = headphone_fits(args.seed, trials_per_session=max(args.trials_per_ear // 3, 1))
    for n, config in enumerate(CONFIGS):
        print(("\n" if n else "") + f"{config.label}:")
        for c in check_corrections(fits, config):
            print(f"  {c.name} (criterion 2): {'ok' if c.ok else 'NOT MET'}")
            for fc, gain, h in c.own:
                print(f"    its own {fc:6.0f} Hz {gain:+.1f} dB: corrected {h:+.1f} dB")
            for fc, h in c.shared:
                print(f"    shared  {fc:6.0f} Hz: corrected {h:+.1f} dB")
            print(f"    largest correction elsewhere {c.worst_false_db:.1f} dB")
    print(f"\nConsensus SD, reported / observed (criterion 10): {consensus_coverage(fits):.2f}")


def _simulate_consensus_speakers(args):
    from flatiron.sim.consensus import SPEAKER_CONFIGS, check_speakers, headphone_fits, speaker_fits

    fits = headphone_fits(args.seed, trials_per_session=max(args.trials_per_ear // 3, 1))
    print("Simulating two speakers, and speaker 1 again with its peak on the cup (15 positions x 32 trials each)...",
          flush=True)
    speakers = speaker_fits(args.seed)
    for config in SPEAKER_CONFIGS:
        print(f"\n{config.label} (criterion {config.criterion}):")
        for c in check_speakers(fits, speakers, config):
            print(f"  {c.name}: {'ok' if c.ok else 'NOT MET'}; cup 9500 Hz corrected {c.cup:+.1f} dB; "
                  f"largest correction elsewhere {c.worst_false_db:.1f} dB; "
                  f"largest change from headphones only {c.vs_headphones_only_db:.1f} dB")
            for fc, gain, h in c.own:
                print(f"    its own {fc:6.0f} Hz {gain:+.1f} dB: corrected {h:+.1f} dB")
            for fc, h in c.shared:
                print(f"    listener {fc:5.0f} Hz: corrected {h:+.1f} dB")


def _simulate_seating(args):
    from flatiron.sim.seating import coverage, moving_resonance, speed, tau_recovery

    for tau in (0.0, 2.0, 3.0):
        f = tau_recovery((tau, tau), args.seed)
        print(f"True spread {tau:g} dB (criterion 9): estimated {f.tau_lo:.2f}, {f.tau_mid:.2f} and {f.tau_hi:.2f} at 5, 8 and 12 kHz")
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


def _simulate_speaker(args):
    from flatiron.sim.speaker import criterion_7

    r = criterion_7(args.seed)
    print(f"Speaker, 15 positions x 32 trials, noise known (criterion 7): RMS error {r.rms:.2f} dB, "
          f"1 SD covers {r.coverage:.0%}; with the position spread assumed at half: {r.rms_half:.2f} dB, "
          f"{r.coverage_half:.0%}")


def cmd_simulate(args):
    if args.scenario == "speaker":
        return _simulate_speaker(args)
    if args.scenario == "consensus":
        return _simulate_consensus(args)
    if args.scenario == "consensus-speakers":
        return _simulate_consensus_speakers(args)
    if args.scenario == "seating":
        return _simulate_seating(args)
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.sim.listener import SimulatedListener, design_example
    from flatiron.sim.run import feature_depth, run, score, EVAL_GRID

    lis = SimulatedListener({e: design_example(e) for e in EARS}, sigma0=args.sigma0,
                            lapse=args.lapse, seed=args.seed)
    cfg = SchedulerConfig(adaptive=args.design == "adaptive")
    sch, fits = run(lis, args.trials_per_ear, cfg, seed=args.seed + 1)
    for ear in EARS:
        fit, sc = fits[ear], score(lis, ear, fits[ear])
        n_catch = sum(1 for a, b in zip(sch.ears[ear].f_a, sch.ears[ear].f_b) if a == b)
        print(f"ear {ear}: {args.trials_per_ear} trials ({n_catch} catch, {args.design}), "
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

    def profile_arg(s, help="profile name, as LISTENER/PROFILE when there are several listeners "
                             "(default: the only profile)"):
        s.add_argument("profile", nargs="?", metavar="PROFILE", help=help)

    def device_arg(s):
        s.add_argument("-d", "--device", metavar="INDEX|NAME",
                       help="output device: an index from `flatiron devices`, or part of its name")
        s.add_argument("-n", "--dry-run", action="store_true", help="no audio")

    s = sub.add_parser("devices", help="list output devices and flag problems")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("tone", help="tone generator: glide through frequencies by hand, mark peaks and dips")
    profile_arg(s, "apply this profile's correction (Q toggles it) and save marks to it")
    device_arg(s)
    s.add_argument("-f", "--freq", type=_hz, default=1000.0, help="starting frequency (Hz, or kHz with a k)")
    s.add_argument("-e", "--ear", choices=("L", "R", "both"), default="L")
    s.set_defaults(func=cmd_tone)

    def session_args(s):
        profile_arg(s)
        device_arg(s)
        s.add_argument("-m", "--minutes", type=float, default=12.0, help="session length, at most 15")
        s.add_argument("--stimulus", type=_stimulus, default=StimulusSpec(), metavar="noise[:N]|warble[:N]|tone",
                       help="noise:N, a 1/N-octave noise band (default noise:12); warble:N, a tone swinging "
                            "over 1/N octave (default 12); tone, a pure tone")
        s.add_argument("--active-eq", metavar="APO_TXT",
                       help="system EQ in effect while measuring (Equalizer APO / AutoEQ text)")
        s.add_argument("--eq-note", help="free-text note on the EQ context")
        s.add_argument("--no-match", dest="match", action="store_false",
                       help="play every burst at the set level instead of matching their loudness to your "
                            "answers so far (matching keeps quiet regions audible without the loud ones "
                            "getting uncomfortable)")
        s.add_argument("--max-match-boost", type=float, default=12.0, metavar="DB",
                       help="loudness matching: the most a quiet region is raised above the set level "
                            "(0 to 24 dB, default 12)")

    s = sub.add_parser("run", help="run forced-choice sessions")
    session_args(s)
    s.add_argument("--new", action="store_true", help="start a new profile with this name")
    s.add_argument("-s", "--sweep", action="store_true",
                   help="start with the coarse sweep, through the current correction if there is one")
    s.add_argument("--sweep-seconds", type=float, help="sweep length each way (default 30)")
    s.add_argument("--block", type=int, metavar="N",
                   help="speaker profiles: trials per block, one position each (16 to 60, default 32)")
    s.add_argument("--debug", action="store_true",
                   help="D during trials shows each burst's frequency and level (answers given while it "
                        "shows are marked in the session file)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("verify", help="short session with the current correction applied to every burst")
    session_args(s)
    s.add_argument("--trials", type=int, default=150, help="answers in the session, both ears (default 150)")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("audition", help="listen with the current correction on and off")
    profile_arg(s)
    device_arg(s)
    s.add_argument("--source", action="append", metavar="pink|sweep|FILE.wav",
                   help="what to play (repeatable; default: pink noise and the slow sweep)")
    s.set_defaults(func=cmd_audition)

    s = sub.add_parser("listeners", help="list listeners, add, rename or delete one")
    s.set_defaults(func=cmd_listeners)
    actions = s.add_subparsers(dest="action")
    n = actions.add_parser("new", help="add a listener")
    n.add_argument("name")
    n = actions.add_parser("rename", help="rename a listener (its folder, and how its profiles are named)")
    n.add_argument("name")
    n.add_argument("new")
    n = actions.add_parser("delete", help="delete a listener and all their profiles and sessions (asks first)")
    n.add_argument("name")
    n.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    s = sub.add_parser("profiles", help="list headphone profiles, add, change, rename, move or delete one")
    s.set_defaults(func=cmd_profiles)
    actions = s.add_subparsers(dest="action")
    n = actions.add_parser("new", help="create a profile")
    n.add_argument("name")
    n.add_argument("--kind", choices=("headphone", "iem", "speaker"), default="headphone",
                   help="IEMs and speakers are shown in comparisons but don't count toward a consensus")
    n.add_argument("--channel", choices=("L", "R"), help="speakers: the output channel that drives the speaker")
    n = actions.add_parser("set", help="change a profile's kind or label (no trials affected)")
    n.add_argument("name")
    n.add_argument("--kind", choices=("headphone", "iem", "speaker"))
    n.add_argument("--label")
    n = actions.add_parser("move", help="move a profile to another listener")
    n.add_argument("name")
    n.add_argument("--to", required=True, metavar="LISTENER")
    n = actions.add_parser("rename", help="rename a profile (its label and folder; references to it follow)")
    n.add_argument("name")
    n.add_argument("new")
    n = actions.add_parser("delete", help="delete a profile and all its sessions (asks first)")
    n.add_argument("name")
    n.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    s = sub.add_parser("sessions", help="list a profile's sessions, or delete one")
    profile_arg(s)
    s.add_argument("--delete", metavar="SESSION_ID", help="delete a session (e.g. one left open by a crash)")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("config", help="show or change a profile's fit settings, or a listener's defaults; "
                                      "checkpoint a profile's filters")
    profile_arg(s, help="profile name (LISTENER/PROFILE when there are several listeners; default: the only "
                          "profile), or, for the listener settings, a listener (default: the only one)")
    for flags, f, kw, *_ in FIT_OPTIONS:
        if not ({"metavar", "choices", "action"} & set(kw)):
            kw = dict(kw, metavar=_option_name(flags).upper().replace("-", "_"))
        s.add_argument(*flags, dest=f, default=None, **kw)
    s.add_argument("--sessions", metavar="ID|PROFILE[,...]",
                   help="what the fits use: session ids, and profile names for all of a profile's sessions "
                        "(new ones included). Another profile pools its trials in: the same headphones measured "
                        "with another stimulus, say. Default: all of this profile's")
    s.add_argument("--reset", metavar="SETTING[,SETTING]|all",
                   help="back to the default: the named settings (e.g. width,broad-strength), sessions, checkpoint, "
                        "or all (every setting and the sessions, not the checkpoint). Not with other changes")
    s.add_argument("--checkpoint", action="store_true",
                   help="keep the current filters fixed from now on; later fits only add filters "
                        "(e.g. before a verify session)")
    s.add_argument("--min-measured", dest="min_measured", type=_hz, metavar="HZ",
                   help="this profile's lowest trial frequency (Hz, or kHz with a k), in place of the listener's "
                        "default. Earlier trials outside the range still count; the correction range follows it "
                        "unless it's set itself")
    s.add_argument("--max-measured", dest="max_measured", type=_hz, metavar="HZ",
                   help="this profile's highest trial frequency, in place of the listener's default")
    s.add_argument("--default-min-measured", dest="default_min_measured", type=_hz, metavar="HZ",
                   help="listener setting (give a listener, or none for the only one): the lowest trial frequency "
                        "for all their profiles (default 850)")
    s.add_argument("--default-max-measured", dest="default_max_measured", type=_hz, metavar="HZ",
                   help="listener setting: the highest trial frequency for all their profiles (default 15k); "
                        "lower it if the top of the range is hard to hear")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("report", help="the current fit: filters and features, and the HTML report with figures")
    profile_arg(s)
    s.add_argument("-H", "--no-html", action="store_true",
                   help="don't open the HTML report in a browser (it's still written)")
    s.add_argument("--csv", metavar="FILE", help="dump the level, reference, baseline and residual with SDs, per ear")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("compare", help="compare profiles per ear: their curves, narrow parts, and where they differ most")
    s.add_argument("profiles", nargs="+", metavar="PROFILE", help="two to eight profiles")
    s.add_argument("-H", "--no-html", action="store_true",
                   help="don't open the HTML comparison in a browser (it's still written)")
    s.add_argument("--ear", choices=("L", "R", "both"), default="both", help="which ear")
    s.add_argument("--include-kind", action="append", choices=("iem", "speaker"),
                   help="count this kind in the alignment, the spread and the strips (it's drawn dashed either way; "
                        "with fewer than two profiles of counted kinds, all of them count)")
    s.add_argument("--broad-spread", type=float, metavar="DB",
                   help="shade where the broad parts (tonality) span more than this between profiles (default 4 dB)")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("export", help="write the current fit's correction for an EQ host")
    profile_arg(s)
    s.add_argument("-t", "--target", choices=("apo", "lsp", "easyeffects", "pipewire", "tincan"), required=True,
                   help="apo: Equalizer APO / AutoEQ text per channel (also PipeWire param_eq files); "
                        "lsp: LSP Parametric Equalizer x32 LeftRight settings; "
                        "easyeffects: EasyEffects preset, the format Equaliser (macOS) imports; "
                        "pipewire: filter-chain drop-in for ~/.config/pipewire/pipewire.conf.d/, measured through "
                        "a private filter-chain first when PipeWire is running; "
                        "tincan: TinCan preset, saved straight into TinCan's presets")
    s.add_argument("--merge", action="append", metavar="FILE|L=FILE|R=FILE",
                   help="preference preset (APO text) to merge in front: one FILE for both channels, or "
                        "--merge L=FILE --merge R=FILE")
    s.add_argument("--preamp", metavar="safe|DB",
                   help="safe (default): enough headroom that no boost can clip; or a gain in dB, "
                        "e.g. 0 to keep the level of the uncorrected sound for A/B comparison "
                        "(not for tincan, which sets its own)")
    s.add_argument("-o", "--output", metavar="FILE",
                   help="output file (apo: FILE_L.txt and FILE_R.txt); default: <profile>-<fit>.cfg/.txt "
                        "in the current directory; tincan: TinCan's preset folder")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("simulate", help="fit a simulated listener and report recovery")
    s.add_argument("--trials-per-ear", type=int, default=600)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--sigma0", type=float, default=1.0, help=argparse.SUPPRESS)
    s.add_argument("--lapse", type=float, default=0.03, help=argparse.SUPPRESS)
    s.add_argument("--design", choices=("adaptive", "uniform"), default="adaptive")
    s.add_argument("--scenario", choices=("consensus", "consensus-speakers", "seating", "speaker"),
                   help="consensus: three headphones on one listener; consensus-speakers: the same with two "
                        "speakers in the consensus; seating: sessions that differ; speaker: speaker blocks at "
                        "positions that differ")
    s.set_defaults(func=cmd_simulate)

    args = p.parse_args(argv)
    if getattr(args, "device", None) is not None:
        try:
            args.device = None if args.dry_run else _device(args.device)
        except ValueError as e:
            print(e)
            return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
