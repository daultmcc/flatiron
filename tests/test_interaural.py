"""Cross-ear, centring and threshold data in the two-ear fit (DESIGN.md, "Using cross-ear trials")."""

import numpy as np
import pytest
from scipy.special import ndtr

from flatiron.session.centring import CentringRunner, CentringSettings
from flatiron.session.runner import RunSettings, TrialRunner
from flatiron.session.scheduler import SchedulerConfig
from flatiron.session.store import Profile, current_listener
from flatiron.sim.listener import SimulatedListener, design_example

FAST_CFG = SchedulerConfig(block_size=20, min_trials_noise_fit=10_000)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A profile whose listener hears the left ear 3 dB louder in cross-ear
    comparisons only: the ears and channels are the same."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    prof = Profile.open("hp", root=current_listener().root)
    lis = SimulatedListener({e: design_example("L") for e in "LR"}, seed=3, channel_offset_db=3.0)
    r = TrialRunner(prof, RunSettings(cross_rate=0.3), config=FAST_CFG, seed=1)
    for k in range(260):
        tr = r.prepare()
        p = tr.plan
        a = bool(lis.respond(p.ear, p.f_a, p.f_b, p.offset, p.a_first))
        r.answer(tr, first_louder=(a == p.a_first), rt_ms=400, t=k)
    r.finish()
    return prof


def centre(prof, n=150, balance=0.0, sigma=2.0, seed=0):
    rng = np.random.default_rng(seed)
    r = CentringRunner(prof, CentringSettings(), seed=seed)
    for i in range(n):
        tr = r.prepare()
        r.answer(tr, bool(rng.random() < ndtr((tr.plan.ild_db + balance) / sigma)), 500, t=i)
    r.finish()


def test_centring_separates_the_listeners_bias_from_the_channels(profile):
    from flatiron.fit.run import current_fit

    centre(profile)
    info = current_fit(profile).fits["L"].interaural
    assert info["cross_trials"] > 50 and info["centring_trials"] == 150 and info["threshold"] is None
    assert info["b_db"] == pytest.approx(3.0, abs=1.2)
    assert info["c_db"] == pytest.approx(0.0, abs=0.8)
    # Cached: the same again without refitting.
    assert current_fit(profile).fits["L"].interaural == info


def test_without_centring_the_prior_splits_them_and_the_setting_turns_it_off(profile):
    from flatiron.cli import main
    from flatiron.fit.run import current_fit
    from flatiron.report.html import interaural_note

    pf = current_fit(profile)
    info = pf.fits["L"].interaural
    # One profile, cross-ear trials only: b + c is what's measured, c held near 0 by its prior.
    assert info["b_db"] + info["c_db"] == pytest.approx(3.0, abs=1.2)
    assert abs(info["c_db"]) < abs(info["b_db"])
    assert "cross-ear bias" in interaural_note(pf)
    assert main(["config", "hp", "--no-cross-ear-fit"]) in (0, None)
    pf = current_fit(profile)
    assert pf.fits["L"].interaural is None and interaural_note(pf) is None
