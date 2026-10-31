"""Equalizer APO / AutoEQ text: parsing (the active-EQ context and preference
layer). Writing lives with the exporters.

Supported: `Preamp:` and `Filter N: ON <type> Fc <f> Hz Gain <g> dB [Q <q>]`
with types PK, LSC / LS and HSC / HS. Disabled (OFF) filters are skipped;
anything else is an error rather than a silent guess.
"""

import re
from dataclasses import dataclass, field

import numpy as np

from flatiron import FS
from flatiron.audio.biquad import peaking_coeffs, shelf_coeffs, sos_response_db

FILTER_RE = re.compile(
    r"^Filter\s*\d*\s*:\s*(?P<state>ON|OFF)\s+(?P<type>[A-Z]+)"
    r"(?:\s+Fc\s+(?P<fc>[\d.]+)\s*Hz)?(?:\s+Gain\s+(?P<gain>[-+\d.]+)\s*dB)?(?:\s+Q\s+(?P<q>[\d.]+))?",
    re.IGNORECASE)
PREAMP_RE = re.compile(r"^Preamp\s*:\s*(?P<db>[-+\d.]+)\s*dB", re.IGNORECASE)
SHELF_DEFAULT_Q = 0.7071
TYPES = {"PK": "PK", "PEQ": "PK", "LSC": "LS", "LS": "LS", "HSC": "HS", "HS": "HS"}


@dataclass(frozen=True)
class ApoFilter:
    kind: str  # PK, LS, HS
    fc: float
    gain_db: float
    q: float


@dataclass
class ApoEq:
    preamp_db: float = 0.0
    filters: list[ApoFilter] = field(default_factory=list)

    def sos(self, fs: int = FS) -> np.ndarray:
        rows = [peaking_coeffs(f.fc, f.gain_db, f.q, fs) if f.kind == "PK"
                else shelf_coeffs(f.kind, f.fc, f.gain_db, f.q, fs) for f in self.filters]
        return np.vstack(rows) if rows else np.array([[1.0, 0, 0, 1.0, 0, 0]])

    def gain_db(self, freqs, fs: int = FS, include_preamp: bool = False) -> np.ndarray:
        g = sos_response_db(self.sos(fs), freqs, fs)
        return g + self.preamp_db if include_preamp else g


def parse_apo(text: str) -> ApoEq:
    eq = ApoEq()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if m := PREAMP_RE.match(line):
            eq.preamp_db += float(m["db"])
            continue
        m = FILTER_RE.match(line)
        if not m:
            raise ValueError(f"line {n}: can't parse {raw.strip()!r}")
        if m["state"].upper() == "OFF":
            continue
        kind = TYPES.get(m["type"].upper())
        if kind is None or m["fc"] is None or m["gain"] is None:
            raise ValueError(f"line {n}: unsupported filter {raw.strip()!r} (PK, LSC, HSC only)")
        if kind == "PK" and m["q"] is None:
            raise ValueError(f"line {n}: PK filter without Q")
        q = float(m["q"]) if m["q"] else SHELF_DEFAULT_Q
        eq.filters.append(ApoFilter(kind, float(m["fc"]), float(m["gain"]), q))
    return eq
