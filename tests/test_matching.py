"""Loudness matching, the measured range and the debug readout."""

import numpy as np
import pytest

from flatiron.model.grid import cover, in_range, log_grid
from flatiron.model.probit import Trials
from flatiron.session.runner import RunSettings, TrialRunner
from flatiron.session.scheduler import Scheduler, SchedulerConfig
from flatiron.session.store import Profile
from flatiron.sim.listener import EarTruth, SimulatedListener

FAST_CFG = SchedulerConfig(block_size=20, min_trials_noise_fit=10_000)


def test_cover_extends_without_moving_points():
    g = log_grid(1000, 8000)
    assert cover(g, 1000, 8000) is g  # already covered
    c = cover(g, 500, 16000)
    assert c[0] == pytest.approx(500) and c[-1] == pytest.approx(16000)
    assert np.all(np.isin(g, c))
    assert np.allclose(np.diff(np.log2(c)), 1 / 24, atol=0.005)
    assert in_range(c, 1000, 8000).sum() == g.size


def hearing_loss_listener(seed=0):
    """A steep treble loss on top of the usual contour: ~25 dB down by 14 kHz."""
    loss = lambda f: -12.0 * np.clip(np.log2(np.asarray(f) / 5000.0), 0, None)
    return SimulatedListener({"L": EarTruth(deviation=loss)}, seed=seed)


def heard_levels(match: bool):
    lis = hearing_loss_listener()
    sch = Scheduler(SchedulerConfig(match=match, block_size=50), seed=3, ears=("L",))
    heard, plans = [], []
    for k in range(250):
        p = sch.next_trial()
        sch.record(p, bool(lis.respond(p.ear, p.f_a, p.f_b, p.offset, p.a_first)))
        if k >= 150:
            plans.append(p)
            heard += [lis.heard_level("L", p.f_a)[0] + p.delta_a, lis.heard_level("L", p.f_b)[0] + p.delta_b]
    return np.array(heard), plans


def test_loudness_matching_evens_out_trials():
    flat, _ = heard_levels(False)
    matched, plans = heard_levels(True)
    # The same trials (a seed gives the same ones either way), but the bursts'
    # loudness evens out as the curve is learnt. Not fully: the broad shape
    # takes a while, and the cap holds back the deepest treble.
    assert np.std(matched) < 0.6 * np.std(flat)
    assert np.ptp(matched) < 0.7 * np.ptp(flat)
    # The quiet treble is raised and the ear-gain region lowered, the boost capped.
    assert max(max(p.delta_a, p.delta_b) for p in plans) <= 12 + 1e-9
    assert any(max(p.delta_a, p.delta_b) > 5 for p in plans if min(p.f_a, p.f_b) > 10_000)
    assert any(min(p.delta_a, p.delta_b) < -3 for p in plans if 2500 < max(p.f_a, p.f_b) < 4000)


def test_matching_moves_only_the_common_level():
    lis = hearing_loss_listener()
    sch = Scheduler(SchedulerConfig(match=True, match_boost_db=4.0), seed=5, ears=("L",))
    capped = 0
    for _ in range(150):
        p = sch.next_trial()
        g = sch.match_gain("L", [p.f_a, p.f_b])
        common = 0.5 * (p.delta_a + p.delta_b)
        # The offset splits evenly around the pair's matched level; the cap
        # moves both bursts down together.
        assert max(p.delta_a, p.delta_b) <= 4 + 1e-9
        if max(p.delta_a, p.delta_b) < 4 - 1e-9:
            assert common == pytest.approx(np.mean(g))
        else:
            capped += 1
            assert common <= np.mean(g) + 1e-9
        if p.catch:
            assert abs(p.offset) == pytest.approx(6)
        sch.record(p, bool(lis.respond(p.ear, p.f_a, p.f_b, p.offset, p.a_first)))
    assert capped  # the treble wants more than 4 dB


def test_trials_stay_in_the_measured_range():
    sch = Scheduler(seed=4, ears=("L",), measured=(1500, 9000))
    g = sch.ears["L"].grid
    assert g[0] == pytest.approx(1500) and g[-1] == pytest.approx(9000)
    # An older, wider grid is kept (its trials still count), but not used for new trials.
    wide = log_grid()
    sch.load_history("L", Trials([1000.0, 2000.0], [1100.0, 2200.0], [0.0, 0.0], [True, False]), wide)
    assert sch.ears["L"].grid[0] == pytest.approx(850)
    for _ in range(150):
        p = sch.next_trial()
        assert 1500 * 0.999 <= min(p.f_a, p.f_b) and max(p.f_a, p.f_b) <= 9000 * 1.001
        sch.record(p, True)
    # A range wider than the grid extends it.
    sch = Scheduler(seed=4, ears=("L",), measured=(600, 16000))
    sch.load_history("L", Trials([1000.0], [1100.0], [0.0], [True]), wide)
    g = sch.ears["L"].grid
    assert g[0] == pytest.approx(600) and g[-1] == pytest.approx(16000) and np.all(np.isin(wide, g))


def test_runner_uses_the_profile_range_and_reports_what_it_played(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    prof.update(measured={"lo": 2000.0, "hi": 10000.0})
    assert prof.measured == (2000.0, 10000.0)
    r = TrialRunner(prof, RunSettings(level_dbfs=-35), config=FAST_CFG, seed=2)
    assert r.session.measured == [2000.0, 10000.0] and r.session.scheduler["match"]
    for _ in range(20):
        tr = r.prepare()
        p = tr.plan
        assert 2000 * 0.999 <= min(p.f_a, p.f_b) and max(p.f_a, p.f_b) <= 10000 * 1.001
        first, second = ((p.f_a, p.delta_a), (p.f_b, p.delta_b))[:: 1 if p.a_first else -1]
        assert tr.freqs == (first[0], second[0])
        assert tr.levels[0] == pytest.approx(-35 + p.rove_db + first[1] + tr.limited_db)
        assert tr.levels[0] - tr.levels[1] == pytest.approx(first[1] - second[1])
        rec = r.answer(tr, first_louder=True, rt_ms=400, peeked=True)
        assert rec.peeked
    with pytest.raises(ValueError):
        RunSettings(match_boost_db=30)




def cli_env(tmp_path, monkeypatch):
    from flatiron.cli import main

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("FLATIRON_LISTENER", raising=False)
    return main


def test_listener_default_and_profile_override(tmp_path, monkeypatch, capsys):
    from flatiron.fit.run import profile_settings
    from flatiron.session.store import current_listener

    main = cli_env(tmp_path, monkeypatch)
    prof = Profile.open("HD 600")
    # The only listener and the only profile, no name: the setting names say which.
    assert main(["config", "--default-max-measured", "12k"]) == 0
    out = capsys.readouterr().out
    assert "default-max-measured: 15k -> 12k" in out
    # The profile follows, and its correction range with it (same margin inside).
    assert "HD 600: max-measured 15k -> 12k, max-corrected 13k -> 10.4k" in out
    assert prof.measured == (850.0, 12000.0)
    assert profile_settings(prof).f_hi == pytest.approx(10400)
    assert "settings" not in prof.read() or "f_hi" not in prof.read()["settings"]  # still a default
    # A profile's own end overrides the listener's.
    assert main(["config", "--max-measured", "14k"]) == 0
    out = capsys.readouterr().out
    assert "max-measured: 12k -> 14k" in out and "max-corrected: 10.4k -> 12.1k" in out
    assert main(["config", "--default-max-measured", "10k"]) == 0
    assert prof.measured == (850.0, 14000.0)  # kept its own
    # An explicit correction range stays put.
    assert main(["config", "--max-corrected", "11k"]) == 0
    capsys.readouterr()
    assert main(["config", "--reset", "max-measured"]) == 0
    out = capsys.readouterr().out
    assert "max-measured: 14k -> 10k" in out and "max-corrected" not in out
    assert profile_settings(prof).f_hi == 11000
    # Showing: the profile, with where its range comes from.
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "    max-measured         10k   (the listener's)" in out
    assert main(["config", "default"]) == 0  # the listener by name
    assert "* default-max-measured 10k   (default 15k)" in capsys.readouterr().out
    assert main(["config", "--reset", "default-max-measured"]) == 0
    assert current_listener().measured_default == (850.0, 15000.0)


def test_config_scope_errors(tmp_path, monkeypatch, capsys):
    main = cli_env(tmp_path, monkeypatch)
    Profile.open("HD 600")
    assert main(["config", "HD 600", "--default-max-measured", "12k"]) == 1
    assert "is a profile" in capsys.readouterr().out
    assert main(["config", "--default-max-measured", "12k", "-w", "0.5"]) == 1
    assert main(["config", "--reset", "width,default-max-measured"]) == 1
    assert main(["config", "--max-measured", "11k", "--min-measured", "10k"]) == 1  # under an octave
    assert main(["config", "--default-max-measured", "20k"]) == 1
    # A listener default that a profile's own end would make invalid is refused.
    assert main(["config", "--min-measured", "7k"]) == 0
    assert main(["config", "--default-max-measured", "12k"]) == 1
    assert "no valid measured range" in capsys.readouterr().out
    # Several profiles and no name: the listener's settings are shown.
    Profile.open("Sundara")
    capsys.readouterr()
    assert main(["config"]) == 0
    assert "Listener settings for 'default'" in capsys.readouterr().out
