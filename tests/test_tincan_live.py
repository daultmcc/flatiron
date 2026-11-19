"""TinCan's EQ read from its state and presets (DESIGN.md, "Measurement context")."""

import os

import numpy as np
import pytest

from flatiron.audio.biquad import peaking_coeffs, sos_response_db
from flatiron.session.centring import CentringRunner, CentringSettings
from flatiron.session.runner import RunSettings, TrialRunner
from flatiron.session.store import Profile
from flatiron.session.threshold import ThresholdRunner, ThresholdSettings
from flatiron.tincan_live import TinCanWatch, follows_default_sink, read_eq

BELL = """format = 1
name = "{name}"

[[filter]]
type = "bell"
channel = "{ch}"
frequency = {f}
gain = {g}
q = 1.0
enabled = true

[[filter]]
type = "high_shelf"
channel = "LR"
frequency = 8000.0
gain = 9.0
q = 0.7071
enabled = false
"""


def state(path, a=(("one", True),), b=(), bypass=False, ab="a", mode="match_bypass", manual=0.0):
    lines = [f"bypass = {str(bypass).lower()}", f'preamp_mode = "{mode}"', f"manual_preamp_db = {manual}",
             f'ab = "{ab}"']
    for key, items in (("loaded", a), ("loaded_b", b)):
        for i, on in items:
            lines += ["", f"[[{key}]]", f'id = "{i}"', f"active = {str(on).lower()}"]
    path.write_text("\n".join(lines) + "\n")
    # A new mtime even within the filesystem's resolution.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


@pytest.fixture
def tc(tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "one.toml").write_text(BELL.format(name="One", ch="L", f=2000.0, g=6.0))
    (presets / "two.toml").write_text(BELL.format(name="Two", ch="LR", f=5000.0, g=-4.0))
    st = tmp_path / "state.toml"
    state(st)
    return st, presets


def test_reads_the_heard_list_and_its_active_presets(tc):
    st, presets = tc
    eq = read_eq(st, presets, 48000)
    assert eq.names == ("One",)
    # Left only; the disabled shelf is left out; match-bypass preamp: −(power average, both channels).
    assert eq.gain_db([2000.0], "L")[0] == pytest.approx(6.0 + eq.preamp_db, abs=1e-6)
    assert eq.gain_db([2000.0], "R")[0] == pytest.approx(eq.preamp_db, abs=1e-9)
    assert -3 < eq.preamp_db < 0
    state(st, a=(("one", True), ("two", False)), b=(("two", True),), ab="b", mode="safe")
    eq = read_eq(st, presets, 96000)
    assert eq.names == ("Two",) and eq.rate == 96000
    assert eq.preamp_db == 0.0  # safe: the response has no boost
    state(st, a=(("one", True), ("two", True)), mode="manual", manual=-3.5)
    eq = read_eq(st, presets, 48000)
    assert eq.names == ("One", "Two") and eq.preamp_db == -3.5
    ref = sos_response_db(np.vstack([peaking_coeffs(2000.0, 6.0, 1.0, 48000), peaking_coeffs(5000.0, -4.0, 1.0, 48000)]),
                          np.array([3000.0]), 48000)[0]
    assert eq.gain_db([3000.0], "L")[0] == pytest.approx(ref - 3.5, abs=1e-9)
    state(st, bypass=True)
    assert read_eq(st, presets, 48000) is None
    state(st, a=(("one", False),))
    assert read_eq(st, presets, 48000) is None


def test_band_gain_averages_over_the_burst(tc):
    st, presets = tc
    eq = read_eq(st, presets, 48000)
    centre = eq.gain_db([2000.0], "L")[0]
    assert eq.band_gain_db([2000.0], "noise:1", "L")[0] < centre - 0.5  # an octave band spreads past the bell
    assert eq.band_gain_db([2000.0], "noise:0.083333", "L")[0] == pytest.approx(centre, abs=0.2)


def test_watch_follows_changes_and_only_the_default_sink(tc):
    st, presets = tc
    rate = {"now": 48000}
    w = TinCanWatch({"name": "default"}, rate=lambda: rate["now"], state_path=st, presets=presets)
    assert w.start().names == ("One",)
    assert w.poll_change() == (False, w.eq)
    state(st, bypass=True)
    assert w.poll_change() == (True, None)
    (presets / "two.toml").write_text(BELL.format(name="Two", ch="LR", f=5000.0, g=-4.0))
    state(st, a=(("two", True),))
    changed, eq = w.poll_change()
    assert changed and eq.names == ("Two",)
    # An edit to a loaded preset's file counts too.
    (presets / "two.toml").write_text(BELL.format(name="Two", ch="LR", f=5000.0, g=-8.0))
    os.utime(presets / "two.toml", ns=(0, st.stat().st_mtime_ns + 10**9))
    assert w.poll_change()[0] and w.eq.gain_db([5000.0], "R")[0] < -6
    rate["now"] = None  # TinCan quit, or isn't the default sink
    assert w.poll_change() == (True, None)
    assert w.note().startswith("TinCan: One → TinCan off → TinCan: Two → TinCan: Two → TinCan off")
    assert len(w.context()["states"]) == 5
    assert not follows_default_sink({"name": "Topping DX1: USB Audio (hw:2,0)"})
    assert TinCanWatch({"name": "hw:2,0"}, rate=lambda: 48000, state_path=st, presets=presets).start() is None


def watch(tc):
    st, presets = tc
    return TinCanWatch({"name": "default"}, rate=lambda: 48000, state_path=st, presets=presets)


def test_threshold_runner_aims_through_and_records_the_eq(tmp_path, tc):
    w = watch(tc)
    r = ThresholdRunner(Profile.open("x", root=tmp_path), ThresholdSettings(tincan=w), seed=1)
    r.startup = {"L": None, "R": None}
    eq = w.eq
    for k in range(40):
        tr = r.prepare()
        p = tr.plan
        g = float(eq.band_gain_db([p.freq], r.key, p.ear)[0])
        assert p.eq_gain == pytest.approx(g, abs=1e-3)
        rec = r.answer(tr, first=None, rt_ms=500, t=k)
        assert rec.eq_gain == p.eq_gain
    # The bursts aim at the estimate of what reaches the ear: EQ gain added back.
    assert any(abs(t.eq_gain) > 1 for t in r.session.trials)
    ctx = r.session.eq_context
    assert ctx["note"].startswith("TinCan: One") and ctx["tincan"][0]["presets"] == ["One"]
    assert "one" in ctx["tincan"][0]["files"]


def test_centring_and_trial_runners_record_the_eq(tmp_path, tc):
    w = watch(tc)
    c = CentringRunner(Profile.open("x", root=tmp_path), CentringSettings(tincan=w), seed=0,
                       curves={"L": None, "R": None})
    tr = c.prepare()
    rec = c.answer(tr, True, 500)
    eq = w.eq
    key = c.key
    assert rec.eq_gain_l == pytest.approx(float(eq.band_gain_db([rec.freq], key, "L")[0]), abs=1e-3)
    assert rec.eq_gain_r == pytest.approx(eq.preamp_db, abs=1e-3)  # the bell is in the left channel only
    assert c.session.eq_context["note"].startswith("TinCan: One")

    t = TrialRunner(Profile.open("y", root=tmp_path), RunSettings(tincan=w), seed=0)
    tr = t.prepare()
    rec = t.answer(tr, first_louder=True, rt_ms=400)
    p = tr.plan
    assert rec.eq_gain_a == pytest.approx(float(eq.band_gain_db([p.f_a], t.tincan.stimulus, p.ear[0])[0]), abs=1e-3)
    assert t.session.eq_context["note"].startswith("TinCan: One")
    assert t.eq_message() is None
    state(tc[0], bypass=True)
    assert t.eq_message().startswith("TinCan is off now")


async def test_notice_before_the_session_then_toast_on_a_change(tmp_path, tc):
    from flatiron.audio.player import NullPlayer
    from flatiron.ui.app import ChannelCheckScreen, FlatironApp, ThresholdIntroScreen, TinCanNoticeScreen

    w = watch(tc)
    prof = Profile.open("x", root=tmp_path)
    app = FlatironApp(lambda: ThresholdRunner(prof, ThresholdSettings(tincan=w), seed=0),
                      NullPlayer())
    async with app.run_test() as pilot:
        assert isinstance(app.screen, TinCanNoticeScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ThresholdIntroScreen)
        await pilot.press("escape")
        await pilot.pause()
    assert prof.threshold_sessions() == []
    # Esc on the notice keeps nothing either.
    app = FlatironApp(lambda: ThresholdRunner(prof, ThresholdSettings(tincan=w), seed=0), NullPlayer())
    async with app.run_test() as pilot:
        assert isinstance(app.screen, TinCanNoticeScreen)
        await pilot.press("escape")
        await pilot.pause()
    assert prof.threshold_sessions() == []
    # Off at the start: no notice.
    state(tc[0], bypass=True)
    app = FlatironApp(lambda: ThresholdRunner(prof, ThresholdSettings(tincan=w), seed=0), NullPlayer())
    async with app.run_test() as pilot:
        assert isinstance(app.screen, ThresholdIntroScreen)
        await pilot.press("enter", "enter")  # introduction, level
        await pilot.pause()
        assert isinstance(app.screen, ChannelCheckScreen)
