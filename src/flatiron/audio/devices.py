"""Output device listing and checks (DESIGN.md, "Setup")."""

from dataclasses import dataclass, field

from flatiron import FS

BLUETOOTH_HINTS = ("bluez", "bluetooth", "airpods", "a2dp", "bt ")


def sd():
    try:
        import sounddevice
    except OSError as e:  # PortAudio missing
        raise SystemExit(f"sounddevice could not load PortAudio: {e}") from e
    return sounddevice


@dataclass
class OutputDevice:
    index: int
    name: str
    hostapi: str
    channels: int
    default_samplerate: float
    is_default: bool = False
    warnings: list[str] = field(default_factory=list)


def check_device(d: OutputDevice) -> list[str]:
    w = []
    if any(h in d.name.lower() for h in BLUETOOTH_HINTS):
        w.append("looks like Bluetooth (lossy, variable latency); use a wired output")
    if d.channels < 2:
        w.append("mono output; Flatiron needs separate L/R channels")
    if int(d.default_samplerate) != FS:
        w.append(f"native rate {d.default_samplerate:g} Hz, not {FS} Hz")
    return w


def list_output_devices() -> list[OutputDevice]:
    s = sd()
    hostapis = s.query_hostapis()
    default_out = s.default.device[1]
    out = []
    for i, info in enumerate(s.query_devices()):
        if info["max_output_channels"] <= 0:
            continue
        d = OutputDevice(i, info["name"], hostapis[info["hostapi"]]["name"],
                         info["max_output_channels"], info["default_samplerate"],
                         is_default=(i == default_out))
        d.warnings = check_device(d)
        out.append(d)
    return out
