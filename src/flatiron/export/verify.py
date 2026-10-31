"""Truth checks: run an exported LSP .cfg through LSP's own LADSPA build
and compare with Flatiron's prediction (DESIGN.md, "Exporter truth checks").
Tolerance there: 0.3 dB from 1 to 14 kHz."""

import re
from pathlib import Path

import numpy as np

from flatiron.export.ladspa_host import Plugin
from flatiron.export.plan import ExportPlan, response_db

LSP_LADSPA = "/usr/lib/ladspa/lsp-plugins-ladspa.so"
LSP_LABEL = "para_equalizer_x32_lr"
FIELDS = {"ft": "Filter type {side} {i}", "fm": "Filter mode {side} {i}", "s": "Filter slope {side} {i}",
          "f": "Frequency {side} {i} (Hz)", "w": "Filter Width {side} {i} (oct)", "g": "Gain {side} {i} (G)",
          "q": "Quality factor {side} {i}", "xs": "Filter solo {side} {i}", "xm": "Filter mute {side} {i}"}
GLOBAL = {"g_in": "Input gain (G)", "g_out": "Output gain (G)", "bypass": "Bypass", "mode": "Equalizer mode",
          "shift": "Shift gain (G)", "bal": "Output balance (%)"}


def _value(text: str) -> float:
    text = text.strip()
    if text in ("true", "false"):
        return float(text == "true")
    if text.endswith(" db"):
        return 10 ** (float(text[:-3]) / 20)
    return float(text)


def cfg_controls(cfg_text: str) -> dict[str, float]:
    """LSP .cfg → LADSPA control values (the subset that shapes the sound)."""
    out = {}
    for line in cfg_text.splitlines():
        m = re.match(r"^([a-z_]+?)([lr])_(\d+) = (.*)$", line)
        if m and m[1] in FIELDS:
            out[FIELDS[m[1]].format(side="Left" if m[2] == "l" else "Right", i=m[3])] = _value(m[4])
            continue
        m = re.match(r"^([a-z_]+) = (.*)$", line)
        if m and m[1] in GLOBAL:
            out[GLOBAL[m[1]]] = _value(m[2])
    return out


def lsp_response_db(controls: dict[str, float], fs: int = 48_000, nfft: int = 32768
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Measured magnitude response (freqs, (n, 2) dB) of LSP with these controls."""
    plugin = Plugin(LSP_LADSPA, LSP_LABEL)
    x = np.zeros((fs * 2, 2), np.float32)
    i0 = fs  # well after any parameter smoothing at start-up
    x[i0, :] = 0.5
    y = plugin.run(controls, x, fs)
    seg = y[i0 - 256:i0 - 256 + nfft].astype(float)
    h = np.fft.rfft(seg, axis=0) / 0.5
    return np.fft.rfftfreq(nfft, 1 / fs), 20 * np.log10(np.abs(h) + 1e-12)


def check_lsp(cfg_text: str, plan: ExportPlan, f_lo=1000.0, f_hi=14000.0) -> dict[str, float]:
    """Max |measured − predicted| (dB) per channel over [f_lo, f_hi], preamp included."""
    return _check(cfg_controls(cfg_text), plan, f_lo, f_hi)


def _check(controls, plan, f_lo, f_hi):
    f, h = lsp_response_db(controls)
    sel = (f >= f_lo) & (f <= f_hi)
    return {e: float(np.abs(h[sel, c] - (response_db(plan.channels[e], f[sel]) + plan.preamp_db)).max())
            for c, e in enumerate(("L", "R"))}


def available() -> bool:
    return Path(LSP_LADSPA).exists()


EE_TYPES = {"Off": 0, "Bell": 1, "Hi-pass": 2, "Hi-shelf": 3, "Lo-pass": 4, "Lo-shelf": 5}
EE_MODES = ["RLC (BT)", "RLC (MT)", "BWC (BT)", "BWC (MT)", "LRX (BT)", "LRX (MT)", "APO (DR)"]


def easyeffects_controls(preset_text: str) -> dict[str, float]:
    """EasyEffects preset → LSP LADSPA controls (EasyEffects runs the same filters)."""
    import json

    eq = json.loads(preset_text)["output"]["equalizer#0"]
    out = {"Input gain (G)": 10 ** (eq["input-gain"] / 20), "Output gain (G)": 10 ** (eq["output-gain"] / 20),
           "Bypass": float(eq["bypass"]), "Shift gain (G)": 1.0}
    for side, name in (("left", "Left"), ("right", "Right")):
        for i in range(eq["num-bands"]):
            b = eq[side][f"band{i}"]
            out.update({
                f"Filter type {name} {i}": EE_TYPES[b["type"]], f"Filter mode {name} {i}": EE_MODES.index(b["mode"]),
                f"Filter slope {name} {i}": int(b["slope"][1:]) - 1, f"Frequency {name} {i} (Hz)": b["frequency"],
                f"Gain {name} {i} (G)": 10 ** (b["gain"] / 20), f"Quality factor {name} {i}": b["q"],
                f"Filter Width {name} {i} (oct)": b["width"], f"Filter mute {name} {i}": float(b["mute"]),
                f"Filter solo {name} {i}": float(b["solo"]),
            })
    return out


def check_easyeffects(preset_text: str, plan: ExportPlan, f_lo=1000.0, f_hi=14000.0) -> dict[str, float]:
    """As check_lsp, for an EasyEffects preset (what EasyEffects on Linux would do with it)."""
    return _check(easyeffects_controls(preset_text), plan, f_lo, f_hi)


# -- PipeWire --------------------------------------------------------------

def _pipewire_test_conf(plan: ExportPlan, tag: str) -> str:
    """A standalone filter-chain with the plan's param_eq between a virtual
    sink and a virtual source, so nothing reaches a real device."""
    from flatiron.export.pipewire import filter_chain_args

    args = filter_chain_args(
        plan, f"flatiron check {tag}", f"flatiron_check_{tag}",
        "media.class = Audio/Sink audio.rate = 48000 priority.session = 0 priority.driver = 0",
        "media.class = Audio/Source audio.rate = 48000 priority.session = 0 priority.driver = 0")
    return f"""context.properties = {{ log.level = 0 }}
context.spa-libs = {{ audio.convert.* = audioconvert/libspa-audioconvert
                      support.* = support/libspa-support }}
context.modules = [
  {{ name = libpipewire-module-rt flags = [ ifexists nofail ] }}
  {{ name = libpipewire-module-protocol-native }}
  {{ name = libpipewire-module-client-node }}
  {{ name = libpipewire-module-adapter }}
  {{ name = libpipewire-module-filter-chain
{args}
  }}
]
"""


def pipewire_available() -> bool:
    import shutil
    import subprocess

    if not all(shutil.which(b) for b in ("pipewire", "pw-play", "pw-record")):
        return False
    return subprocess.run(["pw-cli", "info", "0"], capture_output=True, timeout=5).returncode == 0


def check_pipewire(plan: ExportPlan, workdir, f_lo=1000.0, f_hi=14000.0, fs=48_000) -> dict[str, float]:
    """Run the plan's param_eq in a private filter-chain on the running
    PipeWire, play noise through it and record the result. Returns max
    |measured − predicted| (dB, preamp included) per channel."""
    import os
    import subprocess
    import time
    import warnings
    from pathlib import Path

    from scipy import signal
    from scipy.io import wavfile

    d = Path(workdir)
    tag = f"{os.getpid()}"
    conf = d / "check.conf"
    conf.write_text(_pipewire_test_conf(plan, tag))
    rng = np.random.default_rng(0)
    x = (rng.standard_normal((fs * 3, 2)) * 0.05).astype(np.float32)
    x[: fs // 4] = 0
    x[-fs // 4:] = 0
    wavfile.write(d / "in.wav", fs, x)
    out = d / "out.wav"
    chain = subprocess.Popen(["pipewire", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        rec = subprocess.Popen(["pw-record", "--target", f"effect_output.flatiron_check_{tag}", "--rate", str(fs),
                                "--channels", "2", "--format", "f32", str(out)], stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        subprocess.run(["pw-play", "--target", f"effect_input.flatiron_check_{tag}", str(d / "in.wav")],
                       check=True, timeout=30, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        rec.terminate()
        rec.wait(5)
    finally:
        chain.terminate()
        chain.wait(5)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _, y = wavfile.read(out)
    y = y.astype(float)
    if y.ndim != 2 or len(y) < len(x) // 2 or not np.any(y):
        raise RuntimeError("recorded nothing from the test filter-chain")
    lag = int(np.argmax(signal.correlate(y[:, 0], x[:, 0], mode="full", method="fft")) - (len(x) - 1))
    y = y[lag:lag + len(x)]
    n = min(len(y), len(x))
    err = {}
    for c, e in enumerate(("L", "R")):
        f, pxy = signal.csd(x[:n, c], y[:n, c], fs, nperseg=8192)
        _, pxx = signal.welch(x[:n, c], fs, nperseg=8192)
        sel = (f >= f_lo) & (f <= f_hi)
        h = 20 * np.log10(np.abs(pxy[sel]) / pxx[sel])
        err[e] = float(np.abs(h - (response_db(plan.channels[e], f[sel]) + plan.preamp_db)).max())
    return err
