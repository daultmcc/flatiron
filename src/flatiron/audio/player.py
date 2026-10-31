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


class SoundDevicePlayer:
    """Non-blocking playback for the UI. `latency` is the output latency (s)
    of the last stream, so the UI can line panel lights and reaction times up
    with what the listener actually hears."""

    def __init__(self, device=None, fs: int = FS):
        self.device = device
        self.fs = fs
        self.latency = 0.0

    def start(self, buf: np.ndarray, loop: bool = False) -> None:
        s = sd()
        s.play(_guard(buf), samplerate=self.fs, device=self.device, loop=loop)
        try:
            self.latency = float(s.get_stream().latency)
        except Exception:
            self.latency = 0.0

    def stop(self) -> None:
        sd().stop()


class NullPlayer:
    """Plays nothing (tests and --dry-run). Records what it was given."""

    latency = 0.0

    def __init__(self):
        self.played: list[np.ndarray] = []

    def start(self, buf: np.ndarray, loop: bool = False) -> None:
        self.played.append(buf)

    def stop(self) -> None:
        pass
