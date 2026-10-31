import numpy as np
import pytest

from flatiron import FS
from flatiron.audio.limiter import CEILING_DB, db_to_amp, limit, peak_db, render_pair
from flatiron.audio.stimulus import StimulusSpec, band_edges, make_stimulus


def spectrum_db(x, fs=FS, pad=8):
    n = x.size * pad
    s = np.abs(np.fft.rfft(x, n)) ** 2
    return np.fft.rfftfreq(n, 1 / fs), 10 * np.log10(s + 1e-30)


@pytest.mark.parametrize("fc", [850, 1000, 4000, 8400, 15000])
@pytest.mark.parametrize("spec", [StimulusSpec(), StimulusSpec(bandwidth_oct=1 / 24),
                                  StimulusSpec(kind="warble"), StimulusSpec(kind="tone")])
def test_band_rejection_one_octave_out(fc, spec):
    x = make_stimulus(fc, spec, seed=3)
    f, s = spectrum_db(x)
    bw = spec.bandwidth_oct if spec.kind == "noise" else 2 * spec.warble_depth_oct
    lo, hi = band_edges(fc, bw)
    inband = s[(f >= lo) & (f <= hi)].max()
    out = s[(f <= lo / 2) | ((f >= 2 * hi) & (f < FS / 2))]
    assert inband - out.max() > 40


@pytest.mark.parametrize("kind", ["noise", "warble", "tone"])
def test_unit_rms(kind):
    x = make_stimulus(3000, StimulusSpec(kind=kind))
    assert np.sqrt(np.mean(x**2)) == pytest.approx(1.0, rel=1e-9)
    assert x.size == int(0.4 * FS)


def test_frozen_tokens():
    a = make_stimulus(5000, seed=1)
    assert np.array_equal(a, make_stimulus(5000, seed=1))
    assert not np.allclose(a, make_stimulus(5000, seed=2))
    assert not np.allclose(a[:100], make_stimulus(5010, seed=1)[:100])


def test_fresh_noise_uses_rng():
    spec = StimulusSpec(fresh_noise=True)
    rng = np.random.default_rng(0)
    assert not np.allclose(make_stimulus(5000, spec, rng=rng), make_stimulus(5000, spec, rng=rng))


def test_ramps_start_and_end_at_zero():
    x = make_stimulus(2000)
    assert abs(x[0]) < 1e-9 and abs(x[-1]) < 0.01


def test_rejects_bad_settings():
    with pytest.raises(ValueError):
        StimulusSpec(kind="chirp")
    with pytest.raises(ValueError):
        StimulusSpec(duration=1.0)


@pytest.mark.parametrize("level", [-60, -30, -12, 0, 20])
def test_limiter_never_exceeds_ceiling(level):
    x = make_stimulus(4000) * db_to_amp(level)
    y = limit(x).signal
    assert peak_db(y) <= CEILING_DB + 1e-9


def test_render_pair_preserves_level_difference_when_limiting():
    a, b = make_stimulus(4000, seed=0), make_stimulus(4500, seed=0)
    buf, res = render_pair(a, b, 0.0, -5.0, 0.3, "R", FS)
    assert res.gain_db < 0 and res.over_target
    assert np.all(buf[:, 0] == 0)  # digital silence in the other ear
    n = a.size
    ra = np.sqrt(np.mean(buf[:n, 1].astype(float) ** 2))
    rb = np.sqrt(np.mean(buf[-n:, 1].astype(float) ** 2))
    assert 20 * np.log10(ra / rb) == pytest.approx(5.0, abs=0.05)
    assert buf.shape == (2 * n + int(0.3 * FS), 2) and buf.dtype == np.float32


def test_default_levels_stay_under_peak_target():
    a, b = make_stimulus(4000, seed=0), make_stimulus(4500, seed=0)
    _, res = render_pair(a, b, -40 + 2, -40 + 8, 0.3, "L", FS)
    assert not res.over_target and res.gain_db == 0
