"""PipeWire filter-chain drop-in using the builtin param_eq, filters inline.

Checked on PipeWire 1.6 by measuring a private filter-chain: bq_peaking,
bq_lowshelf and bq_highshelf match Flatiron's (RBJ) filters within 0.01 dB,
filters1/filters2 are the left/right channels, and a bq_highshelf at
freq 0 is a broadband gain, which is how the preamp is written (the same
thing param_eq does with an APO file's Preamp line).

Install: copy to ~/.config/pipewire/pipewire.conf.d/ and restart PipeWire;
a "Flatiron <profile>" sink appears. Or run it on its own: pipewire -c FILE.
"""

from flatiron.apo import ApoFilter
from flatiron.export.plan import ExportPlan

TYPE = {"PK": "bq_peaking", "LS": "bq_lowshelf", "HS": "bq_highshelf"}


def _filter(f: ApoFilter) -> str:
    return f"{{ type = {TYPE[f.kind]}, freq = {f.fc:.2f}, gain = {f.gain_db:.2f}, q = {f.q:.4f} }}"


def preamp_filter(gain_db: float) -> str:
    return f"{{ type = bq_highshelf, freq = 0, gain = {gain_db:.2f}, q = 1.0 }}  # preamp"


def eq_config(plan: ExportPlan, indent: str = "                    ") -> str:
    """The param_eq config body: filters1 (left) and filters2 (right)."""
    out = []
    for n, e in ((1, "L"), (2, "R")):
        rows = [preamp_filter(plan.preamp_db)] + [_filter(f) for f in plan.channels[e]]
        body = "\n".join(f"{indent}    {r}" for r in rows)
        out.append(f"{indent}filters{n} = [\n{body}\n{indent}]")
    return "\n".join(out)


def filter_chain_args(plan: ExportPlan, description: str, name: str, capture_props: str, playback_props: str) -> str:
    return f'''    args = {{
        node.description = "{description}"
        media.name = "{description}"
        audio.channels = 2
        audio.position = [ FL FR ]
        filter.graph = {{
            nodes = [
                {{ type = builtin name = eq label = param_eq
                  config = {{
{eq_config(plan)}
                  }}
                }}
            ]
            inputs = [ "eq:In 1" "eq:In 2" ]
            outputs = [ "eq:Out 1" "eq:Out 2" ]
        }}
        capture.props = {{ node.name = "effect_input.{name}" {capture_props} }}
        playback.props = {{ node.name = "effect_output.{name}" {playback_props} }}
    }}'''


def write(plan: ExportPlan, label: str, slug: str) -> str:
    desc = f"Flatiron {label}"
    return f'''# Flatiron correction for {label} (fit {plan.fit_hash}{", merged" if plan.merged else ""}).
# Copy to ~/.config/pipewire/pipewire.conf.d/ and restart PipeWire, then pick
# the "{desc}" output. Left = filters1, right = filters2.
context.modules = [
  {{ name = libpipewire-module-filter-chain
{filter_chain_args(plan, desc, "flatiron." + slug, "media.class = Audio/Sink", "node.passive = true")}
  }}
]
'''
