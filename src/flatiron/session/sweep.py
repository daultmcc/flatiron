"""Optional coarse sweep (DESIGN.md, "Sweep and tone-generator marks").

A slow narrowband sweep, up then down, per ear. The listener taps at each peak
or dip. A tap lands a reaction time after the true crossing, so an up-sweep tap
reads high and a down-sweep tap reads low by the same amount in log frequency;
averaging matched pairs cancels the lag.
"""

from dataclasses import dataclass

import numpy as np

from flatiron import FS
from flatiron.audio.stimulus import raised_cosine_gate

F_LO, F_HI = 1000.0, 14_000.0
DURATION_S = 30.0  # each direction (40 s was slow enough to blur peaks)
GAP_S = 1.0
TONES = 9
SPREAD_OCT = 1 / 24  # tones spread over ±this around the sweep frequency
MAX_PAIR_OCT = 0.4  # up/down taps further apart than this aren't the same feature


@dataclass(frozen=True)
class SweepTiming:
    f_lo: float = F_LO
    f_hi: float = F_HI
    duration: float = DURATION_S
    gap: float = GAP_S

    @property
    def total(self) -> float:
        return 2 * self.duration + self.gap

    @property
    def rate_oct_s(self) -> float:
        return np.log2(self.f_hi / self.f_lo) / self.duration

    def freq_at(self, t: float) -> tuple[float, str] | None:
        """Sweep frequency at playback time t, and direction ('up'/'down')."""
        d = self.duration
        if 0 <= t <= d:
            return self.f_lo * 2 ** (self.rate_oct_s * t), "up"
        t2 = t - d - self.gap
        if 0 <= t2 <= d:
            return self.f_hi * 2 ** (-self.rate_oct_s * t2), "down"
        return None


def sweep_signal(timing: SweepTiming = SweepTiming(), fs: int = FS, seed: int = 0) -> np.ndarray:
    """Unit-RMS mono sweep: a cluster of random-phase tones following a log sweep."""
    rng = np.random.default_rng(seed)
    n = int(round(timing.duration * fs))
    t = np.arange(n) / fs
    a = timing.rate_oct_s * np.log(2)
    offsets = np.linspace(-SPREAD_OCT, SPREAD_OCT, TONES)
    up = np.zeros(n)
    # Phase of f_lo * e^(a t) is 2π f_lo (e^(a t) − 1) / a.
    base = 2 * np.pi * timing.f_lo * np.expm1(a * t) / a
    for o in offsets:
        up += np.sin(base * 2.0**o + rng.uniform(0, 2 * np.pi))
    down = np.zeros(n)
    base = 2 * np.pi * timing.f_hi * -np.expm1(-a * t) / a
    for o in offsets:
        down += np.sin(base * 2.0**o + rng.uniform(0, 2 * np.pi))
    gate = raised_cosine_gate(n, int(0.05 * fs))
    x = np.concatenate([up * gate, np.zeros(int(round(timing.gap * fs))), down * gate])
    return x / np.sqrt(np.mean((up * gate) ** 2))


@dataclass
class Mark:
    freq: float
    kind: str  # "peak" or "dip"
    lag_s: float  # reaction time implied by the pair


def pair_taps(taps: list[tuple[float, str]], timing: SweepTiming = SweepTiming()) -> tuple[list[Mark], int]:
    """taps: (playback time, kind). Returns matched marks and the unmatched count."""
    up, down = [], []
    for t, kind in taps:
        r = timing.freq_at(t)
        if r is not None:
            (up if r[1] == "up" else down).append((np.log2(r[0]), kind))
    marks, used = [], set()
    for xu, ku in sorted(up):
        best, bd = None, MAX_PAIR_OCT
        for k, (xd, kd) in enumerate(down):
            # The up tap reads high, the down tap low: xu >= xd for a real pair.
            if k in used or kd != ku or xu < xd:
                continue
            if xu - xd < bd:
                best, bd = k, xu - xd
        if best is not None:
            used.add(best)
            xd = down[best][0]
            marks.append(Mark(float(2 ** ((xu + xd) / 2)), ku, float((xu - xd) / 2 / timing.rate_oct_s)))
    return sorted(marks, key=lambda m: m.freq), len(up) + len(down) - 2 * len(marks)
