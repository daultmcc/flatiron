"""Playback through one persistent output stream.

Opening and closing a stream per sound can click on some backends (the end of
every buffer becomes a stream teardown), and stopping mid-buffer cuts the
waveform. So one stream stays open and plays silence between sounds, and every
early stop is a short raised-cosine fade.
"""

import threading
import time

import numpy as np

from flatiron import FS
from flatiron.audio.devices import sd
from flatiron.audio.limiter import CEILING_DB, db_to_amp

FADE_S = 0.01


def _guard(buf: np.ndarray) -> np.ndarray:
    # Final guard: nothing leaves Flatiron above the hard ceiling.
    c = float(db_to_amp(CEILING_DB))
    return np.clip(buf, -c, c).astype(np.float32)


class Mixer:
    """Sample-accurate playback state, independent of any audio API.

    start() while something is playing fades the old sound out first, then
    starts the new one; stop() fades out. render() fills one output block.
    """

    def __init__(self, channels: int = 2, fs: int = FS, fade_s: float = FADE_S):
        self.channels = channels
        self.n_fade = max(1, int(round(fade_s * fs)))
        k = np.arange(1, self.n_fade + 1) / self.n_fade
        self.fade_curve = (0.5 * (1 + np.cos(np.pi * k))).astype(np.float32)  # just below 1 → 0
        self.buf: np.ndarray | None = None
        self.pos = 0
        self.loop = False
        self.fade_left: int | None = None
        self.pending: tuple[np.ndarray, bool] | None = None

    @property
    def playing(self) -> bool:
        return self.buf is not None or self.pending is not None

    def start(self, buf: np.ndarray, loop: bool = False) -> None:
        buf = np.asarray(buf, np.float32)
        if buf.ndim == 1:
            buf = np.repeat(buf[:, None], self.channels, axis=1)
        if self.buf is not None:
            self.stop()
            self.pending = (buf, loop)
        else:
            self.buf, self.pos, self.loop, self.pending = buf, 0, loop, None

    def stop(self) -> None:
        self.pending = None
        if self.buf is not None and self.fade_left is None:
            self.fade_left = self.n_fade

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, self.channels), np.float32)
        i = 0
        while i < frames:
            if self.buf is None:
                if self.pending is None:
                    break
                (self.buf, self.loop), self.pos, self.pending = self.pending, 0, None
            n = min(frames - i, self.buf.shape[0] - self.pos)
            if self.fade_left is not None:
                n = min(n, self.fade_left)
            chunk = self.buf[self.pos:self.pos + n]
            if self.fade_left is not None:
                k0 = self.n_fade - self.fade_left
                chunk = chunk * self.fade_curve[k0:k0 + n, None]
                self.fade_left -= n
            out[i:i + n] = chunk
            self.pos += n
            i += n
            if self.fade_left == 0:
                self.buf, self.fade_left = None, None
            elif self.pos >= self.buf.shape[0]:
                if self.loop:
                    self.pos = 0
                else:
                    self.buf, self.fade_left = None, None
        return out


class StreamPlayer:
    """Non-blocking playback on a stream that stays open until close().

    `latency` is the stream's output latency (s), so the UI can line panel
    lights and reaction times up with what the listener actually hears.
    """

    def __init__(self, device=None, fs: int = FS):
        self.fs = fs
        self.mixer = Mixer(fs=fs)
        self.lock = threading.Lock()
        self.stream = sd().OutputStream(samplerate=fs, channels=2, dtype="float32", device=device,
                                        latency="low", callback=self._callback)
        self.stream.start()
        self.latency = float(self.stream.latency)

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            outdata[:] = self.mixer.render(frames)

    def start(self, buf: np.ndarray, loop: bool = False) -> None:
        with self.lock:
            self.mixer.start(_guard(buf), loop)

    def stop(self) -> None:
        with self.lock:
            self.mixer.stop()

    @property
    def playing(self) -> bool:
        with self.lock:
            return self.mixer.playing

    def wait(self) -> None:
        while self.playing:
            time.sleep(0.01)
        time.sleep(self.latency)

    def close(self) -> None:
        self.stop()
        self.wait()
        self.stream.stop()
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class NullPlayer:
    """Plays nothing (tests and --dry-run). Records what it was given."""

    latency = 0.0
    playing = False

    def __init__(self):
        self.played: list[np.ndarray] = []

    def start(self, buf: np.ndarray, loop: bool = False) -> None:
        self.played.append(buf)

    def stop(self) -> None:
        pass

    def wait(self) -> None:
        pass

    def close(self) -> None:
        pass
