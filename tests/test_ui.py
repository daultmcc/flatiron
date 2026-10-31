import numpy as np

from flatiron.audio.player import NullPlayer
from flatiron.session.runner import RunSettings, TrialRunner
from flatiron.session.scheduler import SchedulerConfig
from flatiron.session.store import Profile
from flatiron.session.sweep import SweepTiming
from flatiron.ui.app import ChannelCheckScreen, FlatironApp, SessionEndScreen, SweepScreen, TrialScreen

CFG = SchedulerConfig(block_size=20, min_trials_noise_fit=10_000)


def make_app(tmp_path, sweep=False, minutes=12.0):
    prof = Profile.open("ui test", root=tmp_path)
    settings = RunSettings(session_minutes=minutes)
    runners = []

    def make():
        runners.append(TrialRunner(prof, settings, config=CFG, seed=len(runners)))
        return runners[-1]

    app = FlatironApp(make, NullPlayer(), sweep=sweep, sweep_timing=SweepTiming(duration=2.0, gap=0.2))
    return app, runners, prof


async def pass_channel_check(app, pilot):
    """Accept the session-start level screen, then answer the channel check."""
    from flatiron.ui.app import LevelScreen

    if isinstance(app.screen, LevelScreen):
        await pilot.press("enter")
        await pilot.pause()
    for _ in range(2):
        scr = app.screen
        assert isinstance(scr, ChannelCheckScreen)
        await pilot.press("left" if scr.order[0] == "L" else "right")
        await pilot.pause()


async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out")


async def test_channel_check_rejects_wrong_side(tmp_path):
    app, runners, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("enter")  # level screen
        await pilot.pause()
        scr = app.screen
        wrong = "right" if scr.order[0] == "L" else "left"
        await pilot.press(wrong)
        await pilot.pause()
        assert scr.failed and isinstance(app.screen, ChannelCheckScreen)
        await pilot.press("enter")
        await pass_channel_check(app, pilot)
        assert isinstance(app.screen, TrialScreen)
    checks = runners[0].session.channel_checks
    assert [c["ok"] for c in checks] == [False, True, True]


async def test_trials_record_answers_and_quit_saves(tmp_path):
    app, runners, prof = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        assert isinstance(scr, TrialScreen)
        for k in range(3):
            await wait_for(pilot, lambda: scr.accepting)
            await pilot.press("left" if k % 2 == 0 else "2")
            await wait_for(pilot, lambda: len(runners[0].session.trials) == k + 1)
        # Replays are counted on the next answer.
        await wait_for(pilot, lambda: scr.accepting)
        await pilot.press("space")
        await wait_for(pilot, lambda: scr.accepting)
        await pilot.press("1")
        await wait_for(pilot, lambda: len(runners[0].session.trials) == 4)
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, SessionEndScreen)
    s = prof.sessions()[0]
    assert [t.response for t in s.trials] == ["first", "second", "first", "first"]
    assert [t.replays for t in s.trials] == [0, 0, 0, 1]
    assert s.ended is not None
    assert all(-1200 < t.rt_ms < 5000 for t in s.trials)
    assert len(app.player.played) >= 2 + 5


async def test_answers_ignored_before_second_interval(tmp_path):
    app, runners, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        await wait_for(pilot, lambda: scr.trial is not None and not scr.busy)
        await pilot.press("left")  # still in the ITI / first burst
        await pilot.pause(0.1)
        assert runners[0].session.trials == []


async def test_pause_stops_the_clock(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        await pilot.press("p")
        t = scr.active_seconds()
        await pilot.pause(0.3)
        assert scr.active_seconds() == t and scr.paused
        await pilot.press("p")
        await wait_for(pilot, lambda: scr.accepting)


async def test_time_limit_ends_session_and_next_session_reseats(tmp_path):
    app, runners, prof = make_app(tmp_path, minutes=0.02)  # 1.2 s
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        await wait_for(pilot, lambda: scr.accepting)
        await pilot.pause(1.3)
        await pilot.press("left")
        await wait_for(pilot, lambda: isinstance(app.screen, SessionEndScreen))
        await pilot.press("n")
        await pilot.pause()
        from flatiron.ui.app import LevelScreen

        assert isinstance(app.screen, LevelScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChannelCheckScreen)
        assert len(runners) == 2 and runners[1].prior_trials["L"] + runners[1].prior_trials["R"] == 1
    # The second session was left before any trials, so it is not kept.
    assert len(prof.sessions()) == 1


async def test_sweep_taps_become_marks(tmp_path):
    app, runners, _ = make_app(tmp_path, sweep=True)
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        assert isinstance(scr, SweepScreen)
        tm = scr.timing
        since = lambda: app.clock() - scr.t0
        for ear in ("L", "R"):
            await pilot.press("enter")
            # Tap 0.7 s into the up pass, then at the mirrored point of the down
            # pass plus the same lag, so the pair should meet near f(0.7 s).
            await wait_for(pilot, lambda: since() >= 0.7, 5)
            await pilot.press("down")
            await wait_for(pilot, lambda: since() >= tm.duration + tm.gap + tm.duration - 0.7 + 0.1, 5)
            await pilot.press("down")
            await wait_for(pilot, lambda: ear not in scr.ears, 5)
        await wait_for(pilot, lambda: isinstance(app.screen, TrialScreen), 5)
    marks = runners[0].session.sweep_marks
    assert set(marks) == {"L", "R"} and all(len(m) == 1 and m[0]["kind"] == "dip" for m in marks.values())


async def test_replay_does_not_reset_reaction_time(tmp_path):
    app, runners, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        await wait_for(pilot, lambda: scr.accepting)
        await wait_for(pilot, lambda: app.clock() > scr.first_end + 0.1)
        await pilot.press("space")
        # Still answerable during the replay, and the reference stays put.
        assert scr.accepting
        await wait_for(pilot, lambda: app.clock() > scr.last_end + 0.05)
        await pilot.press("left")
        await wait_for(pilot, lambda: len(runners[0].session.trials) == 1)
    t = runners[0].session.trials[0]
    assert t.replays == 1
    assert t.rt_ms > 1000  # counted from the first presentation
    assert 0 < t.rt_last_ms < 150  # a quick answer after the replay...
    assert not t.fast  # ...is not flagged


async def test_discard_session(tmp_path):
    from flatiron.ui.app import ConfirmScreen

    app, runners, prof = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        for k in range(2):
            await wait_for(pilot, lambda: scr.accepting)
            await pilot.press("left")
            await wait_for(pilot, lambda: len(runners[0].session.trials) == k + 1)
        # Changing your mind keeps the session, paused.
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause()
        assert app.screen is scr and scr.paused
        assert len(prof.sessions()) == 1
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert isinstance(app.screen, SessionEndScreen)
        assert "discarded" in str(app.screen.query_one("#title").render()).lower()
        await pilot.press("n")
        await pilot.pause()
        assert runners[1].prior_trials == {"L": 0, "R": 0}
    assert prof.sessions() == []


async def test_session_starts_with_level_from_last_session(tmp_path):
    from flatiron.audio.limiter import DEFAULT_LEVEL_DB, MAX_LEVEL_DB
    from flatiron.ui.app import LevelScreen

    app, runners, prof = make_app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        assert isinstance(scr, LevelScreen) and scr.level == DEFAULT_LEVEL_DB
        for _ in range(3):
            await pilot.press("up")
        await pilot.press("down")
        assert scr.level == DEFAULT_LEVEL_DB + 2
        for _ in range(10):
            await pilot.press("up")
        assert scr.level == MAX_LEVEL_DB and "most Flatiron will play" in scr.note
        for _ in range(3):
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChannelCheckScreen)
        assert runners[0].level_dbfs == MAX_LEVEL_DB - 3
        await pass_channel_check(app, pilot)
        trial = app.screen
        await wait_for(pilot, lambda: trial.accepting)
        await pilot.press("left")
        await wait_for(pilot, lambda: len(runners[0].session.trials) == 1)
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("n")  # next session: starts at the level just used
        await pilot.pause()
        assert isinstance(app.screen, LevelScreen) and app.screen.level == MAX_LEVEL_DB - 3
    assert prof.sessions()[0].level_dbfs == MAX_LEVEL_DB - 3
    assert prof.sessions()[0].trials[0].level_dbfs == MAX_LEVEL_DB - 3
    # Each change restarts the loop at the new level; the loudest loop was at the cap.
    peaks = [np.abs(b).max() for b in app.player.played[:15]]
    assert max(peaks) > peaks[0]


async def test_escape_on_session_level_quits_without_a_session(tmp_path):
    app, runners, prof = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()
        assert not app.is_running
    assert prof.sessions() == []


async def test_level_sequence_is_like_the_trials(tmp_path):
    from flatiron.audio.limiter import DEFAULT_LEVEL_DB
    from flatiron.ui.app import LevelScreen

    app, _, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        buf = app.player.played[-1]
        n = int(0.4 * 48000)
        step = n + int(LevelScreen.GAP_S * 48000)
        assert buf.shape[0] == step * len(LevelScreen.FREQS)
        for i in range(len(LevelScreen.FREQS)):
            burst = buf[i * step:i * step + n]
            on, off = (0, 1) if i % 2 == 0 else (1, 0)
            assert np.all(burst[:, off] == 0)  # one ear at a time
            rms = 20 * np.log10(np.sqrt(np.mean(burst[:, on].astype(float) ** 2)))
            assert abs(rms - DEFAULT_LEVEL_DB) < 0.1  # same level as a trial burst


async def test_mid_session_level_change_warns_and_is_logged(tmp_path):
    from flatiron.ui.app import LevelScreen

    app, runners, prof = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        await wait_for(pilot, lambda: scr.accepting)
        await pilot.press("left")
        await wait_for(pilot, lambda: len(runners[0].session.trials) == 1 and not scr.busy)
        start = runners[0].level_dbfs
        await pilot.press("l")
        await pilot.pause()
        lvl = app.screen
        assert isinstance(lvl, LevelScreen) and scr.paused
        assert "shift the results" in str(lvl.query_one("#message").render())
        # Esc: no change.
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is scr and not scr.paused and runners[0].level_dbfs == start
        await pilot.press("l")
        await pilot.pause()
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert app.screen is scr and not scr.paused
        assert scr.trial.level_dbfs == start - 2  # current pair re-rendered
        await wait_for(pilot, lambda: scr.accepting)
        await pilot.press("left")
        await wait_for(pilot, lambda: len(runners[0].session.trials) == 2)
    s = prof.sessions()[0]
    assert s.level_dbfs == start
    assert [t.level_dbfs for t in s.trials] == [start, start - 2]
    assert s.level_changes == [{"after_trial": 1, "from": start, "to": start - 2,
                                "at": s.level_changes[0]["at"]}]


async def test_undo_replays_the_pair_and_keeps_the_record(tmp_path):
    app, runners, prof = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        await pilot.press("u")
        await pilot.pause()
        assert "Nothing to undo" in str(scr.query_one("#message").render())
        await wait_for(pilot, lambda: scr.accepting)
        first = scr.trial
        await pilot.press("left")  # the typo
        await wait_for(pilot, lambda: len(runners[0].session.trials) == 1 and not scr.busy)
        assert scr.trial.plan is not first.plan
        await pilot.press("u")
        await wait_for(pilot, lambda: scr.trial is not None and scr.trial.plan is first.plan and not scr.busy)
        assert "undone" in str(scr.query_one("#message").render())
        await wait_for(pilot, lambda: scr.accepting)
        await pilot.press("right")  # the intended answer
        await wait_for(pilot, lambda: len(runners[0].session.trials) == 2)
        await pilot.pause(0.2)
        assert runners[0].scheduler.n_trials(first.plan.ear) == 1
    s = prof.sessions()[0]
    assert [(t.response, t.undone, t.retake) for t in s.trials] == [("first", True, False),
                                                                    ("second", False, True)]
    assert s.trials[0].f_a == s.trials[1].f_a and s.trials[0].f_b == s.trials[1].f_b
    assert len(s.trials_for(first.plan.ear)) == 1


async def test_verify_session_ends_at_its_trial_count(tmp_path):
    from flatiron.apo import ApoFilter
    from flatiron.audio.preview import PreviewEQ

    prof = Profile.open("v", root=tmp_path)
    eq = PreviewEQ({"L": [ApoFilter("PK", 6000, -6, 4)], "R": []}, -0.5, "abc")
    runners = []

    def make():
        runners.append(TrialRunner(prof, RunSettings(preview=eq, max_trials=2), config=CFG, seed=0))
        return runners[-1]

    app = FlatironApp(make, NullPlayer())
    async with app.run_test() as pilot:
        await pass_channel_check(app, pilot)
        scr = app.screen
        for k in range(2):
            await wait_for(pilot, lambda: scr.accepting)
            await pilot.press("left")
            await wait_for(pilot, lambda: len(runners[0].session.trials) == k + 1)
        await wait_for(pilot, lambda: isinstance(app.screen, SessionEndScreen))
        assert "Verify session complete" in str(app.screen.query_one("#title").render())
    assert prof.sessions()[0].preview["fit_hash"] == "abc"
