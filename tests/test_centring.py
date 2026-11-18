"""Centring sessions (DESIGN.md, "Centring sessions")."""

import numpy as np
import pytest
from scipy.special import ndtr

from flatiron.audio.player import NullPlayer
from flatiron.session.centring import CentringRunner, CentringSettings
from flatiron.session.store import Profile
from flatiron.ui.app import CentringIntroScreen, CentringScreen, ChannelCheckScreen, FlatironApp, SessionEndScreen

FLAT = {"L": None, "R": None}


def runner(tmp_path, name="x", seed=0, curves=FLAT, **kw):
    return CentringRunner(Profile.open(name, root=tmp_path), CentringSettings(**kw), seed=seed, curves=curves)


def lvl(x):
    return 20 * np.log10(np.sqrt(np.mean(np.asarray(x, float) ** 2)))


def answer_like(r, n, balance, sigma=1.5, seed=0):
    """A listener for whom the left is `balance` dB louder."""
    rng = np.random.default_rng(seed)
    for i in range(n):
        tr = r.prepare()
        p = tr.plan
        r.answer(tr, bool(rng.random() < ndtr((p.ild_db + balance) / sigma)), 500, t=i)


def test_one_burst_in_both_ears_split_by_the_level_difference(tmp_path):
    r = runner(tmp_path, level_dbfs=-40)
    for _ in range(5):
        tr = r.prepare()
        left, right = tr.buffer[:, 0], tr.buffer[:, 1]
        assert lvl(left) - lvl(right) == pytest.approx(tr.plan.ild_db, abs=0.01)
        # The same waveform: a centred image when the levels match.
        assert np.allclose(left * 10 ** (-tr.plan.ild_db / 20), right, atol=1e-6)
        assert tr.played[0] - tr.played[1] == pytest.approx(tr.plan.ild_db, abs=1e-9)
        assert (lvl(left) + lvl(right)) / 2 == pytest.approx(-40 + tr.plan.rove_db, abs=0.05)
        assert tr.first_off == tr.second_off  # one interval


def test_the_estimate_finds_the_balance_and_carries_over(tmp_path):
    r = runner(tmp_path)
    answer_like(r, 150, balance=3.0)
    assert r.current_estimate == pytest.approx(3.0, abs=0.8)
    # Aimed near the centre point: −c, with jitter.
    late = [t.ild_db for t in r.live_trials()[-50:]]
    assert np.mean(late) == pytest.approx(-3.0, abs=1.0) and np.std(late) > 1.0
    r.finish()
    again = runner(tmp_path, seed=1)
    assert again.current_estimate == pytest.approx(r.current_estimate, abs=1e-6)
    assert Profile.open("x", root=tmp_path).sessions() == []  # kept apart from trial sessions


def test_the_ears_curves_shift_the_aim_and_set_the_matching(tmp_path):
    curves = {"L": lambda f: np.full(np.size(f), 2.0), "R": lambda f: np.full(np.size(f), -1.0)}
    r = runner(tmp_path, curves=curves)
    assert r.shape(3000.0) == pytest.approx(3.0)
    assert r.match_gain(3000.0) == pytest.approx(-0.5)
    plans = [r.next_plan() for _ in range(200)]
    assert np.mean([p.ild_db for p in plans]) == pytest.approx(-3.0, abs=0.4)
    assert runner(tmp_path, name="y", curves=curves, match=False).match_gain(3000.0) == 0.0


def test_undo_and_an_empty_session_is_not_kept(tmp_path):
    r = runner(tmp_path)
    tr = r.prepare()
    r.answer(tr, True, 500)
    again = r.undo_last()
    assert again.retake and not r.can_undo() and r.progress()["session_trials"] == 0
    r.answer(again, False, 500)
    assert [t.undone for t in r.session.trials] == [True, False]
    r.finish()
    (s,) = Profile.open("x", root=tmp_path).centring_sessions()
    assert s.trials[1].response == "right" and s.trials[1].retake
    r2 = runner(tmp_path, seed=3)
    r2.finish()
    assert len(Profile.open("x", root=tmp_path).centring_sessions()) == 1


async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out")


@pytest.mark.parametrize("debug", [False, True])
async def test_centring_session_ui(tmp_path, debug):
    from textual.widgets import Static

    prof = Profile.open("ui", root=tmp_path)
    runners = []

    def make():
        runners.append(CentringRunner(prof, CentringSettings(debug=debug), seed=len(runners), curves=FLAT))
        return runners[-1]

    app = FlatironApp(make, NullPlayer())
    async with app.run_test() as pilot:
        assert isinstance(app.screen, CentringIntroScreen)
        await pilot.press("enter")
        await pilot.pause()
        for _ in range(2):
            scr = app.screen
            assert isinstance(scr, ChannelCheckScreen)
            await pilot.press("left" if scr.order[0] == "L" else "right")
            await pilot.pause()
        scr = app.screen
        assert isinstance(scr, CentringScreen)
        await wait_for(pilot, lambda: scr.accepting)
        await pilot.press("d")
        await pilot.pause()
        text = str(scr.query_one("#debug", Static).render())
        assert ("Hz · L" in text) == debug
        for k, key in enumerate(["left", "right", "l"]):
            await wait_for(pilot, lambda: scr.accepting)
            await pilot.press(key)
            await wait_for(pilot, lambda: len(runners[0].session.trials) == k + 1)
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, SessionEndScreen)
    (s,) = prof.centring_sessions()
    assert [t.response for t in s.trials] == ["left", "right", "left"] and s.ended
    assert all(t.peeked == debug for t in s.trials)  # the readout stays on once shown


def test_cli_lists_and_deletes_centring_sessions(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.session.store import current_listener

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    prof = Profile.open("hp", root=current_listener().root)
    r = CentringRunner(prof, CentringSettings(), seed=0, curves=FLAT)
    answer_like(r, 10, balance=0.0)
    r.finish()
    assert main(["sessions", "hp"]) in (0, None)
    out = capsys.readouterr().out
    assert "Centring sessions" in out and "10 trials" in out
    sid = prof.centring_sessions()[0].id
    assert main(["sessions", "hp", "--delete", sid, "-y"]) == 0
    assert prof.centring_sessions() == []
