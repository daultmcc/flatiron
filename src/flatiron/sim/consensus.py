"""The multi-headphone scenario (DESIGN.md, "Seating and multi-headphone scenarios", criteria 1 to 6, 8 and
the consensus half of 10). The rule for passing: the listener's own features are
never corrected, and only headphone-specific ones are.

The scenarios vary what could fool a reference or a vote: broad differences
between the headphones, a listener whose contour isn't the reference's, and a shared
peak that sits somewhat differently on each headphone."""

from dataclasses import dataclass, field, replace

import numpy as np

from flatiron.audio.biquad import PeakingFilter as Pk
from flatiron.fit.compare import Member, compare_ear
from flatiron.fit.consensus import (Source, ear_reference, shares, soft_median, split_consensus, split_parts)
from flatiron.fit.filters import response
from flatiron.fit.pipeline import CorrectionSettings, consensus_references, correct_ear
from flatiron.model.grid import common_grid, log_grid
from flatiron.model.probit import Noise, ProbitFit
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
    """A listener off the reference by about what the real profiles show: a little
    under it near 2.5 kHz, far over it at 8 to 10 kHz."""
    x = np.log2(np.asarray(f, float))
    return (-4.0 * np.exp(-0.5 * ((x - np.log2(2500)) / 0.5) ** 2)
            + 11.0 * np.exp(-0.5 * ((x - np.log2(9000)) / 0.5) ** 2))


@dataclass(frozen=True)
class Scenario:
    name: str
    tilt_scale: float = 1.0
    broad: bool = False  # each headphone has its own broad feature, about an octave wide
    contour_off: bool = False  # the listener isn't on the reference
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
    veto_mode: str = "feature"
    veto_import_db: float = 1.5


CONFIGS = (Config("Veto on the fixed reference", veto=True),
           Config("Pointwise veto on the fixed reference", veto=True, veto_mode="pointwise"),
           Config("Consensus of all three", members="all"),
           Config("Consensus of all three, with veto", members="all", veto=True),
           Config("Consensus of all three, with no-import veto", members="all", veto=True, veto_import_db=0.0),
           Config("Consensus of all three, with pointwise veto", members="all", veto=True, veto_mode="pointwise"),
           Config("Consensus of the other two", members="others"),
           Config("Consensus of the other two, with veto", members="others", veto=True))


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


def corrected(fits: dict[str, ProbitFit], name: str, config: Config, base: CorrectionSettings = CorrectionSettings(),
              speakers: dict[str, ProbitFit] | None = None):
    """One headphone's correction under a config, with any speakers in its consensus."""
    names = list(fits)
    others = [n for n in names if n != name]
    consensus, st = None, base
    if config.members:
        members = names if config.members == "all" else others
        st = replace(st, reference="consensus:" + ",".join(members + list(speakers or {})))
        spk = [Source(n, f.grid, f.L, f.cov, "speaker") for n, f in (speakers or {}).items()]
        sc = split_consensus(_raw_sources({n: fits[n] for n in members}), spk)
        own = members.index(name) if name in members else None
        consensus = consensus_references({"L": sc}, st, "", own, {"L": fits[name].grid})["L"]
    voters = None
    if config.veto:
        st = replace(st, veto=tuple(others), veto_mode=config.veto_mode, veto_import_db=config.veto_import_db)
        voters = [(n, fits[n]) for n in others]
    return correct_ear("L", fits[name], st, consensus=consensus, voters=voters)


def _narrow_correction(c, window_oct: float = 1 / 24):
    """The correction without its tonality compensation, and its largest value near a frequency."""
    f = c.freqs
    narrow = response([x for x in c.added if x not in c.compensation], f)

    def at(fc):
        m = np.abs(np.log2(f / fc)) <= window_oct
        return float(narrow[m][np.argmax(np.abs(narrow[m]))])

    return f, narrow, at


def check_corrections(fits: dict[str, ProbitFit], config: Config, scenario: Scenario = SCENARIOS["standard"],
                      settings: CorrectionSettings = CorrectionSettings(), window_oct: float = 1 / 24) -> list[CorrectionCheck]:
    out = []
    for name in fits:
        c = corrected(fits, name, config, settings)
        f, narrow, at = _narrow_correction(c, window_oct)

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


# -- speakers (criteria 3 to 6) --------------------------------------------------

# Each speaker: the listener's own structure, its own narrow ripple (correlated
# over about 1/12 octave, at this RMS above this frequency), its own peaks and a
# tilt; seen from 15 positions that each add the position model's deviation.
SPEAKERS = {"spk1": (3.0, 3000.0, [Pk(6300.0, 6.0, 6.0)], 1.5),
            "spk2": (3.0, 0.0, [], -2.0)}
SPEAKER_NOISE = Noise(1.3, 0.5, 0.03, 1.5)


def _ripple(rng: np.random.Generator, rms_db: float, from_hz: float, ell_oct: float = 1 / 12):
    f = log_grid(500, 24_000, 1 / 96)
    x = np.log2(f)
    C = np.exp(-0.5 * ((x[:, None] - x[None, :]) / ell_oct) ** 2) + 1e-8 * np.eye(f.size)
    r = np.linalg.cholesky(C) @ rng.normal(size=f.size)
    on = np.clip((x - np.log2(max(from_hz, 1.0))) * 3 + 1, 0, 1)  # fades in over 1/3 octave
    band = (on == 1) & (f <= 15_000)
    r = on * r * rms_db / np.sqrt(np.mean(r[band] ** 2))
    return lambda q: np.interp(np.log2(q), x, r)


def speaker_fit(name: str, seed: int, on_cup: bool = False, positions: int = 15, block: int = 32) -> ProbitFit:
    """One ear of a simulated speaker through the real scheduler, in blocks down
    the positions, as criterion 7 runs it. on_cup: speaker 1's own peak moved
    onto the cup's (the split-speakers variant)."""
    from flatiron.fit.run import SPEAKER_PRIORS
    from flatiron.model.hierarchical import fit_hierarchical
    from flatiron.sim.speaker import position_deviation, run_blocks

    n = list(SPEAKERS).index(name)
    rng = np.random.default_rng(seed * 100 + n)
    rms, from_hz, peaks, tilt = SPEAKERS[name]
    if on_cup:
        peaks = list(CUP)
    ripple = _ripple(rng, rms, from_hz)
    taus = (SPEAKER_PRIORS.tau_lo, SPEAKER_PRIORS.tau_mid, SPEAKER_PRIORS.tau_hi)
    base = EarTruth(tilt_db_per_oct=tilt, features=LISTENER + peaks)
    pos = [replace(base, deviation=lambda f, d=position_deviation(taus, rng): ripple(f) + d(f)) for _ in range(positions)]
    trials, grid, fit = run_blocks(pos, block, SPEAKER_NOISE, seed * 100 + n, sigma0=1.3, treble_noise=1.5,
                                   lapse=0.03, order_bias_db=1.0)
    return fit_hierarchical(trials, grid, SPEAKER_PRIORS, init=fit, noise=SPEAKER_NOISE, fit_tau=False)


@dataclass(frozen=True)
class SpeakerConfig:
    label: str
    criterion: int
    speakers: tuple[str, ...]
    on_cup: bool = False  # speaker 1's peak on the cup
    colour_db: float | None = None  # None: estimated (two speakers) or the default


SPEAKER_CONFIGS = (SpeakerConfig("Both speakers", 3, ("spk1", "spk2")),
                   SpeakerConfig("Both speakers, speaker 1's peak on the cup", 4, ("spk1", "spk2"), on_cup=True),
                   SpeakerConfig("Speaker 2 only", 5, ("spk2",)),
                   SpeakerConfig("Both speakers, low trust (6 dB colouration)", 6, ("spk1", "spk2"), colour_db=6.0))


@dataclass
class SpeakerCheck:
    """Criteria 3 to 6, for one headphone. cup: the correction at the cup's peak."""
    name: str
    criterion: int
    own: list[tuple[float, float, float]]
    shared: list[tuple[float, float]]  # the listener's own features
    cup: float
    worst_false_db: float
    vs_headphones_only_db: float  # largest change in the narrow correction against no speakers

    @property
    def ok(self) -> bool:
        base = (all(abs(c + g) <= FALSE_DB for _, g, c in self.own) and all(abs(c) <= FALSE_DB for _, c in self.shared)
                and self.worst_false_db <= FALSE_DB)
        if self.criterion in (3, 5):
            return base and abs(self.cup + CUP[0].gain_db) <= 2.0
        if self.criterion == 4:
            return base and abs(self.cup) <= FALSE_DB
        return self.vs_headphones_only_db <= 0.5


def check_speakers(fits: dict[str, ProbitFit], speakers: dict[str, ProbitFit], config: SpeakerConfig,
                   settings: CorrectionSettings = CorrectionSettings(), window_oct: float = 1 / 24) -> list[SpeakerCheck]:
    """speakers: the fits by name ("spk1", "spk2", and "spk1_cup" for the variant)."""
    use = {n: speakers[f"{n}_cup" if config.on_cup and n == "spk1" else n] for n in config.speakers}
    st = replace(settings, speaker_colour_db=config.colour_db)
    all3 = Config("Consensus of all three", members="all")
    out = []
    for name in fits:
        f, narrow, at = _narrow_correction(corrected(fits, name, all3, st, use), window_oct)
        _, alone, _ = _narrow_correction(corrected(fits, name, all3, st), window_oct)
        own_feats = HEADPHONES[name]
        own = [(p.fc, p.gain_db, at(p.fc)) for p in own_feats]
        shared = [(p.fc, at(p.fc)) for p in LISTENER if not any(abs(np.log2(p.fc / q.fc)) < 1 / 8 for q in own_feats)]
        away = (f >= settings.f_lo) & (f <= settings.f_hi)
        for p in LISTENER + CUP + own_feats:
            away &= np.abs(np.log2(f / p.fc)) > 1 / 6
        worst = float(np.abs(narrow[away]).max()) if away.any() else 0.0
        inside = (f >= settings.f_lo) & (f <= settings.f_hi)
        out.append(SpeakerCheck(name, config.criterion, own, shared, at(CUP[0].fc), worst,
                                float(np.max(np.abs(narrow - alone)[inside]))))
    return out


def speaker_fits(seed: int) -> dict[str, ProbitFit]:
    return {"spk1": speaker_fit("spk1", seed), "spk2": speaker_fit("spk2", seed),
            "spk1_cup": speaker_fit("spk1", seed, on_cup=True)}


def consensus_coverage(fits: dict[str, ProbitFit], draws: int = 200, seed: int = 0) -> float:
    """Criterion 10, the consensus: redraw each raw curve from its covariance and
    see how much the reference E = broad + k · narrow moves, against the SD it
    reports, with the offsets, the between-headphone spreads and k held at
    their fitted values. The median ratio of the reported SD to the observed
    one (1 is right)."""
    rng = np.random.default_rng(seed)
    raw = _raw_sources(fits)
    sc = split_consensus(raw)
    share = shares({"L": sc}).ears["L"]
    ref = ear_reference(sc, share)
    common = sc.grid
    Es = []
    for _ in range(draws):
        noisy = [replace(s, R=s.R + rng.multivariate_normal(np.zeros(s.R.size), s.cov, method="svd")) for s in raw]
        parts = [split_parts(s) for s in noisy]
        broad = [replace(p[1], R=p[1].R - o) for p, o in zip(parts, sc.broad.offsets)]
        mB = soft_median(broad, common, sc.broad.tau_hp, sc.broad.robust).m
        mN = soft_median([p[2] for p in parts], common, sc.narrow.tau_hp, sc.narrow.robust).m
        Es.append(mB + share.k * mN)
    observed = np.std(Es, axis=0)
    inside = (common >= 1000) & (common <= 13000)
    sd = np.sqrt(np.clip(np.diag(ref.cov), 0, None))
    return float(np.median(sd[inside] / observed[inside]))
