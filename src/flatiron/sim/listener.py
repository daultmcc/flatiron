"""Simulated listener (DESIGN.md, "Simulated listener").

Ground truth per ear = smooth tilt + negated ISO 226 contour + features
(peaking-filter shapes, so they match what the correction can represent).

The listener hears each stimulus at its power-averaged level over the
stimulus spectrum, not at the centre frequency alone, so narrow features are
smeared the way a band of noise smears them. (Auditory-filter smearing is not
modelled.)
"""

from dataclasses import dataclass, field

import numpy as np
from scipy.special import ndtr

from flatiron import iso226
from flatiron.audio.biquad import PeakingFilter, bw_oct_to_q, response_db
from flatiron.audio.stimulus import StimulusSpec, band_edges
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
            # The doc's "−5 dB peak": a +5 dB headphone peak, corrected by −5 dB.
            # (Read as a −5 dB notch it would need a +5 dB boost, past the +4 dB cap.)
            PeakingFilter(6200.0 * s, 5.0, 6.0),
            PeakingFilter(8400.0 * s, -3.0, 10.0),
            PeakingFilter(11000.0, 3.0, 4.0),
        ],
    )


def narrow_example(ear: str = "L") -> EarTruth:
    """Narrow resonances at the hard end of the design goal (Q 12 to 16)."""
    s = 1.0 if ear == "L" else 2 ** (1 / 24)
    return EarTruth(
        tilt_db_per_oct=-1.0,
        features=[
            PeakingFilter(5200.0 * s, -4.0, 12.0),
            PeakingFilter(7300.0 * s, -4.0, 16.0),
            PeakingFilter(9800.0 * s, 3.0, 14.0),
        ],
    )


SPECTRUM_POINTS = 41


def heard_offsets_oct(spec: StimulusSpec | None) -> np.ndarray:
    """Log2 offsets from the centre frequency sampling the stimulus spectrum.

    Noise is flat in Hz between its band edges; a warble spends its time
    uniformly in FM phase; a tone (or None) is a single point.
    """
    if spec is None or spec.kind == "tone":
        return np.zeros(1)
    if spec.kind == "noise":
        lo, hi = band_edges(1.0, spec.bandwidth_oct)
        return np.log2(np.linspace(lo, hi, SPECTRUM_POINTS))
    theta = (np.arange(SPECTRUM_POINTS) + 0.5) * 2 * np.pi / SPECTRUM_POINTS
    return spec.warble_depth_oct * np.sin(theta)


@dataclass
class SimulatedListener:
    ears: dict[str, EarTruth]
    sigma0: float = 1.0
    d0: float = 0.5
    lapse: float = 0.03
    seed: int = 0
    # Stimulus the listener hears; None = an ideal point probe at the centre.
    stimulus: StimulusSpec | None = field(default_factory=StimulusSpec)

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)
        self._offsets = heard_offsets_oct(self.stimulus)

    def heard_level(self, ear, freqs) -> np.ndarray:
        f = np.atleast_1d(np.asarray(freqs, float))
        lv = self.ears[ear].level(f[:, None] * 2.0 ** self._offsets[None, :])
        return 10 * np.log10(np.mean(10 ** (lv / 10), axis=1))

    def p_a_louder(self, ear, f_a, f_b, offset):
        d = octaves(f_a, f_b)
        sigma = self.sigma0 * np.sqrt(1 + (d / self.d0) ** 2)
        z = (self.heard_level(ear, f_a) - self.heard_level(ear, f_b) + np.asarray(offset)) / sigma
        return self.lapse / 2 + (1 - self.lapse) * ndtr(z)

    def respond(self, ear, f_a, f_b, offset) -> np.ndarray:
        p = self.p_a_louder(ear, f_a, f_b, offset)
        return self.rng.random(np.shape(p)) < p
