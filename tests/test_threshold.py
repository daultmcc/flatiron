"""Threshold sessions (DESIGN.md, "Threshold sessions")."""

import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from scipy.special import ndtr

from flatiron.audio.player import NullPlayer
from flatiron.model.grid import log_grid
from flatiron.model.threshold import ThresholdData, fit_thresholds, prior_shape
from flatiron.session.store import Profile
from flatiron.session.threshold import ThresholdRunner, ThresholdSettings
from flatiron.sim.listener import ThresholdListener
from flatiron.ui.app import ChannelCheckScreen, FlatironApp, LevelScreen, NeitherScreen, SessionEndScreen, \
    ThresholdIntroScreen, TwoIntervalScreen, YesNoScreen

GRID = log_grid()
MID = (GRID > 1000) & (GRID < 13000)
TRUTH = {"L": lambda f: -95 + prior_shape(f) + 4 * np.sin(2 * np.log2(f)),
         "R": lambda f: -92 + prior_shape(f) + 4 * np.sin(2 * np.log2(f) + 1)}


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def runner(tmp_path, task="yesno", seed=0, name="x"):
    return ThresholdRunner(Profile.open(name, root=tmp_path), ThresholdSettings(task=task), seed=seed,
                           volume=False)


def test_model_recovers_thresholds_offsets_and_false_alarms():
    rng = np.random.default_rng(0)
    offsets, sessions = [0.0, 6.0, -4.0], []
    for s, c in enumerate(offsets):
        two = s == 2
        gamma = 0.5 if two else 0.08
        recs = []
        for k in range(400):
            e, f = "LR"[k % 2], float(2 ** rng.uniform(np.log2(900), np.log2(14500)))
            kind = "blank" if not two and rng.random() < 0.1 else "probe" if rng.random() < 0.04 else "signal"
            th = TRUTH[e](np.array([f]))[0] + c
            x = th + 4 * rng.normal() + (15 if kind == "probe" else 0)
            p = gamma if kind == "blank" else gamma + (1 - gamma - 0.02) * ndtr((x - th) / 3)
            recs.append(NS(ear=e, freq=f, level_dbfs=x, positive=bool(rng.random() < p), kind=kind, undone=False))
        sessions.append(("2ifc" if two else "yesno", "noise:0.083333", recs))
    fit = fit_thresholds(ThresholdData.build(sessions))
    assert fit.converged and fit.sigma_fitted and fit.sigma == pytest.approx(3, rel=0.25)
    assert fit.c == pytest.approx(offsets, abs=1.5)
    assert fit.false_alarm[:2] == pytest.approx([0.08, 0.08], abs=0.06) and fit.false_alarm[2] == 0.5
    for i, e in enumerate("LR"):
        err = fit.T[i] - TRUTH[e](GRID)
        assert rms(err[MID]) < 2.0
        assert rms(err[MID]) < 2.5 * np.mean(fit.sd(e)[MID])


def test_ears_are_random_not_alternating(tmp_path):
    r = runner(tmp_path)
    ears = [r.next_plan().ear for _ in range(200)]
    switches = sum(a != b for a, b in zip(ears, ears[1:]))
    assert 70 < switches < 130 and 70 < ears.count("L") < 130


def test_startup_descends_until_the_first_miss_then_adapts(tmp_path):
    r = runner(tmp_path)
    start = r.level_dbfs - 30
    levels = {"L": [], "R": []}
    answers = {"L": [True, True, False], "R": [True, False]}
    while any(v is not None for v in r.startup.values()):
        tr = r.prepare()
        p = tr.plan
        if r.startup[p.ear] is None:
            # Ears are random, so an ear that's done goes adaptive while the
            # other is still starting: answered as a threshold 15 dB under the start.
            assert p.kind != "startup"
            r.answer(tr, p.kind != "blank" and p.level_db > start - 15, 500)
            continue
        assert p.kind == "startup" and p.freq == 2000
        levels[p.ear].append(p.level_db)
        r.answer(tr, answers[p.ear].pop(0), 500)
    assert levels == {"L": [start, start - 10, start - 20], "R": [start, start - 10]}
    # The adaptive trials start near what the run found.
    p = r.prepare().plan
    assert p.kind != "startup"
    mean, _ = r.estimate("L", 2000)
    assert start - 25 < mean[0] < start


def test_startup_climbs_when_it_starts_below_threshold(tmp_path):
    for task in ("2ifc-neither", "2ifc"):
        startup_climbs(tmp_path / task, task)


def startup_climbs(tmp_path, task):
    """The volume turned down since the last session: the run's first bursts
    aren't heard, so it goes up until one is, then down to bracket it."""
    r = runner(tmp_path, task)
    start = r.level_dbfs - 30
    heard = lambda x: x >= start + 15  # threshold 15 dB above the start
    levels = []
    while r.startup["L"] is not None:
        tr = r.prepare()
        p = tr.plan
        if p.ear == "R" or p.kind != "startup":
            r.answer(tr, None if task == "2ifc-neither" else True, 500)
            continue
        levels.append(p.level_db)
        right = heard(p.level_db)
        r.answer(tr, (p.interval == 1) if right else (None if task == "2ifc-neither" else p.interval != 1), 500)
    if task == "2ifc":  # a level passes after two right answers
        assert levels == [start, start + 10, start + 20, start + 20]
    else:  # down from the first level heard would be one already missed: done
        assert levels == [start, start + 10, start + 20]
    # Never above the listening level.
    r2 = runner(tmp_path, "2ifc-neither", name="y")
    for _ in range(40):
        tr = r2.prepare()
        if r2.startup["L"] is None and r2.startup["R"] is None:
            break
        assert tr.plan.kind != "startup" or tr.plan.level_db <= r2.level_dbfs
        r2.answer(tr, None, 500)
    assert r2.startup == {"L": None, "R": None}


def test_neither_model_estimates_false_claims_from_wrong_intervals():
    rng = np.random.default_rng(1)
    claim, recs = 0.2, []
    for k in range(600):
        e, f = "LR"[k % 2], float(2 ** rng.uniform(np.log2(900), np.log2(14500)))
        kind = "probe" if rng.random() < 0.04 else "signal"
        th = TRUTH[e](np.array([f]))[0]
        x = th + 4 * rng.normal() + (15 if kind == "probe" else 0)
        interval = int(rng.integers(1, 3))
        if rng.random() < 0.02:
            response = "neither"
        elif rng.random() < ndtr((x - th) / 3):
            response = ("first", "second")[interval - 1]
        elif rng.random() < claim:
            response = ("first", "second")[int(rng.integers(2))]
        else:
            response = "neither"
        positive = response == ("first", "second")[interval - 1]
        recs.append(NS(ear=e, freq=f, level_dbfs=x, positive=positive, kind=kind, undone=False,
                       response=response))
    data = ThresholdData.build([("2ifc-neither", "noise:0.083333", recs)])
    assert data.wrong.sum() > 20
    fit = fit_thresholds(data)
    # gamma: an unheard burst claimed and its interval guessed right.
    assert fit.false_alarm[0] == pytest.approx(claim / 2, abs=0.04)
    for i, e in enumerate("LR"):
        assert rms((fit.T[i] - TRUTH[e](GRID))[MID]) < 2.0


def test_neither_answers_only_in_2ifc_neither(tmp_path):
    r = runner(tmp_path, task="2ifc-neither")
    tr = r.prepare()
    assert tr.second_on > tr.first_off  # two intervals
    rec = r.answer(tr, None, 500)
    assert rec.response == "neither" and not rec.positive
    assert r.progress()["neither"] == 1
    r2 = runner(tmp_path, task="2ifc", name="y")
    with pytest.raises(ValueError):
        r2.answer(r2.prepare(), None, 500)


def test_buffers_blank_and_two_interval(tmp_path):
    r = runner(tmp_path, seed=3)
    r.startup = {"L": None, "R": None}
    seen = set()
    for _ in range(80):
        tr = r.prepare()
        ch = 0 if tr.plan.ear == "L" else 1
        assert np.all(tr.buffer[:, 1 - ch] == 0)
        assert (np.all(tr.buffer == 0)) == (tr.plan.kind == "blank")
        seen.add(tr.plan.kind)
        r.answer(tr, bool(r.rng.random() < 0.5), 500)
    assert {"signal", "blank", "probe"} <= seen
    r2 = runner(tmp_path, task="2ifc", seed=4, name="y")
    fs = r2.fs
    for _ in range(20):
        tr = r2.prepare()
        first = tr.buffer[int(tr.first_on * fs):int(tr.first_off * fs)]
        second = tr.buffer[int(tr.second_on * fs):int(tr.second_off * fs)]
        assert (np.any(first != 0), np.any(second != 0)) == (tr.plan.interval == 1, tr.plan.interval == 2)
        rec = r2.answer(tr, True, 500)
        assert rec.positive == (tr.plan.interval == 1) and rec.response == "first"
        assert tr.plan.kind != "blank"


def test_sessions_are_kept_apart_from_trial_sessions_and_undo_restores_the_run(tmp_path):
    r = runner(tmp_path)
    prof = r.profile
    tr = r.prepare()
    r.answer(tr, True, 500)
    assert r.startup[tr.plan.ear] == tr.plan.level_db - 10
    again = r.undo_last()
    assert again.retake and r.startup[tr.plan.ear] == tr.plan.level_db and not r.can_undo()
    rec = r.answer(again, False, 500)
    assert rec.retake and r.startup[tr.plan.ear] == tr.plan.level_db + 10  # nothing heard yet: up
    r.finish()
    assert prof.sessions() == []
    (s,) = prof.threshold_sessions()
    assert [t.undone for t in s.trials] == [True, False] and s.ended and s.task == "yesno"
    raw = json.loads((prof.thresholds_dir / f"{s.id}.json").read_text())
    assert raw["comfort_dbfs"] == r.level_dbfs and raw["grid"]
    # An empty session isn't kept.
    r2 = ThresholdRunner(prof, ThresholdSettings(), seed=1, volume=False)
    r2.finish()
    assert len(prof.threshold_sessions()) == 1


@pytest.mark.parametrize("task", ["yesno", "2ifc", "2ifc-neither"])
def test_adaptive_sessions_find_a_simulated_threshold(tmp_path, task):
    prof = Profile.open("x", root=tmp_path)
    for k, volume in enumerate((0.0, 8.0)):
        truth = {e: (lambda e: lambda f: TRUTH[e](f) + volume)(e) for e in "LR"}
        lis = ThresholdListener(truth, seed=k)
        r = ThresholdRunner(prof, ThresholdSettings(task=task), seed=k, volume=False)
        for i in range(200):
            tr = r.prepare()
            r.answer(tr, lis.answer(tr.plan, task), 500, t=i)
        r.finish()
    assert r.fit.c[1] == pytest.approx(8.0, abs=3.0)
    for e in "LR":
        # 2IFC says less per trial (a guess is right half the time): judged by its own error bars.
        mean, sd = r.estimate(e, GRID)
        err = rms((mean - TRUTH[e](GRID) - 8.0)[MID])
        assert err < 2 * np.mean(sd[MID]) and err < 6.0


def make_app(tmp_path, task, debug=False):
    prof = Profile.open(f"ui test {debug}", root=tmp_path)
    runners = []

    def make():
        runners.append(ThresholdRunner(prof, ThresholdSettings(task=task, debug=debug), seed=len(runners),
                                       volume=False))
        return runners[-1]

    return FlatironApp(make, NullPlayer()), runners, prof


async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out")


@pytest.mark.parametrize("task,screen,keys", [("yesno", YesNoScreen, ["y", "n", "up"]),
                                              ("2ifc", TwoIntervalScreen, ["1", "2", "down"]),
                                              ("2ifc-neither", NeitherScreen, ["1", "n", "down"])])
async def test_threshold_session_ui(tmp_path, task, screen, keys):
    app, runners, prof = make_app(tmp_path, task)
    async with app.run_test() as pilot:
        assert isinstance(app.screen, ThresholdIntroScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LevelScreen)
        await pilot.press("down", "down", "enter")  # 2 dB down
        await pilot.pause()
        for _ in range(2):
            scr = app.screen
            assert isinstance(scr, ChannelCheckScreen)
            await pilot.press("left" if scr.order[0] == "L" else "right")
            await pilot.pause()
        scr = app.screen
        assert isinstance(scr, screen)
        for k, key in enumerate(keys):
            await wait_for(pilot, lambda: scr.accepting)
            await pilot.press(key)
            await wait_for(pilot, lambda: len(runners[0].session.trials) == k + 1)
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, SessionEndScreen)
    (s,) = prof.threshold_sessions()
    first = {"yesno": ["heard", "nothing", "heard"], "2ifc": ["first", "second", "second"],
             "2ifc-neither": ["first", "neither", "second"]}[task]
    assert [t.response for t in s.trials] == first and s.ended
    assert s.comfort_dbfs == runners[0].level_dbfs  # what the level screen set
    assert prof.sessions() == []


@pytest.mark.parametrize("task", ["yesno", "2ifc"])
async def test_threshold_debug_readout_only_with_the_flag(tmp_path, task):
    from textual.widgets import Static

    for debug in (False, True):
        app, runners, prof = make_app(tmp_path, task, debug)
        async with app.run_test() as pilot:
            await pilot.press("enter", "enter")  # introduction, level
            await pilot.pause()
            for _ in range(2):
                scr = app.screen
                await pilot.press("left" if scr.order[0] == "L" else "right")
                await pilot.pause()
            scr = app.screen
            await wait_for(pilot, lambda: scr.accepting)
            await pilot.press("d")
            await pilot.pause()
            text = str(scr.query_one("#debug", Static).render())
            assert ("Hz at" in text) == debug
            if debug:
                p = scr.trial.plan
                assert f"{p.ear} · {p.freq:,.0f} Hz at {scr.trial.played_db:+.1f} dBFS" in text
                assert ("interval" in text) == (task == "2ifc") and "estimate" in text
            await pilot.press("1")
            await wait_for(pilot, lambda: len(runners[0].session.trials) == 1)
        assert runners[0].session.trials[0].peeked == debug


def test_trials_follow_the_measured_range(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    r = ThresholdRunner(prof, ThresholdSettings(), seed=0, volume=False)
    for i in range(30):
        r.answer(r.prepare(), i % 2 == 0, 500, t=i)
    r.finish()
    prof.update(measured={"lo": 3000.0, "hi": 9000.0})
    r = ThresholdRunner(prof, ThresholdSettings(), seed=1, volume=False)
    assert r.session.measured == [3000.0, 9000.0]
    # The grid still covers the first session's frequencies, for the fit.
    assert r.grid[0] <= 900 and r.grid[-1] >= 14000
    plans = []
    for i in range(80):
        tr = r.prepare()
        plans.append(tr.plan)
        r.answer(tr, i % 3 != 0, 500, t=i)
    assert all(3000 * (1 - 1e-6) <= p.freq <= 9000 * (1 + 1e-6) for p in plans)
    assert {p.freq for p in plans if p.kind == "startup"} == {3000.0}


async def test_escape_on_the_introduction_keeps_nothing(tmp_path):
    app, _, prof = make_app(tmp_path, "yesno")
    async with app.run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()
    assert prof.threshold_sessions() == []


def test_cli_lists_and_deletes_threshold_sessions(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from flatiron.session.store import current_listener

    prof = Profile.open("hp", root=current_listener().root)
    r = ThresholdRunner(prof, ThresholdSettings(), seed=0, volume=False)
    for i in range(12):
        r.answer(r.prepare(), i % 3 == 0, 500, t=i)
    r.finish()
    assert main(["sessions", "hp"]) in (0, None)
    out = capsys.readouterr().out
    assert "Threshold sessions" in out and r.session.id in out and "yesno" in out
    assert main(["sessions", "hp", "--delete", r.session.id, "-y"]) == 0
    assert prof.threshold_sessions() == []


def test_a_known_eq_gain_adds_to_the_level():
    recs = [NS(ear="L", freq=2000.0, level_dbfs=-80.0, positive=True, kind="signal", undone=False, eq_gain=3.5),
            NS(ear="R", freq=2000.0, level_dbfs=-80.0, positive=False, kind="signal", undone=False)]
    data = ThresholdData.build([("yesno", "noise:0.083333", recs)])
    assert list(data.level) == [-76.5, -80.0]


def test_the_level_sets_where_the_descending_run_starts(tmp_path):
    r = ThresholdRunner(Profile.open("x", root=tmp_path), ThresholdSettings(), seed=0, volume=False)
    r.set_level(-40.0)
    assert r.session.comfort_dbfs == -40.0
    assert r.startup == {"L": -40.0 - r.cfg.startup_below_comfort_db, "R": -40.0 - r.cfg.startup_below_comfort_db}
    assert r.match_gain("L", 4000.0) == 0.0  # no fitted curve yet: no matching
    r.answer(r.prepare(), True, 500)
    r.set_level(-20.0)  # ignored once trials have started
    assert r.session.comfort_dbfs == -40.0
