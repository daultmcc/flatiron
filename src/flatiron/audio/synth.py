"""Continuous signal for the tone generator, synthesized block by block in the
audio callback so the frequency can glide without clicks.

The UI sets the public attributes (freq, level_db, ear, stimulus, eq_on,
paused) from its own thread; render() picks them up at the next block and
moves to them smoothly: frequency with a short glide, everything else with a
10 ms ramp. A stimulus change fades out, swaps and fades back in.
"""

import numpy as np
from scipy import signal

from flatiron import FS
from flatiron.audio.limiter import CEILING_DB, db_to_amp
from flatiron.audio.stimulus import StimulusSpec, band_edges

# (kind, label, noise bandwidth in octaves)
STIMULI = (("tone", "pure tone", None), ("warble", "warble", None), ("noise", "1/24-oct noise", 1 / 24),
           ("noise", "1/12-oct noise", 1 / 12), ("noise", "1/3-oct noise", 1 / 3))
F_MIN, F_MAX = 50.0, 18_000.0
GLIDE_TAU_S = 0.02
RAMP_S = 0.01
# Noise bandwidth of the 4th-order Butterworth band-pass relative to its
# nominal width: (π/4) / sin(π/4).
ENBW = (np.pi / 4) / np.sin(np.pi / 4)


def stimulus_key(index: int) -> str:
    """The stimulus as a trial session's key would name it (for marks)."""
    from flatiron.audio.stimulus import stimulus_key as key

    kind, _, bw = STIMULI[index]
    return key(StimulusSpec(kind=kind, bandwidth_oct=bw or 1 / 12))


def _ramp(cur: float, target: float, n: int, step: float) -> np.ndarray:
    """Linear ramp from cur toward target by `step` per sample, holding at it."""
    k = np.arange(1, n + 1)
    if target >= cur:
        return np.minimum(cur + step * k, target)
    return np.maximum(cur - step * k, target)


class ToneSynth:
    def __init__(self, fs: int = FS, freq: float = 1000.0, level_db: float = -30.0, ear: str = "L",
                 stimulus: int = 0, eq=None, ceiling_db: float = CEILING_DB, seed: int = 0):
        self.fs = fs
        self.freq = float(freq)
        self.level_db = float(level_db)
        self.ear = ear  # "L", "R" or "LR"
        self.stimulus = stimulus
        self.eq = eq  # PreviewEQ or None; its preamp isn't applied
        self.eq_on = eq is not None
        self.paused = False
        self.ceiling = float(db_to_amp(ceiling_db))
        self.step = 1 / (RAMP_S * fs)
        self._x = np.log2(np.clip(freq, F_MIN, F_MAX))
        self._phase = 0.0
        self._lfo = 0.0
        self._active = stimulus
        self._master = 0.0  # fades in on the first block
        self._chan = np.array([1.0 if "L" in ear else 0.0, 1.0 if "R" in ear else 0.0])
        self._mix = 1.0 if self.eq_on else 0.0
        self._amp = float(db_to_amp(level_db))
        self._rng = np.random.default_rng(seed)
        self._bp_zi = None
        self._eq_sos = {e: eq.sos(e, fs) for e in "LR"} if eq is not None else {}
        self._eq_zi = {e: np.zeros((s.shape[0], 2)) for e, s in self._eq_sos.items()}
        self._stopping = False

    # -- the source interface StreamPlayer uses --------------------------------

    @property
    def playing(self) -> bool:
        return not (self._stopping and self._master == 0)

    def stop(self) -> None:
        self._stopping = True

    def render(self, frames: int) -> np.ndarray:
        n = frames
        if self.stimulus != self._active and self._master == 0:
            self._active, self._bp_zi = self.stimulus, None
        x_target = np.log2(np.clip(self.freq, F_MIN, F_MAX))
        a = np.exp(-1 / (GLIDE_TAU_S * self.fs))
        x = x_target + (self._x - x_target) * a ** np.arange(1, n + 1)
        self._x = float(x[-1])
        f = 2.0**x

        quiet = self.paused or self._stopping or self.stimulus != self._active
        master = _ramp(self._master, 0.0 if quiet else 1.0, n, self.step)
        self._master = float(master[-1])
        sig = self._source(f, n)

        targets = np.array([1.0 if "L" in self.ear else 0.0, 1.0 if "R" in self.ear else 0.0])
        chan = np.stack([_ramp(self._chan[c], targets[c], n, self.step) for c in range(2)], axis=1)
        self._chan = chan[-1].copy()
        amp = np.linspace(self._amp, db_to_amp(self.level_db), n + 1)[1:]
        self._amp = float(amp[-1])

        out = np.empty((n, 2))
        if self.eq is not None:
            mix = _ramp(self._mix, 1.0 if self.eq_on else 0.0, n, self.step)
            self._mix = float(mix[-1])
            for c, e in enumerate("LR"):
                wet, self._eq_zi[e] = signal.sosfilt(self._eq_sos[e], sig, zi=self._eq_zi[e])
                out[:, c] = mix * wet + (1 - mix) * sig
        else:
            out[:] = sig[:, None]
        out *= chan * (master * amp)[:, None]
        return np.clip(out, -self.ceiling, self.ceiling).astype(np.float32)

    # -- signals, all at unit RMS ----------------------------------------------

    def _source(self, f: np.ndarray, n: int) -> np.ndarray:
        kind, _, bw = STIMULI[self._active]
        if kind == "noise":
            return self._noise(float(f[n // 2]), bw, n)
        if kind == "warble":
            spec = StimulusSpec(kind="warble")
            lfo = self._lfo + 2 * np.pi * spec.warble_rate * np.arange(1, n + 1) / self.fs
            self._lfo = float(lfo[-1] % (2 * np.pi))
            f = f * 2.0 ** (spec.warble_depth_oct * np.sin(lfo))
        phase = self._phase + 2 * np.pi * np.cumsum(f) / self.fs
        self._phase = float(phase[-1] % (2 * np.pi))
        return np.sqrt(2) * np.sin(phase)

    def _noise(self, fc: float, bw: float, n: int) -> np.ndarray:
        lo, hi = band_edges(fc, bw)
        hi = min(hi, 0.45 * self.fs)
        sos = signal.butter(2, [lo, hi], btype="bandpass", fs=self.fs, output="sos")
        if self._bp_zi is None:
            self._bp_zi = np.zeros((sos.shape[0], 2))
        y, self._bp_zi = signal.sosfilt(sos, self._rng.standard_normal(n), zi=self._bp_zi)
        return y * np.sqrt(self.fs / (2 * ENBW * (hi - lo)))
