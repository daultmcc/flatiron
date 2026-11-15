"""Comparing profiles (DESIGN.md, "Comparing profiles"): their raw curves and their
narrow parts against each other, and for every feature what the veto would do
with it. No consensus is formed: what a consensus reference keeps depends on a
profile's settings, so `report` shows it instead.

The curves show broad differences as they are, and only features can be flagged,
so a broad difference between headphones can't produce a flag. A flag means the
veto would correct that feature on that headphone.
"""

from dataclasses import dataclass, replace

import numpy as np

from flatiron.fit.consensus import KINDS_VOTING, Source, estimate_tau_hp, heterogeneity, split_parts
from flatiron.fit.features import weighted_median
from flatiron.fit.pipeline import CorrectionSettings, correct_ear, ear_bases, voter_bases
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
    grid: np.ndarray  # the common grid
    tau_hp: float  # between-headphone spread of the aligned curves
    offsets: dict[str, float]  # each profile's level, for drawing the curves together
    narrow: dict[str, tuple[np.ndarray, np.ndarray]]  # each profile's narrow part and its SD, on the common grid
    voting: dict[str, bool]
    differ_broad: np.ndarray  # on the common grid: Cochran's Q on the aligned broad parts
    differ_narrow: np.ndarray  # and on the narrow parts
    groups: list[list[Seen]]  # overlapping same-sign features, one list per row, by frequency

    @property
    def voters(self) -> int:
        return sum(self.voting.values())

    @property
    def flags(self) -> list[Seen]:
        return sorted((s for g in self.groups for s in g if s.flagged), key=lambda s: -abs(s.correction_db))


ALIGN_ITERS = 50


def _weighted_median_offset(L: np.ndarray, ref: np.ndarray, grid: np.ndarray, sd: np.ndarray) -> float:
    return weighted_median(L - ref, np.gradient(np.log2(grid)) / np.maximum(sd, 1e-6) ** 2)


def align(voters: list[Source], others: list[Source], common: np.ndarray) -> dict[str, float]:
    """Constant offsets that draw the curves together: each voter's weighted
    median difference from the voters' pointwise median is zero (iterated,
    offsets summing to zero), so one headphone's treble difference doesn't
    shift its whole curve. Non-voters are aligned against that median without
    moving it. Only for drawing; nothing else uses the median."""
    up = {s.label: interp_matrix(s.grid, common) for s in voters + others}
    Y = {s.label: up[s.label] @ s.R for s in voters}
    sd = {s.label: up[s.label] @ s.sd for s in voters + others}
    off = {s.label: 0.0 for s in voters}
    for _ in range(ALIGN_ITERS):
        med = np.median([Y[k] - off[k] for k in off], axis=0)
        new = {k: _weighted_median_offset(Y[k], med, common, sd[k]) for k in off}
        mean = np.mean(list(new.values()))
        new = {k: v - mean for k, v in new.items()}
        done = max(abs(new[k] - off[k]) for k in off) < 1e-3
        off = new
        if done:
            break
    med = np.median([Y[k] - off[k] for k in off], axis=0)
    for s in others:
        off[s.label] = _weighted_median_offset(up[s.label] @ s.R, med, common, sd[s.label])
    return off


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
    """Every profile's curve aligned with the others, its narrow part, and
    every feature it would correct judged by the veto. One that doesn't vote
    is aligned against the voters without counting toward anything."""
    voters = [m for m in members if m.kind in voting_kinds]
    if len(voters) < 2:
        raise ValueError(f"comparing needs at least two profiles that vote; "
                         f"{len(voters)} of these do (see --include-kind)")
    src = {m.label: Source(m.label, m.fits[ear].grid, m.fits[ear].L, m.fits[ear].cov, m.kind) for m in members}
    sources = [src[m.label] for m in voters]
    common = common_grid([s.grid for s in sources])
    offsets = align(sources, [src[m.label] for m in members if m.kind not in voting_kinds], common)
    narrow, parts = {}, {}
    for m in members:
        _, b, n = split_parts(src[m.label])
        parts[m.label] = (b, n)
        A = interp_matrix(n.grid, common)
        narrow[m.label] = (A @ n.R, np.sqrt(np.clip(np.einsum("ij,jk,ik->i", A, n.cov, A), 0, None)))
    seen = []
    for m in members:
        voting = m.kind in voting_kinds
        others = [(o.label, o.fits[ear]) for o in voters if o is not m]
        st = replace(settings, veto=tuple(label for label, _ in others), veto_mode="feature")
        # Shared baselines need both ears; they're cached, so the other ear's pass reuses them.
        base = (ear_bases(m.fits, st) or {}).get(ear)
        vb = voter_bases({e: [(o.label, o.fits[e]) for o in voters if o is not m and e in o.fits] for e in m.fits}, st)
        c = correct_ear(ear, m.fits[ear], st, voters=others, own_votes=voting, base=base, voter_base=vb.get(ear))
        for f in c.features:
            if f.veto is not None:
                seen.append(Seen(m.label, voting, f.fc, f.width_oct, f.peak_db,
                                 f.veto.correction_db if f.accepted else 0.0, f.veto))
    aligned = [replace(s, R=s.R - offsets[s.label]) for s in sources]
    _, p_broad = heterogeneity([replace(parts[s.label][0], R=parts[s.label][0].R - offsets[s.label]) for s in sources],
                               common)
    _, p_narrow = heterogeneity([parts[s.label][1] for s in sources], common)
    return EarComparison(ear, common, estimate_tau_hp(aligned, common), offsets, narrow,
                         {m.label: m.kind in voting_kinds for m in members}, p_broad < HETEROGENEITY_P,
                         p_narrow < HETEROGENEITY_P, _grouped(seen))
