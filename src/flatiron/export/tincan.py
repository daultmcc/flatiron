"""TinCan preset (TOML, format 1; TINCAN_DESIGN.md, Presets).

TinCan's bells are RBJ peaking filters, the same as Flatiron's. Presets carry
no preamp (TinCan sets one for everything loaded), so the plan's preamp isn't
written. A filter that's identical in both channels is written once, as LR.
"""

import json
import os
from pathlib import Path

from flatiron.export.plan import ExportPlan

FORMAT = 1
MAX_FILTERS = 256
TYPE = {"PK": "bell", "LS": "low_shelf", "HS": "high_shelf"}
# TinCan clamps anything outside these on load.
F_RANGE, GAIN_RANGE, Q_RANGE = (20.0, 20_000.0), (-24.0, 24.0), (0.1, 40.0)


def presets_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "tincan" / "presets"


def slug(name: str) -> str:
    """TinCan's file stem for a preset name (tincan-core preset::slug)."""
    out = ""
    for c in name.strip().lower():
        if c.isalnum():
            out += c
        elif not out.endswith("-"):
            out += "-"
    out = out.strip("-")
    return "preset" if not out and name.strip() else out


def _entries(plan: ExportPlan) -> list[tuple[str, object]]:
    left, right = list(plan.channels["L"]), list(plan.channels["R"])
    both = [f for f in left if f in right]
    for f in both:
        left.remove(f)
        right.remove(f)
    return [("LR", f) for f in both] + [("L", f) for f in left] + [("R", f) for f in right]


def write(plan: ExportPlan, name: str) -> str:
    entries = _entries(plan)
    if len(entries) > MAX_FILTERS:
        raise ValueError(f"{len(entries)} filters; TinCan holds {MAX_FILTERS}")
    lines = [f"format = {FORMAT}", f"name = {json.dumps(name, ensure_ascii=False)}"]
    for ch, f in entries:
        for value, (lo, hi), what in ((f.fc, F_RANGE, "frequency"), (f.gain_db, GAIN_RANGE, "gain"),
                                      (f.q, Q_RANGE, "Q")):
            if not lo <= value <= hi:
                raise ValueError(f"A filter's {what} ({value:g}) is outside TinCan's range ({lo:g} to {hi:g}).")
        lines += ["", "[[filter]]", f'type = "{TYPE[f.kind]}"', f'channel = "{ch}"',
                  f"frequency = {round(float(f.fc), 2)!r}", f"gain = {round(float(f.gain_db), 3)!r}",
                  f"q = {round(float(f.q), 4)!r}", "enabled = true"]
    return "\n".join(lines) + "\n"
