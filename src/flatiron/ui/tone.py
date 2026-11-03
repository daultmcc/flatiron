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
SPEEDS = ((1 / 48, "1/48"), (1 / 24, "1/24"), (1 / 12, "1/12"), (1 / 6, "1/6"), (1 / 3, "1/3"),
          (2 / 3, "2/3"), (4 / 3, "4/3"))  # oct/s
DEFAULT_SPEED = 3
TICK_S = 1 / 60
KEYS = ("←/→ frequency (hold to glide)   ↑/↓ glide speed   G go to   E ear   S stimulus   +/− level\n"
        "Q correction on/off   P/D mark a peak/dip   U undo mark   Space pause   Esc quit")

CSS = """
Screen { align: center middle; }
Vertical { align-horizontal: center; height: auto; }
#title { width: 100%; content-align: center middle; text-style: bold; padding: 1 0; }
#freq { width: auto; }
#state { width: 100%; content-align: center middle; padding: 1 0 0 0; }
#speed { width: 100%; content-align: center middle; color: $text-muted; }
#marks { width: 100%; content-align: center middle; padding: 1 2; color: $text-muted; }
#keys { width: 100%; content-align: center middle; padding: 1 2; }
GotoScreen { align: center middle; background: $background 70%; }
#goto { width: 40; height: auto; padding: 1 2; border: heavy $accent; background: $surface; }
"""


class Glide:
    """Arrow-key motion. Terminals don't report key releases, only the key's
    auto-repeat while it's held, so a key counts as held while repeats keep
    coming. A single press steps by speed × TAP_S; holding glides at the
    speed, speeding up gently the longer the key is held."""

    HOLD_GAP_S = 0.12  # auto-repeat runs at 25-40/s; a longer gap means the key was let go
    RUN_GAP_S = 0.7  # longer than the usual delay before auto-repeat starts
    TAP_S = 0.125
    ACCEL_AFTER_S = 0.5
    ACCEL_OVER_S = 2.0
    ACCEL_MAX = 3.0

    def __init__(self):
        self.dir = 0
        self.last = -np.inf
        self.run_start = 0.0
        self.presses = 0

    def press(self, direction: int, now: float, speed: float) -> float:
        """Octaves to move right away."""
        if direction == self.dir and now - self.last < self.RUN_GAP_S:
            self.presses += 1
            step = 0.0
        else:
            self.dir, self.run_start, self.presses = direction, now, 1
            step = direction * speed * self.TAP_S
        self.last = now
        return step

    def advance(self, now: float, dt: float, speed: float) -> float:
        """Octaves to move over the last dt seconds."""
        if self.presses < 2 or now - self.last > self.HOLD_GAP_S:
            return 0.0
        u = float(np.clip((now - self.run_start - self.ACCEL_AFTER_S) / self.ACCEL_OVER_S, 0, 1))
        return self.dir * speed * (1 + (self.ACCEL_MAX - 1) * u * u * (3 - 2 * u)) * dt


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
        Binding("up", "speed(1)", "Faster", show=False),
        Binding("down", "speed(-1)", "Slower", show=False),
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
        self.speed = DEFAULT_SPEED
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
        speed, name = SPEEDS[self.speed]
        cents = speed * Glide.TAP_S * 1200
        self.query_one("#speed", Static).update(f"Hz   ·   speed {name} oct/s   ·   a tap moves {cents:.0f} cents")
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
        step = self.glide.advance(now, dt, SPEEDS[self.speed][0])
        if step:
            self.set_freq(self.synth.freq * 2**step)
            self.show()

    def action_move(self, direction: int):
        step = self.glide.press(direction, self.clock(), SPEEDS[self.speed][0])
        if step:
            self.set_freq(self.synth.freq * 2**step)
            self.show()

    def action_speed(self, d: int):
        self.speed = int(np.clip(self.speed + d, 0, len(SPEEDS) - 1))
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
