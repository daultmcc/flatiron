"""EasyEffects equalizer preset (JSON), the format Equaliser (macOS) imports.

Written over a template from an EasyEffects 7 preset with split channels:
output → equalizer#0 → left/right → band0..band31, gains in dB, type and
mode by name, preamp as input-gain (dB). EasyEffects runs LSP's equalizer,
so bands use mode "APO (DR)", the one that matches Flatiron's filters
exactly in LSP. How Equaliser maps q and mode on import isn't verified.
"""

import json
from importlib import resources

from flatiron.export.plan import ExportPlan

MAX_BANDS = 32
MODE_APO = "APO (DR)"
TYPE = {"PK": "Bell", "LS": "Lo-shelf", "HS": "Hi-shelf"}
SIDE = {"L": "left", "R": "right"}


def template() -> dict:
    return json.loads(resources.files("flatiron.export").joinpath("templates/easyeffects.json").read_text())


def write(plan: ExportPlan, mode: str = MODE_APO, base: dict | None = None) -> str:
    doc = base if base is not None else template()
    eq = doc["output"]["equalizer#0"]
    n = plan.max_bands()
    if n > MAX_BANDS:
        raise ValueError(f"{n} bands in a channel; EasyEffects holds {MAX_BANDS}")
    eq["split-channels"] = True
    eq["num-bands"] = max(n, 1)
    eq["input-gain"] = plan.preamp_db
    eq["output-gain"] = 0.0
    eq["bypass"] = False
    for e, filters in plan.channels.items():
        bands = eq[SIDE[e]]
        for i in range(eq["num-bands"]):
            b = bands[f"band{i}"]
            if i >= len(filters):
                b.update(type="Off", gain=0.0)  # the other channel uses more bands
                continue
            f = filters[i]
            b.update(type=TYPE[f.kind], mode=mode, frequency=f.fc, gain=f.gain_db, q=f.q,
                     slope="x1", mute=False, solo=False)
    return json.dumps(doc, indent=4) + "\n"
