"""Comparing profiles (DESIGN.md, "Comparing profiles"): their raw curves against each
other, and for every feature what the veto would do with it.

The curves show broad differences as they are, and only features can be flagged,
so a broad difference between headphones can't produce a flag. A flag means the
veto would correct that feature on that headphone.
"""

from dataclasses import dataclass, replace

import numpy as np

from flatiron.fit.consensus import KINDS_VOTING, SoftMedian, Source, heterogeneity, raw_consensus
from flatiron.fit.features import weighted_median
from flatiron.fit.pipeline import CorrectionSettings, correct_ear
from flatiron.fit.veto import VetoResult, overlaps
from flatiron.model.grid import common_grid, interp_matrix
from flatiron.model.probit import ProbitFit

HETEROGENEITY_P = 0.01


@dataclass
class Member:
    label: str
    kind: str
    fits: dict[str, ProbitFit]


@dataclass
class Seen:
    """A profile's feature, and what the veto does with it."""
    label: str
    voting: bool
    fc: float
    width_oct: float
    height_db: float
    correction_db: float  # 0: left alone
    veto: VetoResult

    @property
    def flagged(self) -> bool:
        return self.correction_db != 0.0

    @property
    def reading(self) -> str:
        v = self.veto
        peak = self.height_db > 0
        kind = "peak" if peak else "dip"
        if v.median is None:
            return f"{kind} only here"
        if v.moved:
            return f"{kind} elsewhere on the others"
        more = abs(self.height_db) > abs(v.median.height_db)
        word = ("taller" if more else "shorter") if peak else ("deeper" if more else "shallower")
        return f"{word} here than on the others"


@dataclass
class EarComparison:
    ear: str
    consensus: SoftMedian
    offsets: dict[str, float]  # each profile's level against the consensus
    voting: dict[str, bool]
    differ: np.ndarray  # on the common grid: Cochran's Q, broad differences included
    groups: list[list[Seen]]  # overlapping same-sign features, one list per row, by frequency

    @property
    def voters(self) -> int:
        return sum(self.voting.values())

    @property
    def flags(self) -> list[Seen]:
        return sorted((s for g in self.groups for s in g if s.flagged), key=lambda s: -abs(s.correction_db))


def _level_against(m: SoftMedian, f: ProbitFit) -> float:
    """A profile's offset against a consensus it isn't in, as robustly as the fit's own."""
    r = f.L - interp_matrix(m.grid, f.grid) @ m.m
    return weighted_median(r, np.gradient(np.log2(f.grid)) / np.maximum(f.sd, 1e-6) ** 2)


def _grouped(seen: list[Seen]) -> list[list[Seen]]:
    """One group per place: features that all overlap each other. Requiring only
    an overlap with some member would chain peaks an octave apart into one."""
    groups: list[list[Seen]] = []
    for s in sorted(seen, key=lambda s: s.fc):
        hit = next((g for g in reversed(groups)
                    if s.label not in {o.label for o in g}
                    and all(np.sign(o.height_db) == np.sign(s.height_db)
                            and overlaps(o.fc, o.width_oct, s.fc, s.width_oct) for o in g)), None)
        if hit is None:
            groups.append([s])
        else:
            hit.append(s)
    return groups


def compare_ear(ear: str, members: list[Member], settings: CorrectionSettings,
                voting_kinds=KINDS_VOTING) -> EarComparison:
    """Every profile's curve aligned with the raw consensus of those that vote,
    and every feature it would correct judged by the veto. One that doesn't vote
    is measured against the voters' median without counting toward it."""
    voters = [m for m in members if m.kind in voting_kinds]
    if len(voters) < 2:
        raise ValueError(f"comparing needs at least two profiles that vote; "
                         f"{len(voters)} of these do (see --include-kind)")
    sources = [Source(m.label, m.fits[ear].grid, m.fits[ear].L, m.fits[ear].cov) for m in voters]
    sm = raw_consensus(sources)
    offsets = {m.label: float(o) for m, o in zip(voters, sm.offsets)}
    seen = []
    for m in members:
        voting = m.kind in voting_kinds
        offsets.setdefault(m.label, _level_against(sm, m.fits[ear]))
        others = [(o.label, o.fits[ear]) for o in voters if o is not m]
        st = replace(settings, veto=tuple(label for label, _ in others), veto_mode="feature")
        c = correct_ear(ear, m.fits[ear], st, voters=others, own_votes=voting)
        for f in c.features:
            if f.veto is not None:
                seen.append(Seen(m.label, voting, f.fc, f.width_oct, f.peak_db,
                                 f.veto.correction_db if f.accepted else 0.0, f.veto))
    aligned = [replace(s, R=s.R - o) for s, o in zip(sources, sm.offsets)]
    _, p = heterogeneity(aligned, common_grid([s.grid for s in aligned]))
    return EarComparison(ear, sm, offsets, {m.label: m.kind in voting_kinds for m in members},
                         p < HETEROGENEITY_P, _grouped(seen))
