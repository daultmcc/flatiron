"""The feature veto behind `--veto` (ADDENDUM.md, Feature 1b): a feature is
corrected toward the median feature at the same place across headphones.

Each profile contributes the same-sign feature of its own that overlaps this
one, in the literal sense that their half-height widths intersect; a profile
with none counts as having nothing there. Features that overlap this headphone's
almost always overlap each other, so the shape difference between them means
something.

The median feature is the most central of those: the actual feature (or
nothing) with the smallest total shape difference to the rest. Taking the median
of frequency, width and height separately can give a combination no headphone
has, and a peak between two others on either side comes back as itself, so it
reads as shared.
"""

from dataclasses import dataclass, field

import numpy as np

SAME_PLACE_OCT = 1 / 12  # about the stimulus width: closer than this isn't a move
MIN_LOBE_DB = 1.0  # a narrow lobe this tall counts for a voter even if it isn't significant
OWN_LABEL = "this profile"
_X = np.arange(np.log2(500.0), np.log2(20000.0), 1 / 96)


@dataclass
class Candidate:
    label: str
    fc: float = 0.0  # 0: this profile has nothing here
    width_oct: float = 0.0
    height_db: float = 0.0

    @property
    def present(self) -> bool:
        return self.height_db != 0.0


@dataclass
class VetoResult:
    candidates: list[Candidate] = field(default_factory=list)  # this profile's first
    median: Candidate | None = None  # None: nothing there, so all of it is corrected
    moved: bool = False  # the median feature is somewhere else
    correction_db: float = 0.0  # the largest correction this leads to


def bump(x: np.ndarray, c: Candidate) -> np.ndarray:
    """A candidate's shape on x (log2 Hz): a Gaussian of its height and half-height width."""
    if not c.present:
        return np.zeros_like(x)
    return c.height_db * np.exp(-0.5 * ((x - np.log2(c.fc)) / (c.width_oct / 2.355)) ** 2)


def overlaps(fc_a: float, width_a: float, fc_b: float, width_b: float) -> bool:
    """Whether two features' half-height widths intersect."""
    return abs(np.log2(fc_a / fc_b)) < (width_a + width_b) / 2


def candidates_for(own: Candidate, voters: dict[str, list[Candidate]]) -> list[Candidate]:
    """This feature and, per voter, the tallest same-sign feature overlapping it."""
    out = [own]
    for label, feats in voters.items():
        hits = [c for c in feats if np.sign(c.height_db) == np.sign(own.height_db)
                and overlaps(own.fc, own.width_oct, c.fc, c.width_oct)]
        out.append(max(hits, key=lambda c: abs(c.height_db)) if hits else Candidate(label))
    return out


def median_feature(cands: list[Candidate]) -> Candidate | None:
    """The most central candidate; ties go to the first (this profile's, when it
    votes). With two there's no middle, so it's their average: half the difference."""
    if len(cands) == 1:
        return cands[0] if cands[0].present else None
    if len(cands) == 2:
        a, b = cands
        if not a.present and not b.present:
            return None
        if not a.present or not b.present:
            c = a if a.present else b
            return Candidate("average", c.fc, c.width_oct, c.height_db / 2)
        return Candidate("average", float(np.sqrt(a.fc * b.fc)), (a.width_oct + b.width_oct) / 2,
                         (a.height_db + b.height_db) / 2)
    shapes = np.array([bump(_X, c) for c in cands])
    d = np.sqrt(((shapes[:, None, :] - shapes[None, :, :]) ** 2).sum(axis=2))
    best = cands[int(np.argmin(d.sum(axis=1)))]
    return best if best.present else None


def judge(own: Candidate, voters: dict[str, list[Candidate]], own_votes: bool = True) -> VetoResult:
    """own_votes False: measured against the voters' median without counting toward it."""
    cands = candidates_for(own, voters)
    med = median_feature(cands if own_votes else cands[1:])
    moved = med is not None and abs(np.log2(med.fc / own.fc)) > SAME_PLACE_OCT
    return VetoResult(cands, med, moved)


def correction(R_seg: np.ndarray, x: np.ndarray, span: slice, result: VetoResult, own_sign: float,
               scale: float, import_db: float) -> np.ndarray:
    """The correction over the whole grid x (log2 Hz) from a feature's residual
    on its span: C = −(R − median feature), with any net boost, in the feature's
    own direction, limited. That limit is import_db if the median feature is
    somewhere else, and 0 if it's the same place, so a feature can only shrink
    there. Outside the span only an import can add anything."""
    c = np.zeros_like(x)
    c[span] = -R_seg * scale
    if result.median is None:
        return c
    limit = import_db if result.moved else 0.0
    b = bump(x, result.median) * scale
    if limit > 0:
        c += b
    else:
        c[span] += b[span]
    return own_sign * np.minimum(own_sign * c, limit)


def correction_rule(R, m) -> np.ndarray:
    """The pointwise rule: move R toward zero only by what it exceeds the median
    in the same direction, and never past zero, so nothing is imported."""
    R, m = np.asarray(R, float), np.asarray(m, float)
    return -np.sign(R) * np.maximum(0.0, np.abs(R) - np.maximum(0.0, np.sign(R) * m))
