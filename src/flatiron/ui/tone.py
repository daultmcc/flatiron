"""Tone generator: glide through frequencies by ear, with the profile's
correction switchable, and mark peaks and dips for the next trial session's
grid (DESIGN.md, "Tone generator")."""

import time

import numpy as np
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Digits, Input, Static

from flatiron.audio.limiter import MAX_LEVEL_DB, MIN_LEVEL_DB
from flatiron.audio.synth import F_MAX, F_MIN, STIMULI, ToneSynth, stimulus_key

EAR_CYCLE = ("L", "R", "LR")
EAR_LABELS = {"L": "LEFT ear", "R": "RIGHT ear", "LR": "BOTH ears"}
TAP_SIZES = ((1 / 96, "1/96"), (1 / 48, "1/48"), (1 / 24, "1/24"), (1 / 12, "1/12"), (1 / 6, "1/6"))  # oct
DEFAULT_TAP = 2
TICK_S = 1 / 60
KEYS = ("←/→ frequency (tap faster to glide faster)   ↑/↓ tap size   G go to   E ear   S stimulus   +/− level\n"
        "Q correction on/off   P/D mark a peak/dip   U undo mark   Space pause   Esc quit")

CSS = """
Screen { align: center middle; }
Vertical { align-horizontal: center; height: auto; }
#title { width: 100%; content-align: center middle; text-style: bold; padding: 1 0; }
#freq { width: 100%; text-align: center; }
#state { width: 100%; content-align: center middle; padding: 1 0 0 0; }
#speed { width: 100%; content-align: center middle; color: $text-muted; }
#marks { width: 100%; content-align: center middle; padding: 1 2; color: $text-muted; }
#keys { width: 100%; content-align: center middle; padding: 1 2; }
GotoScreen { align: center middle; background: $background 70%; }
#goto { width: 40; height: auto; padding: 1 2; border: heavy $accent; background: $surface; }
"""


class Glide:
    """Arrow-key motion, built around tapping (terminals delay a held key's
    auto-repeat, and report no releases). Each press adds a bump to the glide
    speed: a Hann window over ±W seconds with an area of one tap size, so
    tapping steadily at r per second glides at r tap sizes per second and the
    speed follows the tap rate. A press from rest enters at the bump's peak,
    so the tone moves at once (half a tap size in all); once the tone is up to
    speed, a press enters at the bump's start, so the speed changes smoothly.
    In between, the entry point blends linearly with the speed. A press
    against the motion stops it dead and adds nothing; the next one moves. A
    held key's auto-repeat counts as presses at most MAX_RATE per second."""

    W = 0.5
    MAX_RATE = 10.0  # about the fastest steady tapping by hand

    def __init__(self):
        self.pending: list[list[float]] = []  # [time, octaves]: time counts down to -W
        self.last = -np.inf

    @classmethod
    def bump(cls, t: float) -> float:
        return (1 + np.cos(np.pi * t / cls.W)) / (2 * cls.W) if abs(t) < cls.W else 0.0

    @classmethod
    def area(cls, t: float) -> float:
        """The bump's area left of t."""
        u = float(np.clip(t / cls.W, -1, 1))
        return 0.5 * (1 + u + np.sin(np.pi * u) / np.pi)

    def speed(self) -> float:
        """Octaves per second, signed."""
        return sum(w * self.bump(t) for t, w in self.pending)

    def press(self, direction: int, now: float, size: float):
        """A press of direction ±1, with taps of size octaves."""
        if any(w * direction < 0 for _, w in self.pending):
            self.pending.clear()
            self.last = -np.inf
            return
        weight = min(1.0, (now - self.last) * self.MAX_RATE)
        self.last = now
        # Up to speed means as fast as a lone tap's peak, size / W.
        entry = self.W * min(1.0, abs(self.speed()) * self.W / size)
        self.pending.append([entry, direction * size * weight])

    def advance(self, dt: float) -> float:
        """Octaves to move over the next dt seconds."""
        step = 0.0
        for p in self.pending:
            step += p[1] * (self.area(p[0]) - self.area(p[0] - dt))
            p[0] -= dt
        self.pending = [p for p in self.pending if p[0] > -self.W]
        return step


def parse_freq(text: str) -> float:
    t = text.strip().lower().replace(",", "").removesuffix("hz").strip()
    mult = 1000.0 if t.endswith("k") else 1.0
    f = float(t.removesuffix("k")) * mult
    if not F_MIN <= f <= F_MAX:
        raise ValueError(f"pick {F_MIN:.0f} to {F_MAX:,.0f} Hz")
    return f


class GotoScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="goto"):
            yield Static("Go to frequency (e.g. 6150 or 6.15k):")
            yield Input(id="goto-input")
            yield Static("", id="goto-error")

    def on_input_submitted(self, event: Input.Submitted):
        try:
            self.dismiss(parse_freq(event.value))
        except ValueError as e:
            self.query_one("#goto-error", Static).update(str(e))

    def action_cancel(self):
        self.dismiss(None)


class ToneApp(App):
    CSS = CSS
    TITLE = "Flatiron tone generator"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("left", "move(-1)", "Lower", show=False),
        Binding("right", "move(1)", "Higher", show=False),
        Binding("up", "tap_size(1)", "Bigger taps", show=False),
        Binding("down", "tap_size(-1)", "Smaller taps", show=False),
        Binding("e", "ear", "Ear", show=False),
        Binding("s", "stimulus", "Stimulus", show=False),
        Binding("plus,equals_sign", "level(1)", "Louder", show=False),
        Binding("minus,underscore", "level(-1)", "Quieter", show=False),
        Binding("q", "eq", "Correction", show=False),
        Binding("p", "mark('peak')", "Peak", show=False),
        Binding("d", "mark('dip')", "Dip", show=False),
        Binding("u", "unmark", "Undo mark", show=False),
        Binding("g", "goto", "Go to", show=False),
        Binding("space", "pause", "Pause", show=False),
        Binding("escape", "quit", "Quit", show=False),
    ]

    def __init__(self, freq: float = 1000.0, ear: str = "L", level_db: float = -30.0, eq=None, profile=None,
                 device: int | None = None, dry_run: bool = False, clock=time.monotonic):
        super().__init__()
        self.synth = ToneSynth(freq=float(np.clip(freq, F_MIN, F_MAX)), level_db=level_db, ear=ear, eq=eq)
        self.profile = profile
        self.device, self.dry_run = device, dry_run
        self.clock = clock
        self.glide = Glide()
        self.tap = DEFAULT_TAP
        self.marks: list[dict] = []  # made in this run
        self.note = ""
        self.player = None
        self.last_tick = 0.0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="title")
            yield Digits("", id="freq")
            yield Static("", id="state")
            yield Static("", id="speed")
            yield Static("", id="marks")
            yield Static(KEYS, id="keys")

    def on_mount(self):
        from flatiron.audio.player import NullPlayer, StreamPlayer

        self.player = NullPlayer() if self.dry_run else StreamPlayer(self.device, source=self.synth)
        label = self.profile.read()["label"] if self.profile is not None else "no profile"
        self.query_one("#title", Static).update(f"Tone generator · {label}")
        self.last_tick = self.clock()
        self.set_interval(TICK_S, self.tick)
        self.show()

    def on_unmount(self):
        if self.player is not None:
            self.player.close()

    # -- display ---------------------------------------------------------------

    def show(self):
        s = self.synth
        self.query_one("#freq", Digits).update(f"{s.freq:,.0f}" if s.freq >= 100 else f"{s.freq:.1f}")
        eq = ("correction ON" if s.eq_on else "correction OFF") if s.eq is not None else "no correction"
        state = (f"{EAR_LABELS[s.ear]}  ·  {STIMULI[s.stimulus][1]}  ·  {s.level_db:+.0f} dBFS  ·  {eq}"
                 + ("  ·  PAUSED" if s.paused else ""))
        self.query_one("#state", Static).update(state)
        size, name = TAP_SIZES[self.tap]
        self.query_one("#speed", Static).update(
            f"Hz   ·   tap size {name} oct   ·   one tap moves {size * 600:.0f} cents   ·   "
            f"tapping 5/s glides {5 * size:.2g} oct/s")
        lines = [f"{'▲' if m['kind'] == 'peak' else '▼'} {m['freq']:,.0f} Hz {EAR_LABELS[m['ear']].split()[0].lower()}"
                 for m in self.marks[-6:]]
        marks = ("Marks: " + "   ".join(lines)) if lines else (
            "P / D mark a peak / dip here; the next trial session samples more finely around it."
            if self.profile is not None else "Start with -p PROFILE to apply its correction and save marks.")
        self.query_one("#marks", Static).update(marks + (f"\n{self.note}" if self.note else ""))

    def set_freq(self, f: float):
        self.synth.freq = float(np.clip(f, F_MIN, F_MAX))

    # -- actions ---------------------------------------------------------------

    def tick(self):
        now = self.clock()
        dt, self.last_tick = now - self.last_tick, now
        step = self.glide.advance(dt)
        if step:
            self.set_freq(self.synth.freq * 2**step)
            self.show()

    def action_move(self, direction: int):
        self.glide.press(direction, self.clock(), TAP_SIZES[self.tap][0])

    def action_tap_size(self, d: int):
        self.tap = int(np.clip(self.tap + d, 0, len(TAP_SIZES) - 1))
        self.show()

    def action_ear(self):
        self.synth.ear = EAR_CYCLE[(EAR_CYCLE.index(self.synth.ear) + 1) % len(EAR_CYCLE)]
        self.show()

    def action_stimulus(self):
        self.synth.stimulus = (self.synth.stimulus + 1) % len(STIMULI)
        self.show()

    def action_level(self, d: int):
        new = float(np.clip(self.synth.level_db + d, MIN_LEVEL_DB, MAX_LEVEL_DB))
        self.note = (f"{MAX_LEVEL_DB:+.0f} dBFS is the most Flatiron plays." if d > 0 and new == self.synth.level_db
                     else "")
        self.synth.level_db = new
        self.show()

    def action_eq(self):
        if self.synth.eq is None:
            self.note = "No correction to switch: start with -p PROFILE (and enough trials for a fit)."
        else:
            self.synth.eq_on = not self.synth.eq_on
            self.note = ""
        self.show()

    def action_pause(self):
        self.synth.paused = not self.synth.paused
        self.show()

    def action_mark(self, kind: str):
        if self.profile is None:
            self.note = "Marks are saved to a profile: start with -p PROFILE."
        else:
            m = {"ear": self.synth.ear, "freq": round(self.synth.freq, 1), "kind": kind}
            self.profile.add_mark(m["ear"], m["freq"], kind, stimulus_key(self.synth.stimulus))
            self.marks.append(m)
            self.note = ""
        self.show()

    def action_unmark(self):
        if self.marks and self.profile is not None:
            self.profile.remove_last_mark()
            self.marks.pop()
        self.show()

    def action_goto(self):
        def done(f):
            if f is not None:
                self.set_freq(f)
                self.show()

        self.push_screen(GotoScreen(), done)
