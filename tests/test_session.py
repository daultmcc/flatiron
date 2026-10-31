import json

import numpy as np
import pytest

from flatiron.apo import parse_apo
from flatiron.audio.stimulus import StimulusSpec
from flatiron.model.grid import log_grid, octaves
from flatiron.session.runner import RunSettings, TrialRunner
from flatiron.session.scheduler import SchedulerConfig
from flatiron.session.store import Profile, Session, slugify
from flatiron.session.sweep import SweepTiming, pair_taps, sweep_signal
from flatiron.sim.listener import SimulatedListener, design_example

FAST_CFG = SchedulerConfig(block_size=20, min_trials_noise_fit=10_000)


def answer_like(lis, runner, n, t0=0.0):
    for k in range(n):
        tr = runner.prepare()
        p = tr.plan
        a_louder = bool(lis.respond(p.ear, p.f_a, p.f_b, p.offset, p.a_first))
        runner.answer(tr, first_louder=(a_louder == p.a_first), rt_ms=400, t=t0 + k)


def test_slugify():
    assert slugify("Sennheiser HD 600 (pads v2)") == "sennheiser-hd-600-pads-v2"
    with pytest.raises(ValueError):
        slugify("!!!")


def test_profile_and_session_roundtrip(tmp_path):
    prof = Profile.open("HD 600", root=tmp_path)
    assert prof.read()["label"] == "HD 600"
    prof.update(note="pads v2")
    assert Profile.open("HD 600", root=tmp_path).read()["note"] == "pads v2"
    lis = SimulatedListener({e: design_example(e) for e in "LR"}, seed=0)
    r = TrialRunner(prof, config=FAST_CFG, seed=5, device={"name": "test"})
    answer_like(lis, r, 30)
    r.finish()
    raw = json.loads((prof.sessions_dir / f"{r.session.id}.json").read_text())
    assert raw["seed"] == 5 and raw["ended"] and len(raw["trials"]) == 30
    assert raw["device"] == {"name": "test"} and raw["stimulus"]["kind"] == "noise"
    s = Session.from_json(raw)
    assert s.trials[0] == r.session.trials[0]
    assert set(s.grid) == {"L", "R"}


def test_answer_maps_interval_to_stimulus(tmp_path):
    r = TrialRunner(Profile.open("x", root=tmp_path), config=FAST_CFG, seed=1)
    for _ in range(20):
        tr = r.prepare()
        rec = r.answer(tr, first_louder=True, rt_ms=100)
        assert rec.a_louder == tr.plan.a_first
        assert rec.fast
        assert rec.response == "first"


def test_buffer_order_levels_and_ear(tmp_path):
    settings = RunSettings(stimulus=StimulusSpec(kind="tone"), level_dbfs=-40)
    # (An explicit starting level overrides the last session's.)
    r = TrialRunner(Profile.open("x", root=tmp_path), settings, config=FAST_CFG, seed=3)
    for _ in range(10):
        tr = r.prepare()
        p, fs = tr.plan, r.fs
        ch = 0 if p.ear == "L" else 1
        assert np.all(tr.buffer[:, 1 - ch] == 0)
        first = tr.buffer[int(tr.first_on * fs):int(tr.first_off * fs), ch]
        second = tr.buffer[int(tr.second_on * fs):int(tr.second_off * fs), ch]
        lvl = lambda x: 20 * np.log10(np.sqrt(np.mean(x.astype(float) ** 2)))
        d_first, d_second = (p.delta_a, p.delta_b) if p.a_first else (p.delta_b, p.delta_a)
        assert lvl(first) - lvl(second) == pytest.approx(d_first - d_second, abs=0.05)
        assert lvl(first) == pytest.approx(-40 + p.rove_db + d_first + tr.limited_db, abs=0.05)
        f_first = p.f_a if p.a_first else p.f_b
        spec = np.abs(np.fft.rfft(first))
        assert octaves(np.argmax(spec) * fs / first.size, f_first) < 1 / 24


def test_active_eq_gain_recorded_as_known_offset(tmp_path):
    eq = parse_apo("Filter 1: ON PK Fc 4000 Hz Gain 6 dB Q 1.0")
    r = TrialRunner(Profile.open("x", root=tmp_path), RunSettings(active_eq=eq, eq_note="autoeq"),
                    config=FAST_CFG, seed=2)
    tr = r.prepare()
    rec = r.answer(tr, True, 500)
    ga, gb = eq.gain_db([tr.plan.f_a, tr.plan.f_b])
    assert rec.eq_gain_a == pytest.approx(ga) and rec.eq_gain_b == pytest.approx(gb)
    assert rec.offset == pytest.approx(tr.plan.offset + ga - gb)
    assert r.session.eq_context["note"] == "autoeq"


def test_next_session_resumes_from_history(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    lis = SimulatedListener({e: design_example(e) for e in "LR"}, seed=0)
    r1 = TrialRunner(prof, config=FAST_CFG, seed=1)
    answer_like(lis, r1, 60)
    r1.finish()
    r2 = TrialRunner(prof, config=FAST_CFG, seed=2)
    assert r2.prior_trials == {"L": 30, "R": 30}
    assert r2.scheduler.n_trials("L") == 30
    assert np.allclose(r2.scheduler.ears["L"].grid, r1.session.grid["L"])
    trials, _ = prof.history()
    assert len(trials["L"]) == 30
    assert r2.session.id != r1.session.id


def test_catch_accuracy(tmp_path):
    r = TrialRunner(Profile.open("x", root=tmp_path),
                    config=SchedulerConfig(catch_rate=1.0, min_trials_noise_fit=10_000), seed=0)
    for _ in range(10):
        tr = r.prepare()
        louder_first = (tr.plan.offset > 0) == tr.plan.a_first
        r.answer(tr, louder_first, 500)
    assert r.catch_accuracy() == 1.0


def test_settings_validation():
    with pytest.raises(ValueError):
        RunSettings(session_minutes=20)
    with pytest.raises(ValueError):
        RunSettings(gap_s=1.0)


def test_sweep_pairing_cancels_reaction_lag():
    tm = SweepTiming()
    true = {2500.0: "dip", 6200.0: "peak", 9000.0: "dip"}
    taps = []
    for f, kind in true.items():
        t_up = np.log2(f / tm.f_lo) / tm.rate_oct_s
        t_down = tm.duration + tm.gap + np.log2(tm.f_hi / f) / tm.rate_oct_s
        taps += [(t_up + 0.45, kind), (t_down + 0.35, kind)]
    taps.append((5.0, "peak"))  # stray up-sweep tap with no partner
    marks, unmatched = pair_taps(taps, tm)
    assert unmatched == 1
    assert [m.kind for m in marks] == list(true.values())
    for m, f in zip(marks, true):
        # Unequal lags leave a residual of half their difference.
        assert octaves(m.freq, f) <= 0.05 * tm.rate_oct_s + 1e-9
        assert m.lag_s == pytest.approx(0.4, abs=0.01)


def test_sweep_signal_tracks_frequency():
    tm = SweepTiming(duration=4.0, gap=0.5)
    x = sweep_signal(tm, seed=0)
    fs = 48000
    assert x.size == int(tm.total * fs)
    for t in (1.0, 3.0, 5.5):
        seg = x[int(t * fs):int(t * fs) + 2048] * np.hanning(2048)
        f_peak = np.argmax(np.abs(np.fft.rfft(seg))) * fs / 2048
        assert octaves(f_peak, tm.freq_at(t + 1024 / fs)[0]) < 0.1


def test_sweep_marks_refine_grid(tmp_path):
    from flatiron.session.sweep import Mark

    r = TrialRunner(Profile.open("x", root=tmp_path), config=FAST_CFG, seed=0)
    before = r.scheduler.ears["L"].grid.size
    added = r.apply_sweep_marks("L", [Mark(6200.0, "dip", 0.4)])
    g = r.scheduler.ears["L"].grid
    assert added > 0 and g.size == before + added
    near = g[octaves(g, 6200) <= 1 / 12]
    assert np.min(np.diff(np.log2(near))) == pytest.approx(1 / 48, rel=0.05)
    assert r.session.sweep_marks["L"][0]["kind"] == "dip"


def test_empty_session_is_not_kept(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    r = TrialRunner(prof, config=FAST_CFG, seed=0)
    assert len(prof.sessions()) == 1  # saved from the start, so a crash keeps setup data
    r.finish()
    assert prof.sessions() == []


def test_discard_deletes_and_blocks_later_saves(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    r = TrialRunner(prof, config=FAST_CFG, seed=0)
    r.answer(r.prepare(), True, 400)
    tr = r.prepare()
    r.discard()
    r.answer(tr, True, 400)  # e.g. a refit worker finishing after the discard
    r.finish()
    assert prof.sessions() == []


def test_profile_listing(tmp_path):
    assert Profile.all(root=tmp_path) == []
    Profile.open("HD 600", root=tmp_path)
    Profile.open("Sundara", root=tmp_path)
    assert [p.slug for p in Profile.all(root=tmp_path)] == ["hd-600", "sundara"]
    assert Profile.existing("hd 600", root=tmp_path).slug == "hd-600"
    assert Profile.existing("nope", root=tmp_path) is None


def test_cli_profiles_and_session_delete(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    prof = Profile.open("HD 600")
    assert prof.dir.is_relative_to(tmp_path)
    r = TrialRunner(prof, config=FAST_CFG, seed=0)
    r.answer(r.prepare(), True, 400)  # left open, as after a crash
    main(["profiles"])
    out = capsys.readouterr().out
    assert "HD 600  (hd-600)" in out and "1 not closed" in out and "last level -30 dBFS" in out
    monkeypatch.setattr("builtins.input", lambda q: "n")
    main(["sessions", "--profile", "HD 600", "--delete", r.session.id])
    assert len(prof.sessions()) == 1
    main(["sessions", "-p", "HD 600", "--delete", r.session.id, "-y"])
    assert prof.sessions() == []
    assert main(["sessions", "--profile", "nope"]) == 1
    Profile.open("Sundara")
    capsys.readouterr()
    assert main(["sessions"]) == 1  # two profiles now: must pick one
    assert "pick one with -p" in capsys.readouterr().out


def test_undo_withdraws_from_fit_and_scheduler(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    r = TrialRunner(prof, config=FAST_CFG, seed=0)
    assert not r.can_undo() and r.undo_last() is None
    t1 = r.prepare()
    r.answer(t1, True, 400)
    t2 = r.prepare()
    r.answer(t2, False, 400)
    ear = t2.plan.ear
    before = r.scheduler.n_trials(ear)
    again = r.undo_last()
    assert again.plan is t2.plan and again.retake
    assert r.scheduler.n_trials(ear) == before - 1
    assert not r.can_undo()  # one step only
    rec = r.answer(again, True, 400)
    assert rec.retake and r.scheduler.n_trials(ear) == before
    s = prof.sessions()[0]
    assert [t.undone for t in s.trials] == [False, True, False]
    assert r.progress()["session_trials"] == 2 and r.progress()["undone"] == 1
    trials, _ = prof.history()
    assert len(trials["L"]) + len(trials["R"]) == 2


def test_level_carries_over_and_changes_are_logged(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    r = TrialRunner(prof, config=FAST_CFG, seed=0)
    assert r.level_dbfs == -30
    r.set_level(-27)  # before any trials: just the session's level
    r.answer(r.prepare(), True, 400)
    r.set_level(-29)
    tr = r.prepare()
    assert tr.level_dbfs == -29
    r.answer(tr, True, 400)
    r.finish()
    s = prof.sessions()[0]
    assert s.level_dbfs == -27 and [t.level_dbfs for t in s.trials] == [-27, -29]
    assert [(c["after_trial"], c["from"], c["to"]) for c in s.level_changes] == [(1, -27, -29)]
    assert TrialRunner(prof, config=FAST_CFG, seed=1).level_dbfs == -29



def test_session_trials_carry_play_order(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    r = TrialRunner(prof, config=FAST_CFG, seed=0)
    plans = []
    for _ in range(6):
        tr = r.prepare()
        plans.append(tr.plan)
        r.answer(tr, True, 400)
    s = prof.sessions()[0]
    for e in "LR":
        want = [1.0 if p.a_first else -1.0 for p in plans if p.ear == e]
        assert s.trials_for(e).order.tolist() == want
        assert r.scheduler.ears[e].trials().order.tolist() == want


def test_sessions_with_different_stimuli_pool_with_their_keys(tmp_path):
    from flatiron.audio.stimulus import StimulusSpec

    prof = Profile.open("x", root=tmp_path)
    for spec in (StimulusSpec(), StimulusSpec(bandwidth_oct=1 / 24), StimulusSpec(kind="tone")):
        r = TrialRunner(prof, RunSettings(stimulus=spec), config=FAST_CFG, seed=0)
        for _ in range(4):
            r.answer(r.prepare(), True, 400)
        assert set(r.scheduler.ears["L"].trials().stimulus[-2:]) == {
            {"noise": f"noise:{spec.bandwidth_oct:.6f}", "tone": "point"}[spec.kind]}
        r.finish()
    trials, _ = prof.history()
    assert sorted(set(trials["L"].stimulus)) == ["noise:0.041667", "noise:0.083333", "point"]
    # A later session's scheduler starts from all of it, keys included.
    r = TrialRunner(prof, config=FAST_CFG, seed=1)
    assert len(set(r.scheduler.ears["L"].trials().stimulus)) == 3
