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
from flatiron.ui.app import ChannelCheckScreen, FlatironApp, SessionEndScreen, ThresholdIntroScreen, \
    TwoIntervalScreen, YesNoScreen

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
        sessions.append((two, "noise:0.083333", recs))
    fit = fit_thresholds(ThresholdData.build(sessions))
    assert fit.converged and fit.sigma_fitted and fit.sigma == pytest.approx(3, rel=0.25)
    assert fit.c == pytest.approx(offsets, abs=1.5)
    assert fit.false_alarm[:2] == pytest.approx([0.08, 0.08], abs=0.06) and fit.false_alarm[2] == 0.5
    for i, e in enumerate("LR"):
        err = fit.T[i] - TRUTH[e](GRID)
        assert rms(err[MID]) < 2.0
        assert rms(err[MID]) < 2.5 * np.mean(fit.sd(e)[MID])


def test_startup_descends_until_the_first_miss_then_adapts(tmp_path):
    r = runner(tmp_path)
    start = r.level_dbfs - 30
    levels = {"L": [], "R": []}
    answers = {"L": [True, True, False], "R": [True, False]}
    while any(v is not None for v in r.startup.values()):
        tr = r.prepare()
        p = tr.plan
        assert p.kind == "startup" and p.freq == 2000
        levels[p.ear].append(p.level_db)
        r.answer(tr, answers[p.ear].pop(0), 500)
    assert levels == {"L": [start, start - 10, start - 20], "R": [start, start - 10]}
    # The adaptive trials start near what the run found.
    p = r.prepare().plan
    assert p.kind != "startup"
    mean, _ = r.estimate("L", 2000)
    assert start - 25 < mean[0] < start


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
    assert rec.retake and r.startup[tr.plan.ear] is None
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


@pytest.mark.parametrize("task", ["yesno", "2ifc"])
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


def make_app(tmp_path, task):
    prof = Profile.open("ui test", root=tmp_path)
    runners = []

    def make():
        runners.append(ThresholdRunner(prof, ThresholdSettings(task=task), seed=len(runners), volume=False))
        return runners[-1]

    return FlatironApp(make, NullPlayer()), runners, prof


async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out")


@pytest.mark.parametrize("task,screen,keys", [("yesno", YesNoScreen, ["y", "n", "left"]),
                                              ("2ifc", TwoIntervalScreen, ["1", "2", "right"])])
async def test_threshold_session_ui(tmp_path, task, screen, keys):
    app, runners, prof = make_app(tmp_path, task)
    async with app.run_test() as pilot:
        assert isinstance(app.screen, ThresholdIntroScreen)
        await pilot.press("enter")
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
    first = ["heard", "nothing", "heard"] if task == "yesno" else ["first", "second", "second"]
    assert [t.response for t in s.trials] == first and s.ended
    assert prof.sessions() == []


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
