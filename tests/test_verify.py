import numpy as np
import pytest

from flatiron.apo import ApoFilter
from flatiron.audio.preview import PreviewEQ
from flatiron.audio.stimulus import StimulusSpec, make_stimulus
from flatiron.session.runner import RunSettings, TrialRunner
from flatiron.session.scheduler import SchedulerConfig
from flatiron.session.store import Profile

CFG = SchedulerConfig(block_size=20, min_trials_noise_fit=10_000)
EQ = PreviewEQ({"L": [ApoFilter("PK", 6000, -6, 4)], "R": [ApoFilter("PK", 3000, 4, 2)]}, -4.5, "abc")


def test_preview_gain_on_a_tone_matches_the_filter():
    from flatiron.audio.biquad import PeakingFilter, response_db

    for fc, ear, f in ((6000, "L", PeakingFilter(6000, -6, 4)), (4000, "R", PeakingFilter(3000, 4, 2))):
        x = make_stimulus(fc, StimulusSpec(kind="tone"))
        _, g = EQ.burst_gain_db(x, ear)
        assert g == pytest.approx(response_db([f], [fc])[0], abs=0.15)


def test_preview_apply_is_per_channel_with_preamp():
    x = np.zeros((4800, 2))
    x[0] = 1.0
    y = EQ.apply(x)
    h = lambda c: 20 * np.log10(np.abs(np.fft.rfft(y[:, c])))
    f = np.fft.rfftfreq(4800, 1 / 48000)
    k6, k3 = np.argmin(abs(f - 6000)), np.argmin(abs(f - 3000))
    assert h(0)[k6] == pytest.approx(-6 - 4.5, abs=0.2) and h(1)[k3] == pytest.approx(4 - 4.5, abs=0.2)
    assert EQ.to_json() == PreviewEQ.from_json(EQ.to_json()).to_json()


def test_verify_trials_record_the_preview_as_known_offsets(tmp_path):
    prof = Profile.open("x", root=tmp_path)
    r = TrialRunner(prof, RunSettings(preview=EQ), config=CFG, seed=0)
    for _ in range(10):
        tr = r.prepare()
        rec = r.answer(tr, True, 400)
        assert (rec.eq_gain_a, rec.eq_gain_b) == (tr.preview_gain_a, tr.preview_gain_b)
        assert rec.offset == pytest.approx(tr.plan.offset + tr.preview_gain_a - tr.preview_gain_b)
    s = prof.sessions()[0]
    assert s.preview["fit_hash"] == "abc"
    assert any(abs(t.eq_gain_a) > 0.5 for t in s.trials)


@pytest.fixture(scope="module")
def verified(tmp_path_factory):
    """A profile with an incomplete (1-band) correction and a verify session."""
    from flatiron.fit.pipeline import CorrectionSettings
    from flatiron.fit.run import current_fit, set_checkpoint, set_profile_settings
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.sim.listener import design_example
    from flatiron.sim.profile import simulate_profile

    prof = Profile.open("sim", root=tmp_path_factory.mktemp("v"))
    truth = {e: design_example(e) for e in "LR"}
    # These tests are about leftovers and checkpoints, not the scheduler: it
    # refits every 10 answers and keeps the noise at the prior, much quicker.
    quick = SchedulerConfig(refit_every=10, min_trials_noise_fit=10_000)
    simulate_profile(prof, truth, sessions=3, trials_per_ear=200, seed=3, config=quick)
    set_profile_settings(prof, CorrectionSettings(bands=1, preserve_tonality=False))
    pf = current_fit(prof)
    set_checkpoint(prof, pf)
    simulate_profile(prof, truth, sessions=1, trials_per_ear=75, seed=4, config=quick,
                     preview=PreviewEQ.from_fit(pf.summary()))
    return prof


def test_leftovers_find_what_the_correction_missed(verified):
    from flatiron.fit.run import checkpoint, leftovers

    _, left = leftovers(verified, checkpoint(verified))
    # The 1-band fit took one of the 6.2 kHz peak and the 8.4 kHz dip, whichever is
    # larger as measured; the rest of the ground truth is left over.
    fcs = [x.fc for x in left["L"]]
    taken = checkpoint(verified)["L"][0].fc
    near = lambda f0: any(abs(np.log2(f / f0)) < 1 / 6 for f in fcs)
    missed = [f0 for f0 in (6200, 8400) if abs(np.log2(taken / f0)) > 1 / 6]
    assert len(missed) == 1 and near(missed[0]) and near(4000)


def test_checkpoint_keeps_the_filters_and_adds_new_ones(verified):
    from flatiron.fit.pipeline import CorrectionSettings
    from flatiron.fit.run import checkpoint, current_fit, set_profile_settings

    before = checkpoint(verified)
    set_profile_settings(verified, CorrectionSettings(bands=8, preserve_tonality=False))
    pf = current_fit(verified)
    for e, c in pf.correction.ears.items():
        assert c.fixed == before[e] and all(f in c.filters for f in before[e])
        assert len(c.added) >= 1 and len(c.filters) <= 8
        # The design's 6.2 kHz peak and 8.4 kHz dip (1/12 octave higher on the
        # right): whichever the 1-band checkpoint didn't take gets a filter now.
        near = lambda fs, f0: any(abs(np.log2(f.fc / (f0 * (1 if e == "L" else 2 ** (1 / 12))))) < 1 / 12 for f in fs)
        missed = [f0 for f0 in (6200, 8400) if not near(before[e], f0)]
        assert missed and all(near(c.added, f0) for f0 in missed)


def test_commands_that_need_a_fit_need_trials(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    Profile.open("HD 600")
    for cmd in (["verify", "-n"], ["audition", "-n"], ["report"], ["export", "-t", "apo"],
                ["config", "--checkpoint"]):
        assert main(cmd) == 1 and "Can't fit 'HD 600' yet" in capsys.readouterr().out
