"""Comparing profiles (DESIGN.md, "Comparing profiles"): their raw curves and their
narrow parts against each other, and where they differ most. No reference is
used and no consensus is formed: what a consensus reference keeps depends on a
profile's settings, so `report` shows it instead.
"""

from dataclasses import dataclass, replace

import numpy as np

from flatiron.fit.consensus import KINDS_VOTING, Source, estimate_tau_hp, heterogeneity, split_parts
from flatiron.fit.features import weighted_median
from flatiron.model.grid import common_grid, interp_matrix
from flatiron.model.probit import ProbitFit

HETEROGENEITY_P = 0.01
# Broad parts are tonality: they always differ, so what's shaded is where they
# differ most, by this much from the highest to the lowest (dB). On the real
# profiles about 10 to 17% of 1 to 13 kHz spans more than this.
BROAD_SPREAD_DB = 4.0


@dataclass
class Member:
    label: str
    kind: str
    fits: dict[str, ProbitFit]


@dataclass
class EarComparison:
    ear: str
    grid: np.ndarray  # the common grid
    tau_hp: float  # between-profile spread of the aligned curves, counted profiles only
    offsets: dict[str, float]  # each profile's level, for drawing the curves together
    narrow: dict[str, tuple[np.ndarray, np.ndarray]]  # each profile's narrow part and its SD, on the common grid
    counted: dict[str, bool]  # whether it counts toward the alignment, the spread and the strips
    differ_broad: np.ndarray  # on the common grid: where the aligned broad parts span more than broad_spread_db
    differ_narrow: np.ndarray  # where the narrow parts differ beyond their error bars (Cochran's Q)
    broad_spread: np.ndarray  # highest minus lowest aligned broad part, counted profiles only
    broad_spread_db: float = BROAD_SPREAD_DB
    fallback: bool = False  # fewer than two were of a counted kind, so all of them count

    @property
    def n_counted(self) -> int:
        return sum(self.counted.values())


ALIGN_ITERS = 50


def _weighted_median_offset(L: np.ndarray, ref: np.ndarray, grid: np.ndarray, sd: np.ndarray) -> float:
    return weighted_median(L - ref, np.gradient(np.log2(grid)) / np.maximum(sd, 1e-6) ** 2)


def align(counted: list[Source], others: list[Source], common: np.ndarray) -> dict[str, float]:
    """Constant offsets that draw the curves together: each counted profile's
    weighted median difference from their pointwise median is zero (iterated,
    offsets summing to zero), so one headphone's treble difference doesn't
    shift its whole curve. The others are aligned against that median without
    moving it. Only for drawing; nothing else uses the median."""
    up = {s.label: interp_matrix(s.grid, common) for s in counted + others}
    Y = {s.label: up[s.label] @ s.R for s in counted}
    sd = {s.label: up[s.label] @ s.sd for s in counted + others}
    off = {s.label: 0.0 for s in counted}
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


def compare_ear(ear: str, members: list[Member], counted_kinds=KINDS_VOTING,
                broad_spread_db: float | None = None) -> EarComparison:
    """Every profile's curve aligned with the others, its narrow part, and where
    they differ. Profiles of kinds outside counted_kinds are aligned against the
    rest without counting toward anything, unless fewer than two would count:
    then all of them do."""
    if len(members) < 2:
        raise ValueError("comparing needs at least two profiles")
    counted = [m for m in members if m.kind in counted_kinds]
    fallback = len(counted) < 2
    if fallback:
        counted = list(members)
    names = {m.label for m in counted}
    src = {m.label: Source(m.label, m.fits[ear].grid, m.fits[ear].L, m.fits[ear].cov, m.kind) for m in members}
    sources = [src[m.label] for m in counted]
    common = common_grid([s.grid for s in sources])
    offsets = align(sources, [src[m.label] for m in members if m.label not in names], common)
    narrow, parts = {}, {}
    for m in members:
        _, b, n = split_parts(src[m.label])
        parts[m.label] = (b, n)
        A = interp_matrix(n.grid, common)
        narrow[m.label] = (A @ n.R, np.sqrt(np.clip(np.einsum("ij,jk,ik->i", A, n.cov, A), 0, None)))
    aligned = [replace(s, R=s.R - offsets[s.label]) for s in sources]
    broad_spread_db = BROAD_SPREAD_DB if broad_spread_db is None else broad_spread_db
    broad = np.array([interp_matrix(parts[s.label][0].grid, common) @ parts[s.label][0].R - offsets[s.label]
                      for s in sources])
    spread = np.ptp(broad, axis=0)
    _, p_narrow = heterogeneity([parts[s.label][1] for s in sources], common)
    return EarComparison(ear, common, estimate_tau_hp(aligned, common), offsets, narrow,
                         {m.label: m.label in names for m in members}, spread > broad_spread_db,
                         p_narrow < HETEROGENEITY_P, spread, broad_spread_db, fallback)


def spans(grid: np.ndarray, mask: np.ndarray) -> list[tuple[float, float]]:
    """The frequency ranges where mask holds, as (low, high) in Hz."""
    out, start = [], None
    for i, on in enumerate(mask):
        if on and start is None:
            start = i
        if start is not None and (not on or i == len(mask) - 1):
            out.append((float(grid[start]), float(grid[i if on else i - 1])))
            start = None
    return out
