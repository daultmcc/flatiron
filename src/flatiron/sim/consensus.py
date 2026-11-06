"""The multi-headphone scenario (ADDENDUM.md, "Validation", criteria 1, 2, 8 and
the consensus half of 10). The rule for passing: the listener's own features are
never corrected, and only headphone-specific ones are.

The scenarios vary what could fool a reference or a vote: broad differences
between the headphones, a listener whose contour isn't ISO 226's, and a shared
peak that sits somewhat differently on each headphone."""

from dataclasses import dataclass, field, replace

import numpy as np

from flatiron.audio.biquad import PeakingFilter as Pk
from flatiron.fit.compare import Member, compare_ear
from flatiron.fit.consensus import Source, raw_consensus
from flatiron.fit.filters import response
from flatiron.fit.pipeline import CorrectionSettings, correct_ear, ear_reference
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
BROAD = {"hp1": [Pk(1600.0, -5.0, 1.0)], "hp2": [Pk(2500.0, 4.0, 1.0)], "hp3": [Pk(4000.0, -4.0, 1.0)]}
MOVED_OCT = {"hp1": -0.2, "hp2": 0.0, "hp3": 0.2}
NEAR_OCT = 1 / 24
FALSE_DB = 1.5


def _contour_deviation(f):
    """A listener off ISO 226 by about what the real profiles show: a little
    under it near 2.5 kHz, far over it at 8 to 10 kHz."""
    x = np.log2(np.asarray(f, float))
    return (-4.0 * np.exp(-0.5 * ((x - np.log2(2500)) / 0.5) ** 2)
            + 11.0 * np.exp(-0.5 * ((x - np.log2(9000)) / 0.5) ** 2))


@dataclass(frozen=True)
class Scenario:
    name: str
    tilt_scale: float = 1.0
    broad: bool = False  # each headphone has its own broad feature, about an octave wide
    contour_off: bool = False  # the listener isn't ISO 226
    moved: bool = False  # the 7 kHz peak sits up to 0.2 octave apart between headphones


SCENARIOS = {s.name: s for s in (Scenario("standard"), Scenario("broad", tilt_scale=2.0, broad=True),
                                 Scenario("contour", contour_off=True), Scenario("moved", moved=True))}


def _listener(name: str, scenario: Scenario) -> list[Pk]:
    if not scenario.moved:
        return LISTENER
    return [replace(p, fc=p.fc * 2 ** MOVED_OCT[name]) if p.fc == 7000.0 else p for p in LISTENER]


def headphone_truth(name: str, scenario: Scenario = SCENARIOS["standard"]) -> EarTruth:
    return EarTruth(tilt_db_per_oct=TILTS[name] * scenario.tilt_scale,
                    features=_listener(name, scenario) + CUP + HEADPHONES[name]
                    + (BROAD[name] if scenario.broad else []),
                    deviation=_contour_deviation if scenario.contour_off else None)


def headphone_fits(seed: int, sessions: int = 3, trials_per_session: int = 200,
                   scenario: Scenario = SCENARIOS["standard"]) -> dict[str, ProbitFit]:
    """One ear of each headphone: 600 trials, over reseats."""
    out = {}
    for n, name in enumerate(HEADPHONES):
        rng = np.random.default_rng(seed * 10 + n)
        seatings = [reseat(headphone_truth(name, scenario), rng) for _ in range(sessions)]
        out[name] = run_sessions(seatings, trials_per_session, seed=seed * 10 + n)
    return out


def _raw_sources(fits: dict[str, ProbitFit]) -> list[Source]:
    return [Source(name, f.grid, f.L, f.cov) for name, f in fits.items()]


@dataclass
class CompareCheck:
    """Criterion 1."""
    missed: list[tuple[str, float]] = field(default_factory=list)  # (headphone, feature fc)
    on_shared: list[tuple[str, float]] = field(default_factory=list)  # flagged on a listener or cup feature
    false_flags: int = 0  # away from anything the headphone really has

    @property
    def ok(self) -> bool:
        return not self.missed and not self.on_shared


def check_compare(fits: dict[str, ProbitFit], settings: CorrectionSettings = CorrectionSettings()) -> CompareCheck:
    members = [Member(n, "headphone", {"L": f}) for n, f in fits.items()]
    c = compare_ear("L", members, settings)
    out = CompareCheck()
    near = lambda s, fc: abs(np.log2(s.fc / fc)) <= max(s.width_oct / 2, NEAR_OCT)
    for name in fits:
        flags = [s for s in c.flags if s.label == name]
        own = [p.fc for p in HEADPHONES[name]]
        out.missed += [(name, fc) for fc in own if not any(near(s, fc) for s in flags)]
        for s in flags:
            if any(near(s, fc) for fc in own):
                continue
            shared = [p.fc for p in LISTENER + CUP if near(s, p.fc)]
            if shared:
                out.on_shared.append((name, shared[0]))
            else:
                out.false_flags += 1
    return out


@dataclass(frozen=True)
class Config:
    """A combination to judge. members: "all" or "others", for a consensus reference."""
    label: str
    members: str | None = None
    veto: bool = False
    smooth_oct: float = 0.5
    veto_mode: str = "feature"
    veto_import_db: float = 1.5


CONFIGS = (Config("Veto on ISO 226", veto=True),
           Config("Pointwise veto on ISO 226", veto=True, veto_mode="pointwise"),
           Config("Consensus of all three, smoothed 1/2 octave", members="all"),
           Config("Consensus of all three, smoothed, with veto", members="all", veto=True),
           Config("Consensus of all three, smoothed, with no-import veto", members="all", veto=True, veto_import_db=0.0),
           Config("Consensus of all three, smoothed, with pointwise veto", members="all", veto=True, veto_mode="pointwise"),
           Config("Consensus of the other two, smoothed, with veto", members="others", veto=True),
           Config("Consensus of all three, unsmoothed", members="all", smooth_oct=0.0))


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


def corrected(fits: dict[str, ProbitFit], name: str, config: Config, base: CorrectionSettings = CorrectionSettings()):
    """One headphone's correction under a config."""
    names = list(fits)
    others = [n for n in names if n != name]
    consensus, st = None, base
    if config.members:
        members = names if config.members == "all" else others
        sm = raw_consensus(_raw_sources({n: fits[n] for n in members}))
        consensus = ear_reference(sm, members, "", config.smooth_oct, members.index(name) if name in members else None)
        st = replace(st, reference="consensus:" + ",".join(members), consensus_smooth_oct=config.smooth_oct)
    voters = None
    if config.veto:
        st = replace(st, veto=tuple(others), veto_mode=config.veto_mode, veto_import_db=config.veto_import_db)
        voters = [(n, fits[n]) for n in others]
    return correct_ear("L", fits[name], st, consensus=consensus, voters=voters)


def check_corrections(fits: dict[str, ProbitFit], config: Config, scenario: Scenario = SCENARIOS["standard"],
                      settings: CorrectionSettings = CorrectionSettings(), window_oct: float = 1 / 24) -> list[CorrectionCheck]:
    out = []
    for name in fits:
        c = corrected(fits, name, config, settings)
        f = c.freqs
        narrow = response([x for x in c.added if x not in c.compensation], f)

        def at(fc):
            m = np.abs(np.log2(f / fc)) <= window_oct
            return float(narrow[m][np.argmax(np.abs(narrow[m]))])

        own_feats = HEADPHONES[name]
        listener = _listener(name, scenario)
        own = [(p.fc, p.gain_db, at(p.fc)) for p in own_feats]
        # In the moved scenario, moving the peak toward the group's frequency is
        # allowed: neither that peak nor where it might move to is judged.
        moved = [7000.0] + [p.fc for p in listener if abs(np.log2(p.fc / 7000)) < 0.25] if scenario.moved else []
        # A listener or cup feature right next to one of this headphone's own
        # isn't separable from it in the correction, so it isn't judged there.
        shared = [(p.fc, at(p.fc)) for p in listener + CUP
                  if p.fc not in moved and not any(abs(np.log2(p.fc / q.fc)) < 1 / 8 for q in own_feats)]
        away = np.ones(f.size, bool)
        for p in listener + CUP + own_feats:
            away &= np.abs(np.log2(f / p.fc)) > 1 / 6
        for fc in moved:
            away &= np.abs(np.log2(f / fc)) > 1 / 6
        away &= (f >= settings.f_lo) & (f <= settings.f_hi)
        worst = float(np.abs(narrow[away]).max()) if away.any() and np.any(narrow) else 0.0
        out.append(CorrectionCheck(name, own, shared, worst))
    return out


def consensus_coverage(fits: dict[str, ProbitFit], draws: int = 200, seed: int = 0) -> float:
    """Criterion 10, the consensus: redraw each raw curve from its covariance and
    see how much m moves, against the SD m reports, with the offsets held at
    their fitted values. The median ratio of the reported SD to the observed one
    (1 is right)."""
    rng = np.random.default_rng(seed)
    raw = _raw_sources(fits)
    sm = raw_consensus(raw)
    src = [replace(s, R=s.R - o) for s, o in zip(raw, sm.offsets)]
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
