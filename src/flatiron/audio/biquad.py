"""RBJ-cookbook peaking biquads: the one filter definition used everywhere.

The preview EQ, the filter fit and every exporter use these coefficients
(DESIGN.md, key decision 8).
"""

from dataclasses import dataclass

import numpy as np
from scipy import signal

from flatiron import FS


@dataclass(frozen=True)
class PeakingFilter:
    fc: float
    gain_db: float
    q: float


def bw_oct_to_q(bw_oct: float) -> float:
    """Q of a peaking filter with the given bandwidth in octaves."""
    n = 2.0**bw_oct
    return float(np.sqrt(n) / (n - 1.0))


def peaking_coeffs(fc: float, gain_db: float, q: float, fs: int = FS) -> np.ndarray:
    """Return one SOS row [b0, b1, b2, 1, a1, a2] normalised by a0."""
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * fc / fs
    alpha = np.sin(w0) / (2.0 * q)
    cw = np.cos(w0)
    b = np.array([1.0 + alpha * a, -2.0 * cw, 1.0 - alpha * a])
    den = np.array([1.0 + alpha / a, -2.0 * cw, 1.0 - alpha / a])
    return np.concatenate([b / den[0], den / den[0]])


def peaking_sos(filters: list[PeakingFilter], fs: int = FS) -> np.ndarray:
    if not filters:
        return np.array([[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]])
    return np.vstack([peaking_coeffs(f.fc, f.gain_db, f.q, fs) for f in filters])


def response_db(filters: list[PeakingFilter], freqs, fs: int = FS) -> np.ndarray:
    """Magnitude response of a filter cascade in dB at the given frequencies."""
    freqs = np.atleast_1d(np.asarray(freqs, dtype=float))
    _, h = signal.sosfreqz(peaking_sos(filters, fs), worN=freqs, fs=fs)
    return 20.0 * np.log10(np.abs(h))


def apply(filters: list[PeakingFilter], x: np.ndarray, fs: int = FS) -> np.ndarray:
    """Filter a (n,) or (n, channels) signal along axis 0."""
    return signal.sosfilt(peaking_sos(filters, fs), x, axis=0)


def shelf_coeffs(kind: str, fc: float, gain_db: float, q: float = 0.7071, fs: int = FS) -> np.ndarray:
    """RBJ low/high shelf ('LS' or 'HS') as one SOS row, normalised by a0."""
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * fc / fs
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2.0 * q)
    k = 2.0 * np.sqrt(a) * alpha
    if kind == "LS":
        b = [a * ((a + 1) - (a - 1) * cw + k), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - k)]
        den = [(a + 1) + (a - 1) * cw + k, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - k]
    elif kind == "HS":
        b = [a * ((a + 1) + (a - 1) * cw + k), -2 * a * ((a - 1) + (a + 1) * cw), a * ((a + 1) + (a - 1) * cw - k)]
        den = [(a + 1) - (a - 1) * cw + k, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - k]
    else:
        raise ValueError(f"unknown shelf kind {kind!r}")
    b, den = np.array(b), np.array(den)
    return np.concatenate([b / den[0], den / den[0]])


def sos_response_db(sos: np.ndarray, freqs, fs: int = FS) -> np.ndarray:
    freqs = np.atleast_1d(np.asarray(freqs, dtype=float))
    _, h = signal.sosfreqz(sos, worN=freqs, fs=fs)
    return 20.0 * np.log10(np.abs(h))
