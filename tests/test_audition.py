import numpy as np
import pytest

from flatiron.apo import ApoFilter
from flatiron.audio.player import NullPlayer
from flatiron.audio.preview import PreviewEQ
from flatiron.ui.audition import AuditionApp, load_source, pink_noise, versions

EQ = PreviewEQ({"L": [ApoFilter("PK", 6000, -6, 4)], "R": [ApoFilter("PK", 3000, 4, 2)]}, -4.5, "abc")


def test_versions_are_level_matched_and_loop_cleanly():
    src = pink_noise(2.0)
    off, on = versions(src, EQ)
    assert np.allclose(off, src * 10 ** (-4.5 / 20), atol=1e-6)
    # The filtered loop is in steady state at the seam: no jump at the wrap.
    seam = np.abs(on[0] - on[-1]).max()
    assert seam < 4 * np.abs(np.diff(on, axis=0)).mean()


def test_load_source_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_source(str(tmp_path / "nope.wav"))
    (tmp_path / "x.ogg").write_bytes(b"")
    with pytest.raises(ValueError, match="only WAV, FLAC and MP3"):
        load_source(str(tmp_path / "x.ogg"))
    (tmp_path / "x.flac").write_bytes(b"not audio")
    with pytest.raises(ValueError, match="can't decode"):
        load_source(str(tmp_path / "x.flac"))


def test_wav_source_is_resampled_to_stereo_48k(tmp_path):
    from scipy.io import wavfile

    wavfile.write(tmp_path / "m.wav", 44100, (np.sin(np.arange(44100) / 10) * 20000).astype(np.int16))
    name, x = load_source(str(tmp_path / "m.wav"))
    assert name == "m.wav" and x.shape == (48000, 2) and np.abs(x).max() <= 1.0


@pytest.mark.parametrize("ext", ["flac", "mp3"])
def test_compressed_sources(tmp_path, ext):
    import soundfile

    tone = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(48000) / 48000)
    soundfile.write(tmp_path / f"m.{ext}", np.column_stack([tone, -tone]), 48000)
    name, x = load_source(str(tmp_path / f"m.{ext}"))
    assert name == f"m.{ext}" and x.shape[1] == 2 and abs(x.shape[0] - 48000) < 3000
    mid = x[10000:40000]
    assert np.sqrt(np.mean(mid**2, axis=0)) == pytest.approx([0.5 / np.sqrt(2)] * 2, rel=0.05)
    assert np.corrcoef(mid[:, 0], mid[:, 1])[0, 1] < -0.99  # the channels kept apart


async def test_toggle_blind_and_reveal():
    player = NullPlayer()
    app = AuditionApp([("pink", pink_noise(1.0)), ("pink2", pink_noise(1.0))], EQ, player, "test")
    async with app.run_test() as pilot:
        state = lambda: str(app.query_one("#state").render())
        assert "ON" in state() and len(player.played) == 1
        await pilot.press("space")
        assert "OFF" in state() and len(player.played) == 2
        await pilot.press("b")
        assert state() in ("A", "B")
        a_label = state()
        await pilot.press("space")
        assert state() != a_label
        await pilot.press("r")
        assert "Correction" in state()
        await pilot.press("n")
        assert "2 of 2" in str(app.query_one("#status").render())


async def test_volume_is_kept_per_profile_apart_from_the_listening_level(tmp_path):
    from flatiron.session.store import Profile

    prof = Profile.open("a", root=tmp_path)
    prof.set_level(-35.0)
    player = NullPlayer()
    app = AuditionApp([("pink", pink_noise(1.0))], EQ, player, "test", prof)
    async with app.run_test() as pilot:
        first = player.played[-1]
        await pilot.press("minus", "minus", "minus")
        assert np.abs(player.played[-1]).max() == pytest.approx(np.abs(first).max() * 10 ** (-3 / 20), rel=1e-5)
        assert "volume -3 dB" in str(app.query_one("#status").render())
    assert prof.read()["audition_gain_db"] == -3.0 and prof.last_level() == -35.0
    app = AuditionApp([("loud", pink_noise(1.0) * 4)], EQ, NullPlayer(), "test", prof)  # about −8 dB RMS
    assert app.volume_db == -3.0  # the next run starts there
    async with app.run_test() as pilot:
        for _ in range(40):
            await pilot.press("plus")
        assert app.volume_db == 10.0 and "clipping" in str(app.query_one("#status").render())
