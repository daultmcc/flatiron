"""TinCan in the audio path: what EQ it's applying (DESIGN.md, "Measurement context").

TinCan (a PipeWire EQ) makes a sink named "tincan" the default sink while it
runs, writes its state to $XDG_STATE_HOME/tincan/state.toml on every change to
its loaded presets or global settings, and keeps its presets as TOML files in
$XDG_DATA_HOME/tincan/presets. Flatiron plays to the default sink, so when
that's TinCan, what TinCan applies is the active presets of the list being
heard (unless it's in pass-through), then its preamp, at the sink's sample
rate. All of it is read here, so each burst's gain can be recorded as a known
EQ gain, as `--active-eq` does for an APO file.

Not visible from outside: a preset's unsaved edits in an open edit window
(TinCan plays those, not the file).
"""

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from flatiron.audio.biquad import peaking_coeffs, shelf_coeffs, sos_response_db
from flatiron.audio.stimulus import spectrum_offsets_oct
from flatiron.export.tincan import presets_dir

SINK = "tincan"
# Devices that play to the default sink (a named hardware device bypasses TinCan).
DEFAULT_DEVICES = ("default", "pipewire", "pulse", "sysdefault")
# TinCan's own constants (tincan-core filter.rs, response.rs).
MIN_FREQ, MAX_FREQ, MAX_FREQ_FRACTION = 20.0, 20_000.0, 0.45
ANALYSIS_POINTS = 480
SHELVES = {"low_shelf": "LS", "high_shelf": "HS"}


def state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "tincan" / "state.toml"


@dataclass(frozen=True, eq=False)
class TinCanEq:
    """The EQ TinCan applies: per channel, its biquads, then the preamp."""
    names: tuple[str, ...]  # the active presets' names, in order
    ids: tuple[str, ...]
    sos: dict  # "L"/"R": SOS rows at `rate`
    preamp_db: float
    preamp_mode: str
    rate: int
    texts: dict = field(default_factory=dict)  # preset id: its file

    def _signature(self) -> tuple:
        return self.ids, tuple(sorted(self.texts.items())), round(self.preamp_db, 6), self.preamp_mode, self.rate

    def __eq__(self, other) -> bool:
        return isinstance(other, TinCanEq) and self._signature() == other._signature()

    def __hash__(self) -> int:
        return hash(self._signature())

    def gain_db(self, freqs, ear: str) -> np.ndarray:
        """Response (dB) in one channel, preamp included."""
        f = np.clip(np.asarray(freqs, float), 1.0, self.rate / 2 * 0.999)
        return sos_response_db(self.sos[ear], f, self.rate) + self.preamp_db

    def band_gain_db(self, freqs, stimulus: str, ear: str) -> np.ndarray:
        """Power gain (dB) over each burst's spectrum in one channel, as
        ApoEq.band_gain_db (and, unlike it, with the preamp: absolute levels
        matter in threshold sessions)."""
        f = np.atleast_1d(np.asarray(freqs, float))
        pts = f[:, None] * 2.0 ** spectrum_offsets_oct(stimulus)[None, :]
        g = self.gain_db(pts.ravel(), ear).reshape(pts.shape)
        return 10 * np.log10(np.mean(10 ** (g / 10), axis=1))

    @property
    def label(self) -> str:
        return "TinCan: " + " + ".join(self.names)

    def record(self) -> dict:
        return {"presets": list(self.names), "files": dict(self.texts), "preamp_db": round(self.preamp_db, 3),
                "preamp_mode": self.preamp_mode, "sample_rate": self.rate}


def _rows(filters, rate: float) -> dict:
    rows = {"L": [], "R": []}
    top = min(MAX_FREQ, MAX_FREQ_FRACTION * rate)
    for f in filters:
        fc = float(np.clip(f["frequency"], MIN_FREQ, top))
        if f["type"] == "bell":
            row = peaking_coeffs(fc, f["gain"], f["q"], rate)
        elif f["type"] in SHELVES:
            row = shelf_coeffs(SHELVES[f["type"]], fc, f["gain"], f["q"], rate)
        else:
            raise ValueError(f"unknown TinCan filter type {f['type']!r}")
        for ch in "LR":
            if ch in f.get("channel", "LR"):
                rows[ch].append(row)
    flat = np.array([[1.0, 0, 0, 1.0, 0, 0]])
    return {ch: np.vstack(r) if r else flat for ch, r in rows.items()}


def preamp(sos: dict, rate: int, mode: str, manual_db: float) -> float:
    """TinCan's preamp (tincan-core response.rs, Headroom)."""
    if mode == "manual":
        return float(manual_db)
    lo, hi = np.log(MIN_FREQ), np.log(MAX_FREQ)
    grid = np.exp(lo + (hi - lo) * np.arange(ANALYSIS_POINTS) / (ANALYSIS_POINTS - 1))
    db = np.concatenate([sos_response_db(sos[ch], grid, rate) for ch in "LR"])
    if mode == "safe":
        return float(-max(0.0, db.max()))
    return float(-10 * np.log10(np.mean(10 ** (db / 10))))


def read_eq(state_path: Path, presets: Path, rate: int) -> TinCanEq | None:
    """The EQ that the state file describes (None: pass-through, or nothing active)."""
    state = tomllib.loads(state_path.read_text())
    if state.get("bypass", False):
        return None
    loaded = state.get("loaded_b" if state.get("ab", "a") == "b" else "loaded", [])
    ids, names, texts, filters = [], [], {}, []
    for entry in loaded:
        if not entry.get("active", True):
            continue
        path = presets / f"{entry['id']}.toml"
        text = path.read_text()  # a missing preset: TinCan unloads it, so it's caught next time
        preset = tomllib.loads(text)
        ids.append(entry["id"])
        names.append(preset.get("name", entry["id"]))
        texts[entry["id"]] = text
        filters += [f for f in preset.get("filter", []) if f.get("enabled", True)]
    if not filters:
        return None
    sos = _rows(filters, rate)
    mode = state.get("preamp_mode", "match_bypass")
    return TinCanEq(tuple(names), tuple(ids), sos, preamp(sos, rate, mode, state.get("manual_preamp_db", 0.0)),
                    mode, rate, texts)


def _pactl(*args) -> str | None:
    try:
        out = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def sink_rate() -> int | None:
    """TinCan's sample rate if it's the default sink, else None."""
    if shutil.which("pactl") is None or (_pactl("get-default-sink") or "").strip() != SINK:
        return None
    for line in (_pactl("list", "short", "sinks") or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[1] == SINK:
            for p in parts[3].split():
                if p.endswith("Hz") and p[:-2].isdigit():
                    return int(p[:-2])
    return None


def follows_default_sink(device: dict | None) -> bool:
    name = (device or {}).get("name")
    return name is None or name in DEFAULT_DEVICES or SINK in str(name).lower()


class TinCanWatch:
    """Checks TinCan at every burst; re-reads its files only when something
    changed. current() is the EQ in effect, or None."""

    def __init__(self, device: dict | None = None, rate=sink_rate, state_path: Path | None = None,
                 presets: Path | None = None):
        self.in_path = follows_default_sink(device)
        self._rate = rate
        self.state_path = state_path or state_file()
        self.presets = presets or presets_dir()
        self._key = None
        self.eq: TinCanEq | None = None
        self.error: str | None = None
        self.history: list[TinCanEq | None] = []  # every distinct EQ seen, in order
        self._reported: TinCanEq | None = None

    def _stamp(self, eq_ids=()) -> tuple:
        paths = [self.state_path, *(self.presets / f"{i}.toml" for i in eq_ids)]
        out = []
        for p in paths:
            try:
                st = p.stat()
                out.append((st.st_mtime_ns, st.st_size, st.st_ino))
            except OSError:
                out.append(None)
        return tuple(out)

    def current(self) -> TinCanEq | None:
        rate = self._rate() if self.in_path else None
        key = (rate, self._stamp(self.eq.ids if self.eq else ()))
        if key != self._key:
            self._key = key
            self.error = None
            eq = None
            if rate is not None and self.state_path.exists():
                try:
                    eq = read_eq(self.state_path, self.presets, rate)
                except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as e:
                    self.error = f"Can't read TinCan's state: {e}"
            if eq is not None:  # the presets' stamps too, for the next check
                self._key = (rate, self._stamp(eq.ids))
            self.eq = eq
            if not self.history or self.history[-1] != eq:
                self.history.append(eq)
        return self.eq

    def start(self) -> TinCanEq | None:
        """A new session: forget what earlier ones saw; the EQ now (what a
        notice before it shows)."""
        self.history, self._key = [], None
        self._reported = self.current()
        return self._reported

    def poll_change(self) -> tuple[bool, TinCanEq | None]:
        """Whether the EQ changed since start() or the last poll, and what it is now."""
        eq = self.current()
        if eq == self._reported:
            return False, eq
        self._reported = eq
        return True, eq

    def band_gain_db(self, freqs, stimulus: str, ear: str) -> np.ndarray:
        eq = self.current()
        return np.zeros(np.size(freqs)) if eq is None else eq.band_gain_db(freqs, stimulus, ear)

    def note(self) -> str | None:
        """For the session's EQ note: every state seen, in order; None if TinCan was never on."""
        if not any(e is not None for e in self.history):
            return None
        labels = [e.label if e is not None else "TinCan off" for e in self.history]
        return " → ".join(labels) + " (read from TinCan at every burst)"

    def context(self) -> dict:
        return {"note": self.note(), "states": [None if e is None else e.record() for e in self.history]}


def notice(eq: TinCanEq) -> str:
    return (f"TinCan is on: {' + '.join(eq.names)} ({eq.preamp_mode.replace('_', ' ')} preamp "
            f"{eq.preamp_db:+.1f} dB, {eq.rate // 1000 if eq.rate % 1000 == 0 else eq.rate / 1000} kHz).")


class SessionEq:
    """A runner's view of TinCan for one session: each burst's gain, a
    message when the EQ changes, and what the session file records."""

    def __init__(self, watch: TinCanWatch | None, stimulus: str):
        self.watch, self.stimulus = watch, stimulus
        self.at_start = watch.start() if watch is not None else None

    def gain(self, freq: float, ear: str) -> float:
        if self.watch is None or ear not in "LR" or len(ear) != 1:
            return 0.0
        eq = self.watch.current()
        return 0.0 if eq is None else round(float(eq.band_gain_db([freq], self.stimulus, ear)[0]), 4)

    def message(self) -> str | None:
        """A notice if the EQ changed since the last call (for the UI)."""
        if self.watch is None:
            return None
        changed, eq = self.watch.poll_change()
        if not changed:
            return None
        return ("TinCan is off now." if eq is None else
                f"TinCan changed: now {' + '.join(eq.names)}.") + " Its gain at each burst is recorded."

    def context(self, base_note: str | None = None) -> dict | None:
        """The session's eq_context entries (None: TinCan was never on)."""
        if self.watch is None:
            return None
        c = self.watch.context()
        if c["note"] is None:
            return None
        note = c["note"] if base_note in (None, "", "none") else f"{base_note}; {c['note']}"
        return {"note": note, "tincan": c["states"]}
