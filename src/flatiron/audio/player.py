"""Blocking playback of prepared stereo buffers."""

import numpy as np

from flatiron import FS
from flatiron.audio.devices import sd
from flatiron.audio.limiter import CEILING_DB, db_to_amp


def _guard(buf: np.ndarray) -> np.ndarray:
    # Final guard: nothing leaves Flatiron above the hard ceiling.
    c = float(db_to_amp(CEILING_DB))
    return np.clip(buf, -c, c).astype(np.float32)


def play(buf: np.ndarray, device=None, fs: int = FS) -> None:
    s = sd()
    s.play(_guard(buf), samplerate=fs, device=device, blocking=True)


def play_loop(buf: np.ndarray, device=None, fs: int = FS) -> None:
    """Loop until stop() is called."""
    sd().play(_guard(buf), samplerate=fs, device=device, loop=True)


def stop() -> None:
    sd().stop()
