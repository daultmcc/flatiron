"""Reference contours E(f): the perceived-level shape a reference-correct
headphone would produce (DESIGN.md, pipeline step 1).

    diffuse    (default) iso226 plus ISO 389-7's diffuse-minus-free-field
               threshold difference
    iso226     negated ISO 226 (frontal free field) equal-loudness contour
    none       flat
    <path>     a user CSV of perceived-level shape: "freq_hz,db" rows

A consensus reference ("consensus:A,B") is built from other profiles' fits, so
it's made per profile in fit/run.py and doesn't come through here.

Every reference is returned with its mean removed over the frequencies asked for.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from flatiron import iso226, iso389_7

BUILTIN = ("iso226", "diffuse", "none")
CONSENSUS_PREFIX = "consensus:"


def read_curve_csv(path) -> tuple[np.ndarray, np.ndarray]:
    rows = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p for p in line.replace(";", ",").replace("\t", ",").split(",") if p.strip()]
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except (ValueError, IndexError):
            if rows:  # a header is fine, anything later isn't
                raise ValueError(f"{path}:{n}: expected 'freq_hz,db', got {line!r}")
    if len(rows) < 2:
        raise ValueError(f"{path}: need at least two 'freq_hz,db' rows")
    f, db = np.array(sorted(rows)).T
    return f, db


def interp_log(f_src, db_src, freqs) -> np.ndarray:
    """Linear in log2 f; held constant beyond the ends."""
    return np.interp(np.log2(freqs), np.log2(f_src), db_src)


ANCHORED = ("diffuse", "iso226")  # the references that can be anchored at threshold


@dataclass(frozen=True)
class Anchor:
    """A profile's loudness-threshold gap (fit/gap.py): g + δ, with g's
    covariance. With it the built-in references are anchored at threshold
    (DESIGN.md, "Threshold-anchored reference"): diffuse is g plus the
    diffuse-field threshold (ISO 226's, plus ISO 389-7's diffuse-field
    difference), iso226 g plus ISO 226's threshold, each as a sensitivity
    (negated)."""
    knots_hz: tuple
    g: tuple
    cov: tuple
    delta: tuple = (0.0, 0.0)

    @classmethod
    def of(cls, gap, slug: str) -> "Anchor":
        d = gap.profiles.get(slug, {}).get("delta", [0.0, 0.0])
        return cls(tuple(gap.knots_hz), tuple(gap.g), tuple(tuple(r) for r in gap.cov), tuple(d))

    def curve(self, freqs) -> np.ndarray:
        from flatiron.fit.gap import hats, low_hats

        return hats(freqs, self.knots_hz) @ np.asarray(self.g) + low_hats(freqs) @ np.asarray(self.delta)

    def cov_on(self, freqs) -> np.ndarray:
        """Cov of g on the grid (δ's own error is left out, as small next to it)."""
        from flatiron.fit.gap import hats

        B = hats(freqs, self.knots_hz)
        return B @ np.asarray(self.cov) @ B.T


def threshold_sensitivity(name: str, freqs) -> np.ndarray:
    """diffuse or iso226 at threshold, as a sensitivity (negated), mean not removed:
    the reference an anchored one is drawn as, once the gap is taken out."""
    f = np.asarray(freqs, float)
    sens = -iso226.threshold(f)
    if name == "diffuse":
        sens = sens + iso389_7.diffuse_minus_free(f)
    return sens


def anchored(name: str, freqs, anchor: Anchor) -> np.ndarray:
    f = np.asarray(freqs, float)
    e = anchor.curve(f) + threshold_sensitivity(name, f)
    return e - e.mean()


def reference(name: str, freqs, phon: float = 60.0, anchor: Anchor | None = None) -> np.ndarray:
    """anchor: with diffuse or iso226, anchor it at threshold (see Anchor)."""
    f = np.asarray(freqs, float)
    if anchor is not None and name in ANCHORED:
        return anchored(name, f, anchor)
    if name.startswith(CONSENSUS_PREFIX):
        raise ValueError("a consensus reference is only available in a profile's own fit, not here")
    if name == "none":
        e = np.zeros(f.size)
    elif name == "iso226":
        e = iso226.perceived_level(f, phon)
    elif name == "diffuse":
        # Where a diffuse field is heard better than a frontal one of the same
        # level, a diffuse-field-correct headphone sounds louder.
        e = iso226.perceived_level(f, phon) + iso389_7.diffuse_minus_free(f)
    else:
        e = interp_log(*read_curve_csv(name), f)
    return e - e.mean()
