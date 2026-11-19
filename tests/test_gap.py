"""The listener's loudness-threshold gap (DESIGN.md, "Threshold-anchored reference")."""

import numpy as np
import pytest

from flatiron.fit.gap import KNOTS_HZ, _Member, fit_gap, hats, listener_gap, low_hats
from flatiron.model.grid import log_grid
from flatiron.session.runner import RunSettings, TrialRunner
from flatiron.session.scheduler import SchedulerConfig
from flatiron.session.store import Profile, current_listener
from flatiron.session.threshold import ThresholdRunner, ThresholdSettings
from flatiron.sim.listener import SimulatedListener, ThresholdListener, design_example

G_TRUE = np.array([0.0, -3.0, -5.0, 5.0, 10.0])  # at the knots: a 15 dB rise from 4 to 13 kHz
centre = lambda v: v - v.mean()


def test_fit_recovers_the_gap_each_profiles_low_end_and_the_ripple():
    rng = np.random.default_rng(0)
    grid = log_grid(1000, 13000)
    x = np.log2(grid)
    K = np.exp(-0.5 * ((x[:, None] - x[None, :]) / 0.1) ** 2)
    chol = np.linalg.cholesky(K + 1e-9 * np.eye(grid.size))
    deltas = {"a": [2.0, 1.0], "b": [-1.0, 0.0], "c": [0.0, -2.0]}
    members = []
    for slug, delta in deltas.items():
        d, V0 = {}, {}
        for e in "LR":
            noise = 0.5 * rng.normal(size=grid.size) + 1.0 * chol @ rng.normal(size=grid.size)
            d[e] = hats(grid, KNOTS_HZ) @ G_TRUE + low_hats(grid) @ np.array(delta) + rng.normal() * 20 + noise
            V0[e] = 0.25 * np.eye(grid.size)
        members.append(_Member(slug, grid, d, V0, 300))
    gap = fit_gap(members)
    assert np.asarray(gap.g) == pytest.approx(centre(G_TRUE), abs=1.0)
    assert gap.ripple_sd == pytest.approx(1.0, rel=0.4)
    for slug, delta in deltas.items():
        # δ is relative to g's own low end, which the members share: compare differences.
        got = np.array(gap.profiles[slug]["delta"]) - np.mean([gap.profiles[s]["delta"] for s in deltas], axis=0)
        want = np.array(delta) - np.mean(list(deltas.values()), axis=0)
        assert got == pytest.approx(want, abs=1.5)
    sd = np.sqrt(np.diag(np.asarray(gap.cov)))
    assert np.all(sd < 1.5) and gap.curve([500.0, 20000.0]) == pytest.approx(gap.curve([1000.0, 13000.0]))


def test_from_sessions(tmp_path, monkeypatch):
    """A profile's loudness and threshold sessions, the listener's thresholds
    being their loudness curve less the gap."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = current_listener().root
    prof = Profile.open("hp", root=root)
    truth = {e: design_example(e) for e in "LR"}
    lis = SimulatedListener(truth, seed=1)
    r = TrialRunner(prof, RunSettings(), config=SchedulerConfig(block_size=20, min_trials_noise_fit=10_000), seed=1)
    for k in range(300):
        tr = r.prepare()
        p = tr.plan
        a = bool(lis.respond(p.ear, p.f_a, p.f_b, p.offset, p.a_first))
        r.answer(tr, first_louder=(a == p.a_first), rt_ms=400, t=k)
    r.finish()
    assert listener_gap(root) is None  # no thresholds yet
    gap_at = lambda f: hats(f, KNOTS_HZ) @ G_TRUE
    thr = {e: (lambda e: lambda f: -(truth[e].level(f) - gap_at(f)) - 70.0)(e) for e in "LR"}
    tl = ThresholdListener(thr, seed=2)
    t = ThresholdRunner(prof, ThresholdSettings(), seed=2, volume=False)
    for k in range(320):
        tr = t.prepare()
        t.answer(tr, tl.answer(tr.plan), 500, t=k)
    t.finish()
    gap = listener_gap(root)
    assert gap is not None and gap.profiles["hp"]["trials"] == 320
    g = np.asarray(gap.g)
    assert g[4] - g[2] == pytest.approx(15.0, abs=4.0)  # the treble rise
    assert listener_gap(root).key == gap.key  # cached
    # The profile's own fit takes its thresholds too (as an observation of the channel balance).
    from flatiron.fit.run import current_fit

    info = current_fit(prof).fits["L"].interaural
    assert info is not None and info["threshold"] is not None
    # With the threshold fit: the trials are rows of the fit, and diffuse is anchored.
    from dataclasses import replace

    from flatiron.fit.consensus import anchored_broad
    from flatiron.fit.reference import Anchor, reference
    from flatiron.fit.run import profile_settings, set_profile_settings

    plain = current_fit(prof)
    set_profile_settings(prof, replace(profile_settings(prof), threshold_fit=True))
    pf = current_fit(prof)
    info = pf.fits["L"].interaural
    assert info["threshold_fit"]["trials"] == 320 and info["threshold"] is None  # not folded in as well
    assert pf.gap is not None and not pf.anchor_missing()
    ec = pf.correction.ears["L"]
    assert ec.E == pytest.approx(reference("diffuse", ec.grid, 60, Anchor.of(pf.gap, "hp")))
    assert np.abs(ec.E - plain.correction.ears["L"].E).max() > 3
    # A consensus with the anchored diffuse field's broad shape.
    set_profile_settings(prof, replace(profile_settings(prof), reference="consensus:hp", consensus_broad="diffuse"))
    pf = current_fit(prof)
    x = pf.correction.ears["L"].consensus
    assert x.broad == pytest.approx(anchored_broad(x.grid, Anchor.of(pf.gap, "hp"))[0])


def test_anchored_references_follow_the_gap():
    from flatiron import iso226, iso389_7
    from flatiron.fit.reference import Anchor, reference

    a = Anchor(KNOTS_HZ, tuple(centre(G_TRUE)), tuple(tuple(r) for r in 0.01 * np.eye(5)))
    f = log_grid(1000, 13000)
    want = hats(f, KNOTS_HZ) @ G_TRUE - iso226.threshold(f) + iso389_7.diffuse_minus_free(f)
    assert reference("diffuse", f, 60, a) == pytest.approx(centre(want), abs=1e-9)
    assert reference("iso226", f, 60, a) == pytest.approx(centre(hats(f, KNOTS_HZ) @ G_TRUE - iso226.threshold(f)),
                                                          abs=1e-9)
    assert reference("none", f, 60, a) == pytest.approx(np.zeros(f.size))  # not anchored
    assert reference("diffuse", f, 60) == pytest.approx(reference("diffuse", f, 60, None))


def test_config_rules_for_the_threshold_fit(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.fit.run import profile_settings

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    prof = Profile.open("hp", root=current_listener().root)
    assert main(["config", "hp", "--consensus-broad", "diffuse"]) == 1
    assert "needs --threshold-fit" in capsys.readouterr().out
    assert main(["config", "hp", "--threshold-fit"]) in (0, None)
    assert "there's no estimate of your loudness-threshold gap" in capsys.readouterr().out  # no threshold sessions
    assert main(["config", "hp", "--consensus-broad", "diffuse"]) in (0, None)
    assert main(["config", "hp", "--no-threshold-fit"]) == 1
    assert "set --consensus-broad median first" in capsys.readouterr().out
    assert profile_settings(prof).threshold_fit and profile_settings(prof).consensus_broad == "diffuse"
    assert main(["config", "hp", "--consensus-broad", "median", "--no-threshold-fit"]) in (0, None)
    assert not profile_settings(prof).threshold_fit
    main(["config", "hp", "--threshold-fit"])
    capsys.readouterr()
    main(["config", "hp"])
    assert "(unused with --threshold-fit)" in capsys.readouterr().out  # phon
