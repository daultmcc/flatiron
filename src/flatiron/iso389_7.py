"""ISO 389-7:2019, Table 1: reference hearing thresholds for frontal free-field
and diffuse-field listening, and their difference ΔL = T_f − T'_f, the amount
by which a diffuse field is heard better than a frontal one of the same level.
The table's values are 0 from 125 to 250 Hz and stop at 16 kHz (that point is
from one laboratory only); values are held constant beyond the ends.

It agrees with ISO 11904-1's diffuse-minus-free-field response at the ear-canal
entrance to about 1 dB RMS over 850 Hz to 15 kHz, and contradicts that
standard's eardrum column (5 to 6.5 dB higher from 8 to 12.5 kHz). See DESIGN.md,
pipeline step 1.
"""

import numpy as np

F = np.array([125, 160, 200, 250, 315, 400, 500, 630, 750, 800, 1000, 1250, 1500, 1600, 2000, 2500,
              3000, 3150, 4000, 5000, 6000, 6300, 8000, 9000, 10000, 11200, 12500, 14000, 16000.0])
DELTA_L = np.array([0.0, 0.0, 0.0, 0.0, 0.2, 0.4, 0.6, 0.9, 1.2, 1.2, 1.6, 1.6, 1.4, 1.2, 0.2, -1.1,
                    -1.8, -2.0, -1.6, 0.3, 2.9, 3.5, 5.8, 5.5, 4.1, 1.5, -2.1, -4.8, -3.5])


def diffuse_minus_free(freqs) -> np.ndarray:
    """How much louder a diffuse field sounds than a frontal free field of the
    same level (dB), linear in log f."""
    return np.interp(np.log2(np.asarray(freqs, float)), np.log2(F), DELTA_L)
