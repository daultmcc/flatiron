"""The multi-headphone scenario (ADDENDUM.md, "Validation", criteria 1, 2, 8 and
the consensus half of 10). The rule for passing: the listener's own features are
never corrected, and only headphone-specific ones are."""

from dataclasses import dataclass, field

import numpy as np

from flatiron.audio.biquad import PeakingFilter as Pk
from flatiron.fit.consensus import Source, compare_ear, consensus_of, for_member
from flatiron.fit.filters import response
from flatiron.fit.pipeline import CorrectionSettings, correct_ear, member_source
from flatiron.model.grid import common_grid
from flatiron.model.probit import ProbitFit
from flatiron.sim.listener import EarTruth
from flatiron.sim.profile import reseat
from flatiron.sim.seating import run_sessions

LISTENER = [Pk(7000.0, 6.0, 8.0), Pk(11000.0, 5.0, 10.0), Pk(8400.0, -4.0, 12.0)]
CUP = [Pk(9500.0, 4.0, 6.0)]
HEADPHONES = {"hp1": [Pk(5800.0, 5.0, 5.0)],
              "hp2": [Pk(8000.0, -4.0, 10.0)],
              "hp3": [Pk(10500.0, 3.0, 4.0), Pk(3200.0, 3.0, 3.0)]}
TILTS = {"hp1": -1.0, "hp2": 0.5, "hp3": -0.3}
NEAR_OCT = 1 / 24
FALSE_DB = 1.5


def headphone_truth(name: str) -> EarTruth:
    return EarTruth(tilt_db_per_oct=TILTS[name], features=LISTENER + CUP + HEADPHONES[name])


def headphone_fits(seed: int, sessions: int = 3, trials_per_session: int = 200) -> dict[str, ProbitFit]:
    """One ear of each headphone: 600 trials, over reseats."""
    out = {}
    for n, name in enumerate(HEADPHONES):
        rng = np.random.default_rng(seed * 10 + n)
        seatings = [reseat(headphone_truth(name), rng) for _ in range(sessions)]
        out[name] = run_sessions(seatings, trials_per_session, seed=seed * 10 + n)
    return out


def _sources(fits: dict[str, ProbitFit], st: CorrectionSettings) -> list[Source]:
    return [member_source(name, "headphone", f, st) for name, f in fits.items()]


@dataclass
class CompareCheck:
    """Criterion 1."""
    missed: list[tuple[str, float]] = field(default_factory=list)  # (headphone, feature fc)
    on_shared: list[tuple[str, float]] = field(default_factory=list)  # flagged on a listener or cup feature
    false_flags: int = 0  # away from anything the headphone really has

    @property
    def ok(self) -> bool:
        return not self.missed and not self.on_shared


def check_compare(fits: dict[str, ProbitFit], k: float = 2.0, min_width_oct: float = 1 / 12) -> CompareCheck:
    c = compare_ear("L", _sources(fits, CorrectionSettings()), k, min_width_oct)
    out = CompareCheck()
    near = lambda f, fc: f.f_lo * 2**-NEAR_OCT <= fc <= f.f_hi * 2**NEAR_OCT
    for e in c.entries:
        name = e.source.label
        own = [p.fc for p in HEADPHONES[name]]
        out.missed += [(name, fc) for fc in own if not any(near(f, fc) for f in e.flags)]
        for f in e.flags:
            if any(near(f, fc) for fc in own):
                continue
            shared = [p.fc for p in LISTENER + CUP if near(f, p.fc)]
            if shared:
                out.on_shared.append((name, shared[0]))
            else:
                out.false_flags += 1
    return out


@dataclass
class CorrectionCheck:
    """Criterion 2, for one headphone."""
    name: str
    own: list[tuple[float, float, float]]  # (fc, gain, correction there)
    shared: list[tuple[float, float]]  # (fc, correction there)
    worst_false_db: float

    @property
    def ok(self) -> bool:
        return (all(abs(c + g) <= FALSE_DB for _, g, c in self.own)
                and all(abs(c) <= FALSE_DB for _, c in self.shared) and self.worst_false_db <= FALSE_DB)


def check_corrections(fits: dict[str, ProbitFit], settings: CorrectionSettings = CorrectionSettings(),
                      window_oct: float = 1 / 24) -> list[CorrectionCheck]:
    sources = _sources(fits, settings)
    sm = consensus_of(sources)
    out = []
    names = list(fits)
    for i, name in enumerate(names):
        c = correct_ear("L", fits[name], settings, consensus=for_member(sm, i, fits[name].grid, names))
        f = c.freqs
        h = response(c.added, f)
        narrow = response([x for x in c.added if x not in c.compensation], f)

        def at(fc):
            m = np.abs(np.log2(f / fc)) <= window_oct
            return float(narrow[m][np.argmax(np.abs(narrow[m]))])

        own_feats = HEADPHONES[name]
        own = [(p.fc, p.gain_db, at(p.fc)) for p in own_feats]
        # A listener or cup feature right next to one of this headphone's own
        # isn't separable from it in the correction, so it isn't judged there.
        shared = [(p.fc, at(p.fc)) for p in LISTENER + CUP
                  if not any(abs(np.log2(p.fc / q.fc)) < 1 / 8 for q in own_feats)]
        away = np.ones(f.size, bool)
        for p in LISTENER + CUP + own_feats:
            away &= np.abs(np.log2(f / p.fc)) > 1 / 6
        away &= (f >= settings.f_lo) & (f <= settings.f_hi)
        worst = float(np.abs(narrow[away]).max()) if away.any() and np.any(narrow) else 0.0
        out.append(CorrectionCheck(name, own, shared, worst))
    return out


def consensus_coverage(fits: dict[str, ProbitFit], draws: int = 200, seed: int = 0,
                       settings: CorrectionSettings = CorrectionSettings()) -> float:
    """Criterion 10, the consensus: redraw each residual from its covariance and
    see how much m moves, against the SD m reports. The median ratio of the
    reported SD to the observed one (1 is right)."""
    rng = np.random.default_rng(seed)
    src = _sources(fits, settings)
    sm = consensus_of(src)
    common = common_grid([s.grid for s in src])
    from flatiron.fit.consensus import soft_median

    ms = []
    for _ in range(draws):
        noisy = [Source(s.label, s.grid, s.R + rng.multivariate_normal(np.zeros(s.R.size), s.cov, method="svd"), s.cov)
                 for s in src]
        ms.append(soft_median(noisy, common, sm.tau_hp, sm.robust).m)
    observed = np.std(ms, axis=0)
    inside = (common >= 1000) & (common <= 13000)
    return float(np.median(sm.sd[inside] / observed[inside]))
