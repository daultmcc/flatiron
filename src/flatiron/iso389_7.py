"""ISO 389-7:2019, Table 1 (125 Hz to 18 kHz): reference hearing thresholds
for frontal free-field (T_f) and diffuse-field (T'_f) listening, and their
difference ΔL = T_f − T'_f, the amount by which a diffuse field is heard better
than a frontal one of the same level.

From 125 Hz to 12.5 kHz T_f is ISO 226:2003's threshold, at the frequencies
the two share. The standard gives T_f to 18 kHz but T'_f and ΔL only to
16 kHz (the 16 kHz ΔL is from one laboratory). Here T'_f at 18 kHz is T_f
less ΔL at 16 kHz; below 125 Hz both thresholds are ISO 226's (ΔL is 0 there);
above 18 kHz they're held constant.

ΔL agrees with ISO 11904-1's diffuse-minus-free-field response at the ear-canal
entrance to about 1 dB RMS over 850 Hz to 15 kHz, and contradicts that
standard's eardrum column (5 to 6.5 dB higher from 8 to 12.5 kHz). See DESIGN.md,
pipeline step 1.
"""

import numpy as np

F = np.array([125, 160, 200, 250, 315, 400, 500, 630, 750, 800, 1000, 1250, 1500, 1600, 2000, 2500,
              3000, 3150, 4000, 5000, 6000, 6300, 8000, 9000, 10000, 11200, 12500, 14000, 16000.0])
T_FREE = np.array([22.1, 17.9, 14.4, 11.4, 8.6, 6.2, 4.4, 3.0, 2.4, 2.2, 2.4, 3.5, 2.4, 1.7, -1.3, -4.2,
                   -5.8, -6.0, -5.4, -1.5, 4.3, 6.0, 12.6, 13.9, 13.9, 13.0, 12.3, 18.4, 40.2])
T_DIFFUSE = np.array([22.1, 17.9, 14.4, 11.4, 8.4, 5.8, 3.8, 2.1, 1.2, 1.0, 0.8, 1.9, 1.0, 0.5, -1.5, -3.1,
                      -4.0, -4.0, -3.8, -1.8, 1.4, 2.5, 6.8, 8.4, 9.8, 11.5, 14.4, 23.2, 43.7])
DELTA_L = np.array([0.0, 0.0, 0.0, 0.0, 0.2, 0.4, 0.6, 0.9, 1.2, 1.2, 1.6, 1.6, 1.4, 1.2, 0.2, -1.1,
                    -1.8, -2.0, -1.6, 0.3, 2.9, 3.5, 5.8, 5.5, 4.1, 1.5, -2.1, -4.8, -3.5])
F_TOP, T_FREE_TOP = 18000.0, 70.4  # T_f only


def diffuse_minus_free(freqs) -> np.ndarray:
    """How much louder a diffuse field sounds than a frontal free field of the
    same level (dB), linear in log f, held constant beyond 125 Hz and 16 kHz."""
    return np.interp(np.log2(np.asarray(freqs, float)), np.log2(F), DELTA_L)


def _threshold(freqs, table, top) -> np.ndarray:
    from flatiron import iso226

    f = np.asarray(freqs, float)
    x = np.log2(f)
    y = np.interp(x, np.log2(np.append(F, F_TOP)), np.append(table, top))
    low = f < F[0]
    return np.where(low, iso226.threshold(f), y)


def threshold(freqs, field: str = "free") -> np.ndarray:
    """The reference threshold (dB SPL), "free" (T_f, frontal incidence) or
    "diffuse" (T'_f), linear in log f."""
    if field == "free":
        return _threshold(freqs, T_FREE, T_FREE_TOP)
    if field == "diffuse":
        return _threshold(freqs, T_DIFFUSE, T_FREE_TOP - DELTA_L[-1])
    raise ValueError(f"field must be 'free' or 'diffuse', not {field!r}")
