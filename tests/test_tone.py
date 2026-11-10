import numpy as np
import pytest

from flatiron.apo import ApoFilter
from flatiron.audio.preview import PreviewEQ
from flatiron.audio.synth import STIMULI, ToneSynth
from flatiron.session.store import Profile
from flatiron.ui.tone import Glide, ToneApp, parse_freq

FS = 48_000
EQ = PreviewEQ({"L": [ApoFilter("PK", 4000, -12, 2)], "R": []}, -1.0, "abc")


def run(synth, seconds, block=256):
    return np.concatenate([synth.render(block) for _ in range(int(seconds * FS / block))])


def ramps(x, full):
    """True if x's envelope (peak per 1 ms) never moves by more than a 10 ms
    ramp would in 1 ms (plus slack): faded, not cut."""
    env = np.abs(x[: x.size // 48 * 48]).reshape(-1, 48).max(axis=1)
    return np.max(np.abs(np.diff(env))) < 0.15 * full


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(x**2)))


@pytest.mark.parametrize("stim", range(len(STIMULI)))
def test_every_stimulus_plays_at_the_set_level(stim):
    s = ToneSynth(freq=3000, level_db=-30, stimulus=stim, ear="L")
    run(s, 0.2)
    y = run(s, 1.0)
    assert rms_db(y[:, 0]) == pytest.approx(-30, abs=0.6)
    assert not y[:, 1].any()


def test_glide_is_smooth():
    s = ToneSynth(freq=1000, level_db=-20, ear="L")
    run(s, 0.1)
    s.freq = 2000
    y = run(s, 0.2)[:, 0]
    # A jump in frequency glides: no sample-to-sample step bigger than the
    # fastest the target tone could move.
    amp = np.sqrt(2) * 10 ** (-20 / 20)
    assert np.max(np.abs(np.diff(y))) < amp * 2 * np.pi * 2000 / FS * 1.05
    n = 4800
    tail = y[-n:]
    f = np.argmax(np.abs(np.fft.rfft(tail * np.hanning(n)))) * FS / n
    assert f == pytest.approx(2000, abs=15)


def test_ear_eq_pause_and_stop_ramp():
    s = ToneSynth(freq=4000, level_db=-30, ear="L", eq=EQ)
    run(s, 0.1)
    on = rms_db(run(s, 0.3)[:, 0])
    assert on == pytest.approx(-42, abs=0.5)  # through the -12 dB filter, no preamp
    s.eq_on = False
    run(s, 0.05)
    assert rms_db(run(s, 0.3)[:, 0]) == pytest.approx(-30, abs=0.3)
    s.ear = "R"
    y = run(s, 0.05)
    full = np.sqrt(2) * 10 ** (-30 / 20)
    assert ramps(y[:, 0], full) and ramps(y[:, 1], full) and not y[-480:, 0].any()
    y = run(s, 0.2)
    assert not y[:, 0].any() and rms_db(y[:, 1]) == pytest.approx(-30, abs=0.3)
    s.paused = True
    run(s, 0.05)
    assert not run(s, 0.1).any()
    s.paused = False
    s.stop()
    run(s, 0.05)
    assert not s.playing


def test_stimulus_change_fades_out_and_in():
    s = ToneSynth(freq=2000, level_db=-20, ear="L")
    run(s, 0.1)
    s.stimulus = 3
    y = run(s, 0.1)[:, 0]
    full = np.sqrt(2) * 0.1
    gap = np.flatnonzero(y == 0)
    assert gap.size and ramps(y[: gap[0]], full)  # the tone fades out
    after = y[gap[-1] + 1:]
    assert np.abs(after[:96]).max() < 0.3 * full  # the noise fades in
    assert rms_db(run(s, 0.5)[:, 0]) == pytest.approx(-20, abs=0.6)


def drive(g, presses, until, size=1 / 24, dt=1 / 60):
    """Tick g at dt to time until, pressing at each (time, direction) on the
    way; return the speed and position at each tick."""
    presses, t, pos, speeds, path = sorted(presses), 0.0, 0.0, [], []
    while t < until:
        while presses and presses[0][0] <= t:
            g.press(presses.pop(0)[1], t, size)
        speeds.append(g.speed())
        pos += g.advance(dt)
        path.append(pos)
        t += dt
    return np.array(speeds), np.array(path)


def test_glide_tap_moves_at_once_by_half_a_tap():
    v, x = drive(Glide(), [(0.0, 1)], 1.0)
    assert v[0] == pytest.approx(1 / 24 / Glide.W)  # the speed jumps on the press
    assert x[-1] == pytest.approx(1 / 48) and not v[-1]


def test_glide_speed_follows_tap_rate_smoothly():
    for rate in (3, 6, 9):
        v, x = drive(Glide(), [(i / rate, 1) for i in range(int(4 * rate))], 5.0)
        steady = v[90:210]  # 1.5 to 3.5 s
        assert steady.mean() == pytest.approx(rate / 24, rel=0.05)
        assert np.ptp(steady) < 0.2 * steady.mean()  # no jolt per tap
        assert v[-1] == 0 and x[-1] == pytest.approx(4 * rate / 24, rel=0.1)  # stopped, a tap size per tap


def test_glide_auto_repeat_is_capped():
    # A held key: one press, the keyboard's delay, then repeats every 33 ms.
    taps = [(0.0, 1)] + [(0.5 + i / 30, 1) for i in range(90)]
    v, _ = drive(Glide(), taps, 3.5)
    assert v[int(2.5 * 60)] == pytest.approx(Glide.MAX_RATE / 24, rel=0.05)


def test_glide_opposite_press_freezes_then_moves():
    g = Glide()
    _, x = drive(g, [(i / 6, 1) for i in range(12)], 2.0)
    g.press(-1, 2.0, 1 / 24)
    assert g.speed() == 0 and g.advance(0.1) == 0 and not g.pending
    g.press(-1, 2.1, 1 / 24)
    assert g.speed() < 0


def test_parse_freq():
    assert parse_freq("6150") == 6150 and parse_freq("6.15k") == 6150 and parse_freq("12,000 Hz") == 12000
    for bad in ("x", "5", "30k"):
        with pytest.raises(ValueError):
            parse_freq(bad)


async def test_tone_app_marks_go_to_the_profile(tmp_path):
    prof = Profile.open("t", root=tmp_path)
    app = ToneApp(4000, "L", -30, eq=EQ, profile=prof, dry_run=True)
    async with app.run_test() as pilot:
        await pilot.press("right")
        await pilot.pause(0.6)
        assert app.synth.freq == pytest.approx(4000 * 2 ** (1 / 48))  # half a 1/24-octave tap
        await pilot.press("e", "e", "p", "s", "d", "u", "d")  # both ears; peak; noise; dip, undone; dip
        await pilot.press("g")
        await pilot.pause()
        await pilot.press(*"7k", "enter")
        await pilot.pause()
        assert app.synth.freq == 7000
        await pilot.press("q", "minus")
        await pilot.pause()
        assert not app.synth.eq_on and app.synth.level_db == -31
    marks = prof.marks()
    assert [(m["ear"], m["kind"], m["session"]) for m in marks] == [("LR", "peak", None), ("LR", "dip", None)]
    assert marks[0]["stimulus"] == "point" and marks[1]["stimulus"].startswith("warble")


def test_marks_refine_the_next_session_once(tmp_path):
    from flatiron.session.runner import RunSettings, TrialRunner
    from flatiron.session.scheduler import SchedulerConfig

    cfg = SchedulerConfig(min_trials_noise_fit=10_000)
    prof = Profile.open("t", root=tmp_path)
    prof.add_mark("R", 6150.0, "peak", "point")
    r = TrialRunner(prof, RunSettings(), cfg, seed=0)
    base = TrialRunner(Profile.open("u", root=tmp_path), RunSettings(), cfg, seed=0)
    assert r.scheduler.ears["R"].grid.size > base.scheduler.ears["R"].grid.size
    assert r.scheduler.ears["L"].grid.size == base.scheduler.ears["L"].grid.size
    assert r.session.tone_marks[0]["freq"] == 6150.0
    # Left before any trials: the session is deleted, so the mark is pending again.
    r.finish()
    r3 = TrialRunner(prof, RunSettings(), cfg, seed=0)
    assert r3.session.tone_marks and prof.marks()[0]["session"] == r3.session.id
    # r3's session is on disk (saved from the start), so the next one doesn't refine again.
    assert TrialRunner(prof, RunSettings(), cfg, seed=0).session.tone_marks == []
