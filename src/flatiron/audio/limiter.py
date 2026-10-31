"""Output safety: level scaling, peak checks and the hard limiter."""

from dataclasses import dataclass

import numpy as np

PEAK_TARGET_DB = -18.0
CEILING_DB = -12.0


def db_to_amp(db):
    return 10.0 ** (np.asarray(db) / 20.0)


def peak_db(x: np.ndarray) -> float:
    p = float(np.max(np.abs(x))) if x.size else 0.0
    return -np.inf if p == 0 else 20.0 * np.log10(p)


@dataclass
class LimitResult:
    signal: np.ndarray
    gain_db: float  # 0 if untouched, negative if the buffer was scaled down
    over_target: bool  # peak exceeded PEAK_TARGET_DB before limiting


def limit(x: np.ndarray, ceiling_db: float = CEILING_DB) -> LimitResult:
    """Scale the whole buffer under the ceiling, then hard-clip as a last guard.

    Scaling the whole buffer (a full A-gap-B trial, not each burst) keeps the
    A/B level difference intact; only the absolute level moves, which the
    trial rove already randomises.
    """
    ceiling = float(db_to_amp(ceiling_db))
    pk = peak_db(x)
    gain_db = 0.0
    if pk > ceiling_db:
        gain_db = ceiling_db - pk
        x = x * db_to_amp(gain_db)
    return LimitResult(np.clip(x, -ceiling, ceiling), gain_db, pk > PEAK_TARGET_DB)


def monaural(x: np.ndarray, ear: str) -> np.ndarray:
    """Stereo float32 buffer with digital silence in the other channel."""
    out = np.zeros((x.size, 2), dtype=np.float32)
    out[:, 0 if ear == "L" else 1] = x
    return out


def render_pair(a: np.ndarray, b: np.ndarray, level_a_db: float, level_b_db: float,
                gap: float, ear: str, fs: int) -> tuple[np.ndarray, LimitResult]:
    """A, gap, B in one ear. Inputs are unit-RMS bursts; levels are dBFS RMS."""
    silence = np.zeros(int(round(gap * fs)))
    mono = np.concatenate([a * db_to_amp(level_a_db), silence, b * db_to_amp(level_b_db)])
    res = limit(mono)
    return monaural(res.signal, ear), res
