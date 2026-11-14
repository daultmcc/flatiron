"""The speaker scenario (DESIGN.md, "Seating and multi-headphone scenarios",
criterion 7): one ear through the real scheduler in blocks, one position each,
as the runner does them, and how well the fit recovers the average over
positions."""

from dataclasses import dataclass, replace

import numpy as np

from flatiron.audio.stimulus import stimulus_key
from flatiron.fit.run import SPEAKER_PRIORS
from flatiron.model.grid import log_grid
from flatiron.model.hierarchical import fit_hierarchical
from flatiron.model.probit import Noise, Priors, ProbitFit, Trials
from flatiron.session.scheduler import Scheduler, SchedulerConfig
from flatiron.sim.listener import EarTruth, SimulatedListener, design_example

KNOTS_HZ = (5000.0, 8000.0, 12000.0)


def position_deviation(taus, rng: np.random.Generator, ell_oct: float = 1 / 12):
    """One position's deviation from the average over positions: a draw from
    the model's own prior, with the spread at the three knots."""
    f = log_grid(500, 24_000, 1 / 96)
    x = np.log2(f)
    C = np.exp(-0.5 * ((x[:, None] - x[None, :]) / ell_oct) ** 2) + 1e-8 * np.eye(f.size)
    amp = np.exp(np.interp(x, np.log2(KNOTS_HZ), np.log(taus)))
    u = amp * (np.linalg.cholesky(C) @ rng.normal(size=f.size))
    return lambda q: np.interp(np.log2(q), x, u)


def run_blocks(positions: list[EarTruth], block: int, noise: Noise, seed: int = 0,
               priors: Priors = SPEAKER_PRIORS, **listener_kw) -> tuple[Trials, np.ndarray, ProbitFit]:
    """One ear, one block per position, each a new session that starts from the
    ones before (refining the grid then, as the runner does), with the
    listener's noise given."""
    cfg = SchedulerConfig(estimate_tau=False)
    hist, grid, fit = [], None, None
    for k, truth in enumerate(positions):
        lis = SimulatedListener({"L": truth}, seed=seed * 1000 + k, **listener_kw)
        sch = Scheduler(cfg, seed=seed * 1000 + k + 500, ears=("L",), priors=priors,
                        stimulus_key=stimulus_key(lis.stimulus), noise={"L": noise})
        if hist:
            sch.load_history("L", Trials.grouped(hist), grid, refine=True)
        for _ in range(block):
            plan = sch.next_trial()
            sch.record(plan, bool(lis.respond("L", plan.f_a, plan.f_b, plan.offset, plan.a_first)))
        st = sch.ears["L"]
        keep = np.asarray(st.group) == st.current
        t = st.trials()
        hist.append(Trials(t.f_a[keep], t.f_b[keep], t.offset[keep], t.a_louder[keep], t.order[keep], t.stimulus[keep]))
        grid, fit = st.grid, st.fit
    return Trials.grouped(hist), grid, fit


@dataclass
class SpeakerResult:
    rms: float  # RMS error against the average over positions, 1-13 kHz, level and slope removed
    coverage: float  # share of points the 1 SD band covers
    rms_half: float  # the same with the position spread assumed at half the truth
    coverage_half: float


def _score(fit: ProbitFit, truth: np.ndarray) -> tuple[float, float]:
    g = fit.grid
    n = np.flatnonzero((g >= 1000) & (g <= 13000))
    X = np.column_stack([np.ones(n.size), np.log2(g[n])])
    T = np.eye(n.size) - X @ np.linalg.solve(X.T @ X, X.T)
    err = T @ (fit.L[n] - truth[n])
    sd = np.sqrt(np.clip(np.diag(T @ fit.cov[np.ix_(n, n)] @ T.T), 0, None))
    return float(np.sqrt(np.mean(err**2))), float(np.mean(np.abs(err) < sd))


def criterion_7(seed: int, positions: int = 15, block: int = 32) -> SpeakerResult:
    """Criterion 7: L recovers the population average over positions, with a
    listener whose noise is known (as the listener's shared noise would be)."""
    rng = np.random.default_rng(seed)
    base = design_example("L")
    truth_taus = (SPEAKER_PRIORS.tau_lo, SPEAKER_PRIORS.tau_mid, SPEAKER_PRIORS.tau_hi)
    pos = [replace(base, deviation=position_deviation(truth_taus, rng)) for _ in range(positions)]
    noise = Noise(1.3, 0.5, 0.03, 1.5)
    trials, grid, fit = run_blocks(pos, block, noise, seed, sigma0=1.3, treble_noise=1.5, lapse=0.03,
                                   order_bias_db=1.0)
    full = fit_hierarchical(trials, grid, SPEAKER_PRIORS, init=fit, noise=noise, fit_tau=False)
    half_p = replace(SPEAKER_PRIORS, tau_lo=truth_taus[0] / 2, tau_mid=truth_taus[1] / 2, tau_hi=truth_taus[2] / 2)
    # Not estimated, the spread stays at the starting fit's, so that carries the half values.
    start = replace(full, tau_lo=half_p.tau_lo, tau_mid=half_p.tau_mid, tau_hi=half_p.tau_hi)
    half = fit_hierarchical(trials, grid, half_p, init=start, noise=noise, fit_tau=False)
    truth = base.level(grid)
    return SpeakerResult(*_score(full, truth), *_score(half, truth))


def simulate_speaker_profile(profile, truths: dict[str, EarTruth], blocks: int, block: int = 16, seed: int = 0,
                             spread=(SPEAKER_PRIORS.tau_lo, SPEAKER_PRIORS.tau_mid, SPEAKER_PRIORS.tau_hi),
                             **listener_kw) -> None:
    """Write speaker blocks into a profile through the real TrialRunner, down
    the position plan, each position with its own deviation."""
    from flatiron.session.runner import RunSettings, TrialRunner
    from flatiron.session.speaker import next_block

    rng = np.random.default_rng(seed)
    channel = profile.read()["channel"]
    for k in range(blocks):
        pos = next_block(profile)
        if pos is None:
            return
        truth = replace(truths[pos.ear], deviation=position_deviation(spread, rng))
        lis = SimulatedListener({pos.ear: truth}, seed=seed * 100 + k, **listener_kw)
        speaker = {"open_ear": pos.ear, "channel": channel, "position": pos.to_json(), "block": block}
        r = TrialRunner(profile, RunSettings(speaker=speaker, max_trials=block), seed=seed * 100 + k)
        for _ in range(block):
            tr = r.prepare()
            p = tr.plan
            a = bool(lis.respond(p.ear, p.f_a, p.f_b, p.offset, p.a_first))
            r.answer(tr, first_louder=(a == p.a_first), rt_ms=600.0)
        r.finish()
