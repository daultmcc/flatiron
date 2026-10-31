"""LSP Parametric Equalizer x32 LeftRight settings (.cfg).

Written over a template exported from LSP 1.2.35 (band keys like `fl_3`,
gains as "x.xx db"): only the band, gain and mode lines change, so every other
setting stays exactly as LSP wrote it. Bands use filter mode 6, "APO (DR)",
which an offline check against LSP's LADSPA build showed to be the RBJ
cookbook filter exactly (bell and shelves, < 0.01 dB); the default RLC (BT)
mode differs by up to 1.7 dB on narrow bells.
"""

import re
from importlib import resources

from flatiron.apo import ApoFilter
from flatiron.export.plan import ExportPlan

MAX_BANDS = 32
MODE_APO = 6
TYPE = {"PK": 1, "HS": 3, "LS": 5}  # Bell, Hi-shelf, Lo-shelf
SIDE = {"L": "l", "R": "r"}
LINE = re.compile(r"^([a-z_]+[a-z0-9_]*) = (.*)$")


def template() -> str:
    return resources.files("flatiron.export").joinpath("templates/para_equalizer_x32_lr.cfg").read_text()


def overrides(plan: ExportPlan, mode: int = MODE_APO) -> dict[str, str]:
    out = {"bypass": "false", "clink": "false", "mode": "0", "g_in": f"{plan.preamp_db:.2f} db"}
    for e, filters in plan.channels.items():
        if len(filters) > MAX_BANDS:
            raise ValueError(f"{e} channel has {len(filters)} bands; LSP x32 holds {MAX_BANDS}")
        s = SIDE[e]
        for i in range(MAX_BANDS):
            if i >= len(filters):
                out[f"ft{s}_{i}"] = "0"
                continue
            f: ApoFilter = filters[i]
            out.update({
                f"ft{s}_{i}": str(TYPE[f.kind]), f"fm{s}_{i}": str(mode), f"s{s}_{i}": "0",
                f"xs{s}_{i}": "false", f"xm{s}_{i}": "false",
                f"f{s}_{i}": f"{f.fc:.5f}", f"g{s}_{i}": f"{f.gain_db:.2f} db", f"q{s}_{i}": f"{f.q:.5f}",
            })
    return out


def write(plan: ExportPlan, mode: int = MODE_APO, base: str | None = None) -> str:
    over = overrides(plan, mode)
    seen = set()
    lines = []
    for line in (base or template()).splitlines(keepends=True):
        m = LINE.match(line.rstrip("\n"))
        if m and m[1] in over:
            seen.add(m[1])
            line = f"{m[1]} = {over[m[1]]}\n"
        lines.append(line)
    missing = set(over) - seen
    if missing:
        raise ValueError(f"template lacks keys: {', '.join(sorted(missing)[:5])}")
    return "".join(lines)
