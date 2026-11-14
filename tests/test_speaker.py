"""Speaker profiles (DESIGN.md, "Speaker profiles"): the position plan, the
block runner, the position model (criterion 7) and the commands."""

import numpy as np
import pytest

from flatiron.cli import main
from flatiron.session.speaker import CHUNK, Position, next_block, plan, plan_key, progress


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("FLATIRON_LISTENER", raising=False)
    return tmp_path / "flatiron"


def test_the_plan_switches_ears_every_chunk_one_height_at_a_time():
    p = plan()
    assert len(p) == 30 and len(set(p)) == 30
    chunks = [p[i:i + CHUNK] for i in range(0, len(p), CHUNK)]
    assert [c[0].ear for c in chunks] == ["L", "R"] * 3
    for c in chunks:  # one ear and one height, the five directions
        assert len({(x.ear, x.elevation) for x in c}) == 1 and sorted(x.azimuth for x in c) == [0, 30, 60, 90, 135]
    assert [c[0].elevation for c in chunks[::2]] == [0, 30, -30]
    # Shuffled within each chunk, the same way every time for one profile.
    assert plan("a") == plan("a") and plan("a") != plan("b")
    assert len({tuple(x.azimuth for x in c) for c in chunks}) > 1


def test_criterion_7_the_average_over_positions_is_recovered():
    from flatiron.sim.speaker import criterion_7

    for seed in (0, 1):
        r = criterion_7(seed)
        assert r.rms < 1.5 and 0.5 < r.coverage < 0.95
        assert r.coverage_half <= r.coverage + 0.05  # a smaller assumed spread can only narrow the bands


def _speaker(data, blocks=6, **kw):
    from flatiron.session.store import Profile
    from flatiron.sim.listener import design_example
    from flatiron.sim.speaker import simulate_speaker_profile

    assert main(["profiles", "new", "Phone", "--kind", "speaker", "--channel", "R"]) == 0
    prof = Profile.existing("Phone")
    simulate_speaker_profile(prof, {e: design_example(e) for e in "LR"}, blocks=blocks, **kw)
    return prof


def test_blocks_play_on_the_speaker_channel_and_follow_the_plan(data):
    from flatiron.session.runner import RunSettings, TrialRunner

    prof = _speaker(data, blocks=6)
    sessions = prof.sessions()
    ears = [s.speaker["open_ear"] for s in sessions]  # (simulated blocks share a second, so ids don't order them)
    assert ears.count("L") == 5 and ears.count("R") == 1
    assert all(s.speaker["channel"] == "R" and s.speaker["plugged"] for s in sessions)
    assert sorted(len(s.grid) for s in sessions) == [1] * 6
    assert all({t.ear for t in s.trials} == {s.speaker["open_ear"]} for s in sessions)
    assert progress(sessions) == (6, 30) and next_block(prof) == plan(plan_key(prof))[6]
    # The left ear is open, but the bursts come from the speaker on the right channel.
    speaker = {"open_ear": "L", "channel": "R", "position": Position("L", 0, 30).to_json(), "block": 16}
    r = TrialRunner(prof, RunSettings(speaker=speaker, max_trials=16), seed=1)
    assert list(r.scheduler.ears) == ["L"]
    buf = r.prepare().buffer
    assert np.abs(buf[:, 0]).max() == 0 and np.abs(buf[:, 1]).max() > 0
    r.discard()


def test_a_speakers_fit_holds_the_position_spread_and_skips_the_seating_step(data, capsys):
    import flatiron.fit.run as run_mod

    prof = _speaker(data, blocks=10)
    pf = run_mod.current_fit(prof)
    for e, f in pf.fits.items():
        assert not f.tau_fitted and (f.tau_lo, f.tau_mid, f.tau_hi) == (1.0, 2.1, 4.0) and f.n_groups == 5
        assert pf.correction.ears[e].sessions_checked == 0
    # Speaker trials don't inform the listener's noise.
    assert run_mod.listener_noise(prof.dir.parent.parent)["L"].n_trials == 0
    capsys.readouterr()
    main(["report", "Phone"])
    out = capsys.readouterr().out
    assert "Speaker profile (channel R): 10 of 30 blocks" in out and "PK" not in out and "assumed" in out


def test_speaker_commands(data, capsys, monkeypatch):
    from flatiron.session.store import Profile

    assert main(["profiles", "new", "Phone", "--kind", "speaker"]) == 1 and Profile.existing("Phone") is None
    assert main(["profiles", "new", "HD", "--channel", "L"]) == 1
    prof = _speaker(data, blocks=2)
    capsys.readouterr()
    for cmd in (["export", "Phone", "-t", "apo"], ["verify", "Phone", "-n"], ["audition", "Phone", "-n"]):
        assert main(cmd) == 1 and "speaker profile" in capsys.readouterr().out
    assert main(["run", "Phone", "--sweep", "-n"]) == 1
    assert main(["run", "Phone", "--block", "100", "-n"]) == 1
    launched = []
    monkeypatch.setattr("flatiron.cli._launch", lambda args, **kw: launched.append(kw) or 0)
    assert main(["run", "Phone"]) == 0 and launched[0]["max_trials"] == 32  # the default block
    assert main(["profiles", "set", "Phone", "--kind", "headphone"]) == 1  # it has sessions
    assert main(["profiles"]) is None and "speaker, channel R" in capsys.readouterr().out
    assert main(["sessions", "Phone"]) is None and "° ear level]" in capsys.readouterr().out


def test_blocks_at_the_same_position_share_one_deviation(data):
    import flatiron.fit.run as run_mod

    prof = _speaker(data, blocks=6)
    first, second = plan(plan_key(prof))[:2]
    s = next(x for x in prof.sessions() if x.speaker["position"] == second.to_json())
    s.speaker["position"] = first.to_json()  # relabelled to the first block's position
    prof.save(s)
    trials, _ = prof.history()
    assert trials["L"].group.max() == 3 and len(trials["L"]) == 5 * 16
    pf = run_mod.current_fit(prof)
    assert pf.fits["L"].n_groups == 4 and len(pf.fits["L"].groups) == 4
    assert progress(prof.sessions()) == (5, 30) and next_block(prof) == second
