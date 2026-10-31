"""Narrowband stimulus synthesis (DESIGN.md, "Stimuli").

Every stimulus is returned at unit RMS (0 dBFS RMS); levels are applied later
in dB relative to that.
"""

from dataclasses import dataclass, field

import numpy as np

from flatiron import FS

KINDS = ("noise", "warble", "tone")


@dataclass(frozen=True)
class StimulusSpec:
    kind: str = "noise"
    bandwidth_oct: float = 1 / 12  # noise only
    duration: float = 0.4
    ramp: float = 0.02
    warble_depth_oct: float = 1 / 24
    warble_rate: float = 8.0
    fresh_noise: bool = False  # True: new token per presentation

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown stimulus kind {self.kind!r}")
        if not 0.2 <= self.duration <= 0.8:
            raise ValueError("burst length must be 200 to 800 ms")


def raised_cosine_gate(n: int, ramp_samples: int) -> np.ndarray:
    gate = np.ones(n)
    if ramp_samples > 0:
        r = 0.5 * (1.0 - np.cos(np.pi * np.arange(ramp_samples) / ramp_samples))
        gate[:ramp_samples] = r
        gate[n - ramp_samples:] = r[::-1]
    return gate


def band_edges(fc: float, bw_oct: float) -> tuple[float, float]:
    return fc * 2.0 ** (-bw_oct / 2), fc * 2.0 ** (bw_oct / 2)


def noise_band(fc: float, bw_oct: float, n: int, fs: int, rng: np.random.Generator) -> np.ndarray:
    """Gaussian noise shaped in the frequency domain for steep band edges."""
    lo, hi = band_edges(fc, bw_oct)
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    band = (freqs >= lo) & (freqs <= hi)
    k = int(band.sum())
    if k == 0:
        raise ValueError(f"band {lo:.1f}-{hi:.1f} Hz is narrower than one FFT bin")
    spec = np.zeros(freqs.size, dtype=complex)
    spec[band] = rng.standard_normal(k) + 1j * rng.standard_normal(k)
    return np.fft.irfft(spec, n)


def warble(fc: float, depth_oct: float, rate: float, n: int, fs: int, phase0: float = 0.0) -> np.ndarray:
    t = np.arange(n) / fs
    inst = fc * 2.0 ** (depth_oct * np.sin(2 * np.pi * rate * t))
    return np.sin(phase0 + 2 * np.pi * np.cumsum(inst) / fs)


def tone(fc: float, n: int, fs: int, phase0: float = 0.0) -> np.ndarray:
    return np.sin(phase0 + 2 * np.pi * fc * np.arange(n) / fs)


def token_rng(fc: float, seed: int) -> np.random.Generator:
    """Frozen noise token per (frequency, seed)."""
    return np.random.default_rng([seed, int(round(fc * 1000))])


def make_stimulus(fc: float, spec: StimulusSpec = StimulusSpec(), seed: int = 0,
                  fs: int = FS, rng: np.random.Generator | None = None) -> np.ndarray:
    """One gated burst at unit RMS.

    `rng` is only used when `spec.fresh_noise` is set; otherwise the noise token
    is frozen by (fc, seed).
    """
    n = int(round(spec.duration * fs))
    if spec.kind == "noise":
        r = rng if (spec.fresh_noise and rng is not None) else token_rng(fc, seed)
        x = noise_band(fc, spec.bandwidth_oct, n, fs, r)
    elif spec.kind == "warble":
        x = warble(fc, spec.warble_depth_oct, spec.warble_rate, n, fs)
    else:
        x = tone(fc, n, fs)
    x = x * raised_cosine_gate(n, int(round(spec.ramp * fs)))
    return x / np.sqrt(np.mean(x**2))
