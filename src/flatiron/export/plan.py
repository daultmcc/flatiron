"""What gets exported: per-channel filter lists plus one preamp
(DESIGN.md, "Measurement context" and "Export formats").

Correction only: Flatiron's filters, to stack after an existing EQ.
Merged: the user's preference layer (APO text, per channel) first, then the
correction sorted by frequency, with the preamp computed over both.
"""

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from flatiron import EARS
from flatiron.apo import ApoEq, ApoFilter
from flatiron.fit.filters import PREAMP_GRID

PREAMP_MARGIN_DB = 0.5


@dataclass
class ExportPlan:
    channels: dict[str, list[ApoFilter]]  # "L"/"R" -> filters, in order
    preamp_db: float
    merged: bool
    fit_hash: str

    def max_bands(self) -> int:
        return max(len(v) for v in self.channels.values())


def response_db(filters: list[ApoFilter], freqs=PREAMP_GRID) -> np.ndarray:
    if not filters:
        return np.zeros(np.size(freqs))
    return ApoEq(0.0, filters).gain_db(freqs)


def fit_hash(fit: dict) -> str:
    key = {e: fit["ears"][e]["filters"] for e in EARS}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:8]


def build_plan(fit: dict, preference: dict[str, ApoEq] | None = None) -> ExportPlan:
    """fit: the "fit" entry of profile.json. preference: per-channel APO EQ to
    merge in front of the correction (None: correction only)."""
    corr = {e: [ApoFilter("PK", f["fc"], f["gain_db"], f["q"]) for f in fit["ears"][e]["filters"]]
            for e in EARS}
    if preference is None:
        return ExportPlan(corr, float(fit["preamp_db"]), False, fit_hash(fit))
    chans = {e: list(preference[e].filters) + sorted(corr[e], key=lambda f: f.fc) for e in EARS}
    # One preamp for both channels keeps the L/R balance.
    peak = max(float(response_db(chans[e]).max()) for e in EARS)
    pre = -max(0.0, peak) - PREAMP_MARGIN_DB
    return ExportPlan(chans, pre, True, fit_hash(fit))
