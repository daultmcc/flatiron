"""Equalizer APO / AutoEQ text: one file per channel. PipeWire's param_eq
reads these through filename1/filename2, and REW-style importers accept them."""

from flatiron.apo import ApoFilter
from flatiron.export.plan import ExportPlan

KIND = {"PK": "PK", "LS": "LSC", "HS": "HSC"}


def filter_line(n: int, f: ApoFilter) -> str:
    return f"Filter {n}: ON {KIND[f.kind]} Fc {f.fc:.0f} Hz Gain {f.gain_db:.2f} dB Q {f.q:.3f}"


def channel_text(filters: list[ApoFilter], preamp_db: float) -> str:
    lines = [f"Preamp: {preamp_db:.2f} dB"] + [filter_line(i + 1, f) for i, f in enumerate(filters)]
    return "\n".join(lines) + "\n"


def write(plan: ExportPlan) -> dict[str, str]:
    """Returns {"L": text, "R": text}."""
    return {e: channel_text(fs, plan.preamp_db) for e, fs in plan.channels.items()}
