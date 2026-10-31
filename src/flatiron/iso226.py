"""ISO 226:2003 equal-loudness contours.

The standard tabulates 20 Hz to 12.5 kHz. Above 12.5 kHz the contour is
extrapolated linearly in log-frequency from the last two points; treat values
there as a rough guess.
"""

import numpy as np

F = np.array([20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630,
              800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500.0])
AF = np.array([0.532, 0.506, 0.480, 0.455, 0.432, 0.409, 0.387, 0.367, 0.349, 0.330, 0.315,
               0.301, 0.288, 0.276, 0.267, 0.259, 0.253, 0.250, 0.246, 0.244, 0.243, 0.243,
               0.243, 0.242, 0.242, 0.245, 0.254, 0.271, 0.301])
LU = np.array([-31.6, -27.2, -23.0, -19.1, -15.9, -13.0, -10.3, -8.1, -6.2, -4.5, -3.1, -2.0,
               -1.1, -0.4, 0.0, 0.3, 0.5, 0.0, -2.7, -4.1, -1.0, 1.7, 2.5, 1.2, -2.1, -7.1,
               -11.2, -10.7, -3.1])
TF = np.array([78.5, 68.7, 59.5, 51.1, 44.0, 37.5, 31.5, 26.5, 22.1, 17.9, 14.4, 11.4, 8.6,
               6.2, 4.4, 3.0, 2.2, 2.4, 3.5, 1.7, -1.3, -4.2, -6.0, -5.4, -1.5, 6.0, 12.6,
               13.9, 12.3])


def contour_table(phon: float) -> np.ndarray:
    """SPL (dB) at the tabulated frequencies for the given loudness level."""
    af = 4.47e-3 * (10 ** (0.025 * phon) - 1.15) + (0.4 * 10 ** ((TF + LU) / 10 - 9)) ** AF
    return 10.0 / AF * np.log10(af) - LU + 94.0


def contour(freqs, phon: float = 60.0) -> np.ndarray:
    """Equal-loudness SPL at arbitrary frequencies (interpolated in log f)."""
    x = np.log2(np.asarray(freqs, dtype=float))
    xt, yt = np.log2(F), contour_table(phon)
    y = np.interp(x, xt, yt)
    hi = x > xt[-1]
    slope = (yt[-1] - yt[-2]) / (xt[-1] - xt[-2])
    y[hi] = yt[-1] + slope * (x[hi] - xt[-1])
    return y


def perceived_level(freqs, phon: float = 60.0) -> np.ndarray:
    """Relative perceived level (dB) of equal-SPL stimuli: the negated contour."""
    return -(contour(freqs, phon) - phon)
