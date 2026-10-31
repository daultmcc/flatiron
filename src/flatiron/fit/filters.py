"""Fit peaking filters to a target (DESIGN.md, pipeline steps 7-9).

Greedy start: put a filter on the largest remaining weighted error, then refit
all filters jointly (bounded least squares over log Fc, gain, log Q, weighted
by 1/SD). Repeat until the error is within noise everywhere (no point off by
more than `stop_z` SDs) or the band budget is used up. Filters under
`min_gain_db` are dropped.

(DESIGN.md stops on chi² per point ≤ 1.5. Averaged over the whole range that
let a whole accepted feature go unfitted, since one feature barely moves the
mean, so the stop is pointwise; chi² is still reported.)
"""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from flatiron.audio.biquad import PeakingFilter, bw_oct_to_q, peaking_db_fast


@dataclass(frozen=True)
class FilterBounds:
    f_lo: float = 1000.0
    f_hi: float = 13000.0
    q_min: float = 0.7
    q_max: float = 16.0
    max_gain_db: float = 12.0


@dataclass
class FilterFit:
    filters: list[PeakingFilter]
    response: np.ndarray  # on the fit grid
    chi2: float  # weighted residual per point
    budget_bound: bool  # stopped by the band budget, not by reaching noise
    contributions: list[float]  # increase in chi2 if each filter were removed


def response(filters: list[PeakingFilter], freqs) -> np.ndarray:
    if not filters:
        return np.zeros(np.size(freqs))
    return peaking_db_fast([f.fc for f in filters], [f.gain_db for f in filters],
                           [f.q for f in filters], freqs).sum(axis=0)


def _pack(filters):
    return np.array([v for f in filters for v in (np.log(f.fc), f.gain_db, np.log(f.q))])


def _unpack(p):
    p = p.reshape(-1, 3)
    return [PeakingFilter(float(np.exp(a)), float(g), float(np.exp(b))) for a, g, b in p]


def _refit(filters, freqs, target, sd, bounds: FilterBounds):
    if not filters:
        return filters
    lo = np.tile([np.log(bounds.f_lo), -bounds.max_gain_db, np.log(bounds.q_min)], len(filters))
    hi = np.tile([np.log(bounds.f_hi), bounds.max_gain_db, np.log(bounds.q_max)], len(filters))
    p0 = np.clip(_pack(filters), lo + 1e-9, hi - 1e-9)

    def resid(p):
        q = p.reshape(-1, 3)
        h = peaking_db_fast(np.exp(q[:, 0]), q[:, 1], np.exp(q[:, 2]), freqs).sum(axis=0)
        return (target - h) / sd

    res = least_squares(resid, p0, bounds=(lo, hi), x_scale="jac", max_nfev=2000)
    return _unpack(res.x)


def _chi2(filters, freqs, target, sd):
    return float(np.mean(((target - response(filters, freqs)) / sd) ** 2))


def _initial_guess(freqs, err, sd, bounds: FilterBounds) -> PeakingFilter:
    """A filter on the largest weighted error, its Q from the half-height width."""
    inside = (freqs >= bounds.f_lo) & (freqs <= bounds.f_hi)
    score = np.where(inside, np.abs(err) / sd, 0)
    k = int(np.argmax(score))
    x = np.log2(freqs)
    half = abs(err[k]) / 2
    i = k
    while i > 0 and abs(err[i - 1]) > half and np.sign(err[i - 1]) == np.sign(err[k]):
        i -= 1
    j = k
    while j < err.size - 1 and abs(err[j + 1]) > half and np.sign(err[j + 1]) == np.sign(err[k]):
        j += 1
    bw = max(x[j] - x[i], 1 / 48)
    q = float(np.clip(bw_oct_to_q(bw), bounds.q_min, bounds.q_max))
    g = float(np.clip(err[k], -bounds.max_gain_db, bounds.max_gain_db))
    return PeakingFilter(float(freqs[k]), g, q)


def _max_z(filters, freqs, target, sd):
    return float(np.max(np.abs(target - response(filters, freqs)) / sd))


def fit_filters(freqs, target, sd, budget: int, bounds: FilterBounds = FilterBounds(),
                stop_z: float = 2.0, min_gain_db: float = 0.5) -> FilterFit:
    freqs, target, sd = (np.asarray(v, float) for v in (freqs, target, sd))
    filters: list[PeakingFilter] = []
    while len(filters) < budget and _max_z(filters, freqs, target, sd) > stop_z:
        err = target - response(filters, freqs)
        filters = _refit(filters + [_initial_guess(freqs, err, sd, bounds)], freqs, target, sd, bounds)
    # Drop negligible filters, then refit what's left.
    while filters:
        small = [f for f in filters if abs(f.gain_db) < min_gain_db]
        if not small:
            break
        filters = _refit([f for f in filters if f not in small], freqs, target, sd, bounds)
    c = _chi2(filters, freqs, target, sd)
    contrib = [_chi2([g for g in filters if g is not f], freqs, target, sd) - c for f in filters]
    order = np.argsort([f.fc for f in filters])
    return FilterFit([filters[i] for i in order], response(filters, freqs), c,
                     budget_bound=len(filters) >= budget and _max_z(filters, freqs, target, sd) > stop_z,
                     contributions=[contrib[i] for i in order])


def preamp_db(filters: list[PeakingFilter], extra_db=None, margin_db: float = 0.5) -> float:
    """−max(0, peak of the combined response, 20 Hz-20 kHz) − margin.
    extra_db: response of any EQ merged into the same instance, on PREAMP_GRID."""
    h = response(filters, PREAMP_GRID)
    if extra_db is not None:
        h = h + extra_db
    return float(-max(0.0, h.max()) - margin_db)


PREAMP_GRID = np.geomspace(20, 20000, 2000)
