"""Simulated listener (DESIGN.md, "Simulated listener").

Ground truth per ear = smooth tilt + negated ISO 226 contour + features
(peaking-filter shapes, so they match what the correction can represent).
"""

from dataclasses import dataclass, field

import numpy as np
from scipy.special import ndtr

from flatiron import iso226
from flatiron.audio.biquad import PeakingFilter, bw_oct_to_q, response_db
from flatiron.model.grid import octaves


@dataclass
class EarTruth:
    tilt_db_per_oct: float = 0.0
    phon: float | None = 60.0  # None: no equal-loudness contour
    features: list[PeakingFilter] = field(default_factory=list)
    # Extra smooth deviation (e.g. an individual ear vs ISO 226), dB at each freq.
    deviation: object = None

    def level(self, freqs) -> np.ndarray:
        f = np.atleast_1d(np.asarray(freqs, float))
        out = self.tilt_db_per_oct * np.log2(f / 1000.0)
        if self.phon is not None:
            out = out + iso226.perceived_level(f, self.phon)
        if self.features:
            out = out + response_db(self.features, f)
        if self.deviation is not None:
            out = out + self.deviation(f)
        return out

    def features_only(self, freqs) -> np.ndarray:
        return response_db(self.features, freqs) if self.features else np.zeros(np.size(freqs))


def design_example(ear: str = "L") -> EarTruth:
    """The four-feature ground truth from the design doc (R shifted slightly)."""
    s = 1.0 if ear == "L" else 2 ** (1 / 12)
    return EarTruth(
        tilt_db_per_oct=-1.0,
        features=[
            PeakingFilter(4000.0, 4.0, bw_oct_to_q(0.5)),
            PeakingFilter(6200.0 * s, -5.0, 6.0),
            PeakingFilter(8400.0 * s, -3.0, 10.0),
            PeakingFilter(11000.0, 3.0, 4.0),
        ],
    )


@dataclass
class SimulatedListener:
    ears: dict[str, EarTruth]
    sigma0: float = 1.0
    d0: float = 0.5
    lapse: float = 0.03
    seed: int = 0

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)

    def p_a_louder(self, ear, f_a, f_b, offset):
        t = self.ears[ear]
        d = octaves(f_a, f_b)
        sigma = self.sigma0 * np.sqrt(1 + (d / self.d0) ** 2)
        z = (t.level(f_a) - t.level(f_b) + np.asarray(offset)) / sigma
        return self.lapse / 2 + (1 - self.lapse) * ndtr(z)

    def respond(self, ear, f_a, f_b, offset) -> np.ndarray:
        p = self.p_a_louder(ear, f_a, f_b, offset)
        return self.rng.random(np.shape(p)) < p
