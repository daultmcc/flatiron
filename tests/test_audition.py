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
    (tmp_path / "x.flac").write_bytes(b"")
    with pytest.raises(ValueError):
        load_source(str(tmp_path / "x.flac"))


def test_wav_source_is_resampled_to_stereo_48k(tmp_path):
    from scipy.io import wavfile

    wavfile.write(tmp_path / "m.wav", 44100, (np.sin(np.arange(44100) / 10) * 20000).astype(np.int16))
    name, x = load_source(str(tmp_path / "m.wav"))
    assert name == "m.wav" and x.shape == (48000, 2) and np.abs(x).max() <= 1.0


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
