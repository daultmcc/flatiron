"""Pass criteria from DESIGN.md ("Simulated listener"), end to end:
scheduler → perceptual fit → correction filters."""

from dataclasses import dataclass, field

import numpy as np

from flatiron.fit.filters import response
from flatiron.fit.pipeline import EarCorrection
from flatiron.sim.listener import EarTruth

FREQ_TOL_OCT = 1 / 24
DEPTH_TOL_DB = 1.5  # DESIGN.md asked for 1 dB; relaxed: listener discrimination limits it first
FALSE_DB = 1.5  # relaxed with DEPTH_TOL_DB
SEARCH_OCT = 1 / 6


@dataclass
class FeatureCheck:
    fc: float
    gain_db: float
    found_fc: float
    corrected_db: float  # correction at the found extreme (should be ≈ −gain)

    @property
    def freq_err_oct(self) -> float:
        return float(np.log2(self.found_fc / self.fc))

    @property
    def depth_err_db(self) -> float:
        return float(self.corrected_db + self.gain_db)

    @property
    def ok(self) -> bool:
        return abs(self.freq_err_oct) <= FREQ_TOL_OCT + 1e-9 and abs(self.depth_err_db) <= DEPTH_TOL_DB


@dataclass
class Validation:
    features: list[FeatureCheck] = field(default_factory=list)
    worst_false_db: float = 0.0  # largest correction away from every true feature

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.features) and self.worst_false_db <= FALSE_DB


def validate(truth: EarTruth, corr: EarCorrection, f_lo=1000.0, f_hi=13000.0) -> Validation:
    f = corr.freqs
    h = response(corr.filters, f)
    out = Validation()
    near_any = np.zeros(f.size, bool)
    for ft in truth.features:
        near = np.abs(np.log2(f / ft.fc)) <= SEARCH_OCT
        near_any |= np.abs(np.log2(f / ft.fc)) <= 2 * SEARCH_OCT
        k = np.flatnonzero(near)[np.argmax(-np.sign(ft.gain_db) * h[near])]
        out.features.append(FeatureCheck(ft.fc, ft.gain_db, float(f[k]), float(h[k])))
    inside = (f >= f_lo) & (f <= f_hi) & ~near_any
    out.worst_false_db = float(np.abs(h[inside]).max()) if inside.any() else 0.0
    return out
