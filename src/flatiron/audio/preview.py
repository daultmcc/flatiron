"""Flatiron's own copy of a correction, applied in software (DESIGN.md,
"Fit and audition" and "Verify"). Built from the same export plan as the
exporters, so what you audition or verify is what an export would do."""

from dataclasses import dataclass

import numpy as np
from scipy import signal

from flatiron import FS
from flatiron.apo import ApoEq, ApoFilter
from flatiron.export.plan import ExportPlan, build_plan

EAR_INDEX = {"L": 0, "R": 1}


@dataclass
class PreviewEQ:
    channels: dict[str, list[ApoFilter]]
    preamp_db: float
    fit_hash: str = ""

    @classmethod
    def from_plan(cls, plan: ExportPlan) -> "PreviewEQ":
        return cls(plan.channels, plan.preamp_db, plan.fit_hash)

    @classmethod
    def from_fit(cls, fit: dict) -> "PreviewEQ":
        return cls.from_plan(build_plan(fit))

    def sos(self, ear: str, fs: int = FS) -> np.ndarray:
        return ApoEq(0.0, self.channels[ear]).sos(fs)

    def filter(self, x: np.ndarray, ear: str, fs: int = FS) -> np.ndarray:
        """Filter a mono signal with one ear's filters (no preamp)."""
        return signal.sosfilt(self.sos(ear, fs), x)

    def apply(self, stereo: np.ndarray, fs: int = FS, preamp: bool = True) -> np.ndarray:
        """Filter an (n, 2) buffer per channel, with the preamp."""
        out = np.empty_like(stereo, dtype=np.float32)
        g = 10 ** (self.preamp_db / 20) if preamp else 1.0
        for ear, c in EAR_INDEX.items():
            out[:, c] = self.filter(stereo[:, c].astype(float), ear, fs) * g
        return out

    def burst_gain_db(self, burst: np.ndarray, ear: str, fs: int = FS) -> tuple[np.ndarray, float]:
        """Filter one stimulus burst (with a tail for ringing) and return it
        with its level change in dB: the known offset a verify trial records."""
        padded = np.concatenate([burst, np.zeros(int(0.02 * fs))])
        y = self.filter(padded, ear, fs)
        gain = 10 * np.log10(np.sum(y**2) / np.sum(burst**2))  # energy: the tail is the burst's too
        return y, float(gain)

    def to_json(self) -> dict:
        return {"fit_hash": self.fit_hash, "preamp_db": self.preamp_db,
                "channels": {e: [[f.kind, f.fc, f.gain_db, f.q] for f in fs] for e, fs in self.channels.items()}}

    @classmethod
    def from_json(cls, d: dict) -> "PreviewEQ":
        return cls({e: [ApoFilter(*f) for f in fs] for e, fs in d["channels"].items()},
                   d["preamp_db"], d.get("fit_hash", ""))
