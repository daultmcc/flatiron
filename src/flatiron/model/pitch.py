"""The listener's pitch bias (DESIGN.md, "Pitch bias"): in a loudness trial,
the higher of the two bands is heard b(f̄) · min(|Δ| / SATURATION_OCT, 1) dB
louder than it is, where Δ is the pair's separation in octaves and f̄ its
centre (geometric mean). b is piecewise linear in log f with values at
KNOTS_HZ, flat beyond the end knots. It's the listener's: fitted once across
their profiles (fit_pitch_bias in model/hierarchical.py) and carried with the
noise (Noise.pitch) into every fit.
"""

import numpy as np

KNOTS_HZ = (2000.0, 5000.0, 12000.0)
SATURATION_OCT = 0.25  # the bias grows with the separation up to this, then stays
PRIOR_SD_DB = 3.0  # each knot's, centred on no bias
ZERO = (0.0,) * len(KNOTS_HZ)


def hats(freqs) -> np.ndarray:
    """Piecewise-linear hats in log f at KNOTS_HZ, flat beyond the ends."""
    x = np.log2(np.atleast_1d(np.asarray(freqs, float)))
    k = np.log2(KNOTS_HZ)
    return np.stack([np.interp(x, k, np.eye(k.size)[i]) for i in range(k.size)], axis=1)


def design(f_a, f_b) -> np.ndarray:
    """trials x knots: d(offset of A over B)/d(bias at each knot)."""
    f_a, f_b = np.asarray(f_a, float), np.asarray(f_b, float)
    d = np.log2(f_a / f_b)
    shape = np.sign(d) * np.minimum(np.abs(d) / SATURATION_OCT, 1.0)
    return shape[:, None] * hats(np.sqrt(f_a * f_b))


def offsets(f_a, f_b, pitch) -> np.ndarray:
    """How much louder A is heard than B because of the bias (dB), per trial."""
    if pitch is None or not np.any(pitch):
        return np.zeros(np.size(f_a))
    return design(f_a, f_b) @ np.asarray(pitch, float)


def curve(freqs, pitch) -> np.ndarray:
    """b at the given frequencies (dB)."""
    return hats(freqs) @ np.asarray(pitch, float)
