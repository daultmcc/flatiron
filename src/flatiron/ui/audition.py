"""Audition: play a source with the correction on or off (DESIGN.md, "Audition
and verify"). Space flips between the two at the same playback position; the
preamp applies to both, so the comparison is level-matched. Blind mode hides
which is which until you reveal it."""

import random
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Static

from flatiron import FS
from flatiron.audio.limiter import db_to_amp
from flatiron.audio.preview import PreviewEQ
from flatiron.session.sweep import SweepTiming, sweep_signal

SOURCE_LEVEL_DB = -20.0  # RMS of the generated sources
PINK_S = 10.0

CSS = """
Screen { align: center middle; }
#title { width: 100%; content-align: center middle; text-style: bold; padding: 1 0; }
#state { width: 100%; margin: 0 20; height: 7; content-align: center middle; text-style: bold; border: heavy $panel-lighten-2; }
#state.on { background: $accent; color: $text; border: heavy $accent-lighten-2; }
#message { width: 100%; content-align: center middle; padding: 1 2; }
#status { width: 100%; content-align: center middle; color: $text-muted; }
Vertical { align-horizontal: center; }
"""


def pink_noise(seconds: float = PINK_S, fs: int = FS, seed: int = 0) -> np.ndarray:
    """Stereo pink noise (independent channels) that loops seamlessly."""
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    f = np.fft.rfftfreq(n, 1 / fs)
    shape = np.where(f > 20, 1 / np.sqrt(np.maximum(f, 1)), 0)
    out = np.empty((n, 2))
    for c in range(2):
        spec = (rng.standard_normal(f.size) + 1j * rng.standard_normal(f.size)) * shape
        out[:, c] = np.fft.irfft(spec, n)
    return out / np.sqrt(np.mean(out**2)) * db_to_amp(SOURCE_LEVEL_DB)


def sweep(fs: int = FS) -> np.ndarray:
    x = sweep_signal(SweepTiming(), fs) * db_to_amp(SOURCE_LEVEL_DB)
    return np.column_stack([x, x])


def read_wav(path: Path, fs: int = FS) -> np.ndarray:
    rate, x = wavfile.read(path)
    if np.issubdtype(x.dtype, np.integer):
        x = x / float(np.iinfo(x.dtype).max)
    x = np.asarray(x, float)
    if x.ndim == 1:
        x = np.column_stack([x, x])
    x = x[:, :2]
    if rate != fs:
        g = np.gcd(int(rate), fs)
        x = signal.resample_poly(x, fs // g, int(rate) // g, axis=0)
    return x


def load_source(spec: str) -> tuple[str, np.ndarray]:
    if spec == "pink":
        return "pink noise", pink_noise()
    if spec == "sweep":
        return "slow sweep (1-14 kHz, up and down)", sweep()
    p = Path(spec).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {spec}")
    if p.suffix.lower() != ".wav":
        raise ValueError(f"{spec}: only WAV files can be played (or 'pink', 'sweep')")
    return p.name, read_wav(p)


def versions(src: np.ndarray, eq: PreviewEQ) -> tuple[np.ndarray, np.ndarray]:
    """(off, on): both with the preamp. The EQ runs over two copies of the
    loop and keeps the second, so the filters are settled where it wraps."""
    g = db_to_amp(eq.preamp_db)
    on = eq.apply(np.concatenate([src, src]))[src.shape[0]:]
    return (src * g).astype(np.float32), on.astype(np.float32)


class AuditionApp(App):
    CSS = CSS
    TITLE = "Flatiron audition"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("space", "toggle", "Switch"),
        Binding("n", "next_source", "Next source"),
        Binding("b", "blind", "Blind A/B"),
        Binding("r", "reveal", "Reveal"),
        Binding("q,escape", "quit", "Quit"),
    ]

    def __init__(self, sources: list[tuple[str, np.ndarray]], eq: PreviewEQ, player, label: str = ""):
        super().__init__()
        self.sources, self.eq, self.player, self.label = sources, eq, player, label
        self.k = 0
        self.on = True
        self.blind = False
        self.a_is_on = True
        self.revealed = False
        self.buffers: tuple[np.ndarray, np.ndarray] | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="title")
            yield Static("", id="state")
            yield Static("", id="message")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self):
        self.load()

    def load(self):
        name, src = self.sources[self.k]
        self.buffers = versions(src, self.eq)
        self.player.start(self.current(), loop=True, ceiling_db=0.0)
        self.query_one("#title", Static).update(f"{self.label} · {name}")
        self.show()

    def current(self) -> np.ndarray:
        off, on = self.buffers
        return on if self.on else off

    def show(self):
        st = self.query_one("#state", Static)
        if self.blind and not self.revealed:
            which = "A" if self.on == self.a_is_on else "B"
            st.update(which)
            st.set_class(False, "on")
            msg = "Blind: Space switches between A and B. R reveals which one is corrected."
        else:
            st.update("Correction ON" if self.on else "Correction OFF")
            st.set_class(self.on, "on")
            msg = ("Same level both ways: the preamp ({:+.1f} dB) applies to both.".format(self.eq.preamp_db)
                   + (f"\nA was {'corrected' if self.a_is_on else 'uncorrected'}." if self.blind else ""))
        self.query_one("#message", Static).update(msg)
        self.query_one("#status", Static).update(f"source {self.k + 1} of {len(self.sources)}")

    def action_toggle(self):
        self.on = not self.on
        self.player.switch(self.current(), ceiling_db=0.0)
        self.show()

    def action_next_source(self):
        self.k = (self.k + 1) % len(self.sources)
        self.load()

    def action_blind(self):
        self.blind, self.revealed = True, False
        self.a_is_on = random.random() < 0.5
        self.on = random.random() < 0.5
        self.player.switch(self.current(), ceiling_db=0.0)
        self.show()

    def action_reveal(self):
        if self.blind:
            self.revealed = True
            self.show()

    def on_unmount(self):
        self.player.close()
