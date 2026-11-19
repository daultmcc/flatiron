"""Keyboard-driven trial UI (DESIGN.md, "Session flow").

Flow per session: channel check → optional sweep (first session only) →
trials until the time limit or Q → end screen (reseat, then next session).
Speaker blocks: the level once, then per block the earplug screen (when the
open ear changes) and the position screen
→ the block's trials; an end screen only on Q or once the plan is done.
Threshold sessions: an introduction → channel check → detection trials →
end screen. The fitted curve is never shown during trials.
"""

import random
import time
from typing import Callable

import numpy as np
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Digits, Footer, Static

from flatiron import EARS, FS
from flatiron.audio.limiter import MAX_LEVEL_DB, MIN_LEVEL_DB, db_to_amp, limit, monaural
from flatiron.audio.stimulus import StimulusSpec, make_stimulus
from flatiron.session.runner import PreparedTrial, TrialRunner
from flatiron.session.sweep import SweepTiming, pair_taps, sweep_signal

ITI_S = 0.5  # pause between an answer and the next trial


def khz(f: float) -> str:
    return f"{f / 1000:.3g}"
EAR_NAMES = {"L": "LEFT", "R": "RIGHT"}

CSS = """
Screen { align: center middle; }
#title { width: 100%; content-align: center middle; text-style: bold; padding: 1 0; }
#message { width: 100%; content-align: center middle; padding: 1 2; }
#status { width: 100%; content-align: center middle; color: $text-muted; padding: 1 0; }
#debug { width: 100%; content-align: center middle; color: $warning; }
#panels { height: 17; width: 100%; align: center middle; }
#panels .panel { width: 44; height: 1fr; margin: 0 0 1 0; }
.panel {
    width: 1fr; height: 100%; margin: 0 2; border: heavy $panel-lighten-2;
    content-align: center middle; text-style: bold;
}
#plug-panels { height: 9; width: 100%; padding: 0 6; }
.panel.plugged { background: $warning 30%; border: heavy $warning; }
.panel.lit { background: $accent; color: $text; border: heavy $accent-lighten-2; }
.panel.answerable { border: heavy $success; }
#where { height: auto; width: 100%; align: center middle; }
.where { width: auto; height: auto; margin: 0 4; }
.where Digits { width: auto; }
.where Static { width: auto; color: $text-muted; }
ConfirmScreen { align: center middle; background: $background 70%; }
#confirm { width: 60; height: auto; padding: 1 2; border: heavy $warning; background: $surface; }
"""


class MessageScreen(Screen):
    """Title, message and status lines; subclasses add behaviour."""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="title")
            yield Static("", id="message")
            yield Static("", id="status")
        yield Footer()

    def say(self, title: str | None = None, message: str | None = None, status: str | None = None):
        if title is not None:
            self.query_one("#title", Static).update(title)
        if message is not None:
            self.query_one("#message", Static).update(message)
        if status is not None:
            self.query_one("#status", Static).update(status)


class LevelScreen(MessageScreen):
    """Set the presentation level with the system volume left where it normally is.

    Shown at the start of every session (starting from the last session's
    level) and on demand mid-session. Raising the system volume to suit quiet
    test tones leaves it dangerously high for everything played afterwards, so
    Flatiron moves its own level instead. Dismisses with the chosen level, or
    None if cancelled.
    """

    BINDINGS = [
        Binding("up", "step(1)", "+1 dB"),
        Binding("down", "step(-1)", "−1 dB"),
        Binding("pageup", "step(5)", "+5 dB", show=False),
        Binding("pagedown", "step(-5)", "−5 dB", show=False),
        Binding("enter", "accept", "Continue"),
        Binding("escape", "cancel", "Cancel"),
    ]

    # Bursts like the trials': one ear at a time, across the measured range,
    # at the levels loudness matching would give them. A single 4 kHz tone in
    # both ears misleads: both ears sound louder than one, and 4 kHz is where
    # hearing is most sensitive.
    FREQS = (1000, 2000, 3000, 4000, 6000, 8000, 11000, 14000)
    GAP_S = 0.3

    def __init__(self, start_db: float, mid_session: bool = False,
                 measured: tuple[float, float] | None = None,
                 gain: Callable[[str, float], float] = lambda ear, f: 0.0):
        super().__init__()
        self.start = float(np.clip(start_db, MIN_LEVEL_DB, MAX_LEVEL_DB))
        self.level = self.start
        self.mid_session = mid_session
        self.freqs = self.FREQS if measured is None else level_freqs(*measured)
        self.gain = gain
        self.note = ""

    def on_mount(self):
        self.play()

    def sequence(self) -> np.ndarray:
        gap = np.zeros((int(self.GAP_S * FS), 2), np.float32)
        parts = []
        for i, f in enumerate(self.freqs):
            ear = "LR"[i % 2]
            x = make_stimulus(float(f), StimulusSpec(), seed=0) * db_to_amp(self.level + self.gain(ear, float(f)))
            parts += [monaural(limit(x).signal, ear), gap]
        return np.concatenate(parts)

    def play(self):
        self.app.player.start(self.sequence(), loop=True)
        warning = ("Changing the level mid-session shifts the results slightly: only if you need to.\n\n"
                   if self.mid_session else "")
        cancel = "Esc: back, no change" if self.mid_session else "Esc: quit"
        self.say("Listening level",
                 warning +
                 "Set the loudest burst to about the level of quiet conversation.\n\n"
                 f"Level: {self.level:+.0f} dBFS   ({'current' if self.mid_session else 'last used'} "
                 f"{self.start:+.0f})"
                 + (f"\n\n{self.note}" if self.note else ""),
                 f"↑/↓: 1 dB   PgUp/PgDn: 5 dB   Enter: continue   {cancel}")

    def action_step(self, d: int):
        new = float(np.clip(self.level + d, MIN_LEVEL_DB, MAX_LEVEL_DB))
        self.note = ""
        if new == self.level and d > 0:
            self.note = ("That's the most Flatiron will play. If it's still too quiet, raise the system "
                         "volume a little (and lower it afterwards).")
        self.level = new
        self.play()

    def action_accept(self):
        self.app.player.stop()
        self.dismiss(self.level)

    def action_cancel(self):
        self.app.player.stop()
        self.dismiss(None)


def level_freqs(lo: float, hi: float, n: int = 8) -> list[float]:
    """The level screen's bursts: n across the measured range, from 1 to 14 kHz
    at most (the range's ends are rarely where the level matters)."""
    a, b = max(lo, 1000.0), min(hi, 14_000.0)
    if b / a < 2:
        a, b = lo, hi
    return [float(round(f, -1)) for f in np.geomspace(a, b, n)]


class ConfirmScreen(ModalScreen):
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        yield Static(self.question + "\n\nY: yes   N: no", id="confirm")

    def action_answer(self, yes: bool):
        self.dismiss(yes)


class ChannelCheckScreen(MessageScreen):
    """Play a burst in each ear in random order; the listener names the side."""

    BINDINGS = [
        Binding("left,l", "heard('L')", "Left"),
        Binding("right,r", "heard('R')", "Right"),
        Binding("space", "replay", "Replay"),
        Binding("enter", "retry", "Retry", show=False),
        Binding("escape", "app.quit", "Quit"),
    ]

    def __init__(self, runner: TrialRunner):
        super().__init__()
        self.runner = runner
        self.order: list[str] = []
        self.failed = False

    def on_mount(self):
        self.start()

    def start(self):
        self.order = random.sample(list(EARS), 2)
        self.failed = False
        self.say("Channel check", "", "")
        self.play_current()

    def play_current(self):
        ear = self.order[0]
        x = make_stimulus(1000.0, StimulusSpec(), seed=0)
        burst = limit(np.concatenate([x, np.zeros(int(0.25 * FS)), x]) * db_to_amp(self.runner.level_dbfs)).signal
        self.app.player.start(monaural(burst, ear))
        self.say(message=f"Check {3 - len(self.order)} of 2: which side did you hear?\n\n← left      → right",
                 status="Space replays.")

    def action_replay(self):
        if self.order and not self.failed:
            self.play_current()

    def action_heard(self, side: str):
        if not self.order or self.failed:
            return
        played = self.order.pop(0)
        if not self.runner.record_channel_check(played, side):
            self.failed = True
            self.say(message=f"That was the {EAR_NAMES[played].lower()} ear, not the {EAR_NAMES[side].lower()}."
                             "\n\nCheck the headphones are on the right way round, then press Enter.",
                     status="Enter: retry   Esc: quit")
            return
        if self.order:
            self.play_current()
        else:
            self.dismiss(True)

    def action_retry(self):
        if self.failed:
            self.start()


class EarplugScreen(MessageScreen):
    """Before a speaker block whose open ear differs from the last one's (and
    before a run's first block): only the earplug, so a switch isn't missed
    among the position's details."""

    BINDINGS = [
        Binding("enter", "ready", "Earplug in"),
        Binding("escape", "app.quit", "Quit"),
    ]

    def __init__(self, ear: str, first: bool, last: str = ""):
        super().__init__()
        self.ear, self.first, self.last = ear, first, last

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="title")
            yield Static("", id="message")
            with Horizontal(id="plug-panels"):
                for e in EARS:
                    yield Static(f"{EAR_NAMES[e]} EAR\n\n" + ("open" if e == self.ear else "EARPLUG"),
                                 id=f"plug-{e}", classes="panel" + (" plugged" if e != self.ear else ""))
            yield Static("", id="status")
        yield Footer()

    def on_mount(self):
        other = EAR_NAMES["R" if self.ear == "L" else "L"].lower()
        head = "Earplug" if self.first else "Switch the earplug"
        what = (f"Put a foam earplug fully into your {other} ear." if self.first else
                f"Take the earplug out of your {EAR_NAMES[self.ear].lower()} ear and put it (or a fresh one) "
                f"fully into your {other} ear.")
        last = f"{self.last}\n\n" if self.last else ""
        self.say(head, last + what, "Enter when the earplug is in   Esc: quit")

    def action_ready(self):
        self.dismiss(True)


class BlockScreen(MessageScreen):
    """A speaker block's setup: the position (after EarplugScreen when the
    open ear changes). A plugged ear can't be checked by listening, so these
    are confirmations."""

    BINDINGS = [
        Binding("enter", "ready", "In position"),
        Binding("escape", "app.quit", "Quit"),
    ]

    def __init__(self, runner: TrialRunner, done: int, total: int, last: str = ""):
        super().__init__()
        self.runner, self.done, self.total, self.last = runner, done, total, last
        from flatiron.session.speaker import Position

        info = runner.speaker
        self.pos = Position(info["open_ear"], info["position"]["azimuth"], info["position"]["elevation"])

    def compose(self) -> ComposeResult:
        # The position in large digits: with the order shuffled, it's the thing to check.
        pos = self.pos
        side = EAR_NAMES[pos.ear].lower()
        across = "straight ahead" if pos.azimuth == 0 else \
            f"degrees to your {side}" + (" (behind you)" if pos.azimuth > 90 else "")
        height = "at ear level" if pos.elevation == 0 else \
            f"degrees {'above' if pos.elevation > 0 else 'below'} ear level"
        with Vertical():
            yield Static("", id="title")
            with Horizontal(id="where"):
                with Vertical(classes="where"):
                    yield Digits(str(pos.azimuth), id="azimuth")
                    yield Static(across)
                with Vertical(classes="where"):
                    yield Digits(str(abs(pos.elevation)), id="elevation")
                    yield Static(height)
            yield Static("", id="message")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self):
        info = self.runner.speaker
        pos = self.pos
        last = f"{self.last}\n\n" if self.last else ""
        self.say(f"Block {self.done + 1} of {self.total} · {EAR_NAMES[pos.ear].lower()} ear · {pos.label}",
                 last + pos.instructions() + f"\n\n{info['block']} trials.",
                 "Enter when you're in position   Esc: quit")

    def action_ready(self):
        self.dismiss(True)


class SweepScreen(MessageScreen):
    """Phase 0: slow sweep per ear; tap at peaks and dips.

    With `eq` (the profile's current correction), the sweep plays through it,
    so what stands out is what the correction hasn't fixed; Q toggles it.
    """

    BINDINGS = [
        Binding("enter", "begin", "Start"),
        Binding("up,p", "tap('peak')", "Peak (louder)"),
        Binding("down,d", "tap('dip')", "Dip (quieter)"),
        Binding("q", "toggle_eq", "Correction on/off"),
        Binding("escape", "skip", "Skip sweep"),
    ]

    def __init__(self, runner: TrialRunner, timing: SweepTiming = SweepTiming(), eq=None):
        super().__init__()
        self.runner = runner
        self.timing = timing
        self.eq = eq
        self.eq_on = eq is not None
        self.buffers: dict[bool, np.ndarray] = {}
        self.ears = list(EARS)
        self.playing = False
        self.t0 = 0.0
        self.taps: list[tuple[float, str]] = []
        self.timer = None
        self.results: dict[str, int] = {}

    def on_mount(self):
        if self.eq is not None:
            self.runner.set_sweep_eq(self.eq.fit_hash)
        self.intro()

    def eq_state(self) -> str:
        if self.eq is None:
            return ""
        return f"Correction {'ON' if self.eq_on else 'OFF'} (Q switches)"

    def intro(self):
        ear = self.ears[0]
        eq = "\nYour current correction is on (Q toggles it)." if self.eq is not None else ""
        self.say(f"Sweep: {EAR_NAMES[ear]} ear",
                 f"A slow sweep from {khz(self.timing.f_lo)} to {khz(self.timing.f_hi)} kHz and back.\n"
                 "Tap ↑ (P) where it gets suddenly louder, ↓ (D) where it gets quieter, on both passes." + eq,
                 "Enter: start   Esc: skip the sweep")

    def render_buffers(self, ear: str) -> dict[bool, np.ndarray]:
        """Plain and corrected versions, scaled alike (switching between them
        changes only the EQ). The correction's preamp isn't applied: the level
        screen set the level for the uncorrected sound."""
        x = sweep_signal(self.timing, self.runner.fs, seed=self.runner.seed) * db_to_amp(self.runner.level_dbfs)
        versions = {False: x}
        if self.eq is not None:
            versions[True] = self.eq.filter(x, ear, self.runner.fs)
        gain = min(limit(v).gain_db for v in versions.values())
        return {k: monaural(limit(v * db_to_amp(gain)).signal, ear) for k, v in versions.items()}

    def action_toggle_eq(self):
        if self.eq is None:
            return
        self.eq_on = not self.eq_on
        if self.playing:
            self.app.player.switch(self.buffers[self.eq_on])
        else:
            self.say(status=self.eq_state())

    def action_begin(self):
        if self.playing or not self.ears:
            return
        ear = self.ears[0]
        self.buffers = self.render_buffers(ear)
        self.app.player.start(self.buffers[self.eq_on])
        self.playing, self.taps = True, []
        self.t0 = time.monotonic() + self.app.player.latency
        self.timer = self.set_interval(0.25, self.tick)

    def tick(self):
        t = time.monotonic() - self.t0
        if t >= self.timing.total:
            self.finish_ear()
            return
        where = self.timing.freq_at(t)
        arrow = "▲ going up" if where and where[1] == "up" else "▼ coming down" if where else "…"
        self.say(message=f"{arrow}\n\n{int(t)} s of {int(self.timing.total)} s",
                 status=f"{len(self.taps)} taps" + (f"   ·   {self.eq_state()}" if self.eq is not None else ""))

    def action_tap(self, kind: str):
        if self.playing:
            self.taps.append((time.monotonic() - self.t0, kind))

    def finish_ear(self):
        self.timer.stop()
        self.playing = False
        self.app.player.stop()
        ear = self.ears.pop(0)
        marks, unmatched = pair_taps(self.taps, self.timing)
        added = self.runner.apply_sweep_marks(ear, marks)
        self.results[ear] = len(marks)
        note = f" ({unmatched} taps had no partner on the other pass)" if unmatched else ""
        if self.ears:
            self.intro()
            self.say(status=f"{EAR_NAMES[ear].capitalize()} ear: {len(marks)} marks{note}, "
                            f"{added} grid points added.   Enter: start   Esc: skip")
        else:
            self.dismiss(self.results)

    def action_skip(self):
        if self.playing:
            self.timer.stop()
            self.app.player.stop()
        self.dismiss(self.results)


class TrialScreen(Screen):
    PANELS = ("A", "B")  # panel ids: the first and second interval's
    QUESTION = "Which was louder?"
    BINDINGS = [
        # Up and down, not left and right: a cross-ear trial whose first
        # burst is in the right ear would otherwise put "first" on the wrong side.
        Binding("up,1", "answer(True)", "A louder"),
        Binding("down,2", "answer(False)", "B louder"),
        Binding("space", "replay", "Replay"),
        Binding("p", "pause", "Pause"),
        Binding("u,backspace", "undo", "Undo"),
        Binding("l", "level", "Level"),
        Binding("q", "quit_session", "Quit"),
        Binding("x", "discard", "Discard"),
        Binding("d", "debug", "Debug"),  # only with run --debug or threshold --debug
    ]

    def __init__(self, runner: TrialRunner, clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self.runner = runner
        self.clock = clock
        self.show_debug = False
        self.trial: PreparedTrial | None = None
        self.accepting = False
        self.paused = False
        self.busy = False
        self.replays = 0
        self.play_start = 0.0
        # Reaction time counts from the end of B in the first presentation
        # that reached B; replays don't reset it (answering soon after a
        # replay is an informed answer, not a careless one).
        self.heard_b = False
        self.first_end: float | None = None
        self.last_end = 0.0
        self.timers = []
        self.active_s = 0.0
        self.active_since: float | None = None
        self.started = clock()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.QUESTION, id="title")
            with Vertical(id="panels"):  # stacked: the first interval on top (↑), the second below (↓)
                for k in dict.fromkeys(self.PANELS):
                    yield Static(self.panel_label(k), id=k, classes="panel")
            yield Static("", id="message")
            yield Static("", id="debug")
            yield Static("", id="status")
        yield Footer()

    def check_action(self, action: str, parameters) -> bool | None:
        if action == "debug" and not self.runner.settings.debug:
            return False  # hidden from the footer, and the key does nothing
        return True

    def action_debug(self):
        self.show_debug = not self.show_debug
        self.show_readout()

    def show_readout(self):
        """Debug: what the current pair was played at. Answers given while it
        shows are marked `peeked` in the session file."""
        w = self.query_one("#debug", Static)
        tr = self.trial
        if not self.show_debug or tr is None:
            w.update("")
            return
        (f1, f2), (l1, l2) = tr.freqs, tr.levels
        cut = f" · limiter {tr.limited_db:+.1f} dB" if tr.limited_db < 0 else ""
        catch = " · catch trial" if tr.plan.catch else ""
        w.update(f"A {f1:,.0f} Hz at {l1:+.1f} dBFS   ·   B {f2:,.0f} Hz at {l2:+.1f} dBFS   "
                 f"(A − B {l1 - l2:+.1f} dB · level {tr.level_dbfs:+.0f}{cut}{catch})")

    def panel_label(self, key: str) -> str:
        return key

    def answer_kwargs(self, first: bool) -> dict:
        """The runner's answer for the left-hand key (first) or the right-hand one."""
        return {"first_louder": first, "peeked": self.show_debug}

    def on_mount(self):
        self.active_since = self.clock()
        self.set_interval(1.0, self.show_status)
        self.fetch_next(None)

    # -- timing ------------------------------------------------------------

    def active_seconds(self) -> float:
        extra = self.clock() - self.active_since if self.active_since is not None else 0.0
        return self.active_s + extra

    @property
    def limit_s(self) -> float:
        return self.runner.settings.session_minutes * 60

    def show_status(self):
        p = self.runner.progress()
        t, lim = int(self.active_seconds()), int(self.limit_s)
        if self.runner.speaker:  # the scheduler's own blocks would read as the speaker block's end
            info = self.runner.speaker
            self.query_one("#status", Static).update(
                f"Trial {min(p['session_trials'] + 1, info['block'])} of {info['block']} this block · "
                f"{EAR_NAMES[info['open_ear']].lower()} ear open · level {self.runner.level_dbfs:+.0f} dBFS")
            return
        tot = " / ".join(f"{e} {p['total'][e]}" for e in EARS)
        self.query_one("#status", Static).update(
            f"Trial {p['session_trials'] + 1} this session · totals {tot} · "
            f"{p['block_left']} to next block · {t // 60:02d}:{t % 60:02d} of {lim // 60:02d}:{lim % 60:02d} · "
            f"level {self.runner.level_dbfs:+.0f} dBFS")

    # -- playback ----------------------------------------------------------

    def light(self, which: str | None, answerable: bool = False):
        for k in dict.fromkeys(self.PANELS):
            w = self.query_one(f"#{k}", Static)
            w.set_class(k == which, "lit")
            w.set_class(answerable, "answerable")

    def cancel_timers(self):
        for t in self.timers:
            t.stop()
        self.timers = []

    def play(self):
        tr = self.trial
        self.cancel_timers()
        self.accepting = self.heard_b
        self.light(None, answerable=self.heard_b)
        self.app.player.start(tr.buffer)
        lat = self.app.player.latency
        self.play_start = self.clock() + lat
        self.last_end = self.play_start + tr.second_off
        at = lambda s, fn: self.timers.append(self.set_timer(max(s + lat, 0.001), fn))
        at(tr.first_on, lambda: self.light(self.PANELS[0], answerable=self.heard_b))
        at(tr.first_off, lambda: self.light(None, answerable=self.heard_b))
        at(tr.second_on, self.second_on)
        at(tr.second_off, lambda: self.light(None, answerable=True))

    def second_on(self):
        if not self.heard_b:
            self.heard_b = True
            self.first_end = self.last_end
        self.light(self.PANELS[-1], answerable=True)
        self.accepting = True

    # -- trial loop --------------------------------------------------------

    def fetch_next(self, answer: tuple | None):
        """Record an answer (if any) and prepare the next trial off the UI thread."""
        self.busy = True

        def work():
            if answer is not None:
                self.runner.answer(**answer)
            nxt = self.runner.prepare()
            eq_message = getattr(self.runner, "eq_message", None)  # TinCan changed (DESIGN.md, "Measurement context")
            if eq_message is not None and (note := eq_message()):
                self.app.call_from_thread(self.app.notify, note, severity="warning", timeout=10)
            self.app.call_from_thread(self.ready, nxt)

        self.run_worker(work, thread=True, exclusive=True, group="trial")
        self.set_timer(0.7, self.show_busy)

    def show_busy(self):
        # Block-end refits (noise fit, grid refinement) take a few seconds.
        if self.busy:
            self.query_one("#message", Static).update("Updating the model…")

    def ready(self, nxt: PreparedTrial, message: str = ""):
        self.busy = False
        self.show_status()
        if self.active_seconds() >= self.limit_s and not nxt.retake:
            self.dismiss("time")
            return
        cap = self.runner.settings.max_trials
        if cap and self.runner.progress()["session_trials"] >= cap and not nxt.retake:
            self.dismiss("done")
            return
        if nxt.level_dbfs != self.runner.level_dbfs:  # level changed while it was being prepared
            nxt = self.runner.render(nxt.plan, retake=nxt.retake)
        self.trial, self.replays = nxt, 0
        self.heard_b, self.first_end = False, None
        self.query_one("#message", Static).update(message)
        self.show_readout()
        if not self.paused:
            self.timers.append(self.set_timer(ITI_S, self.play))

    def action_undo(self):
        if self.paused or self.busy:
            return
        if not self.runner.can_undo():
            self.query_one("#message", Static).update("Nothing to undo (only the last answer can be undone).")
            return
        self.app.player.stop()
        self.cancel_timers()
        self.accepting = False
        self.light(None)
        self.busy = True

        def work():
            tr = self.runner.undo_last()
            self.app.call_from_thread(self.ready, tr, "Last answer undone. Here's that pair again.")

        self.run_worker(work, thread=True, exclusive=True, group="trial")

    def action_level(self):
        if not self.paused:
            self.action_pause()

        def done(level):
            if level is not None and level != self.runner.level_dbfs:
                self.runner.set_level(level)
                if self.trial is not None and not self.busy:
                    self.trial = self.runner.render(self.trial.plan, retake=self.trial.retake)
                    self.show_readout()
            self.action_pause()  # resume: the current pair replays
            self.show_status()

        self.app.push_screen(self.app.level_screen(mid_session=True), done)

    def action_answer(self, first_louder: bool):
        if not self.accepting or self.paused or self.busy:
            return
        self.accepting = False
        now = self.clock()
        self.app.player.stop()  # fades out if still playing
        self.cancel_timers()
        self.light(None)
        self.fetch_next(dict(trial=self.trial, **self.answer_kwargs(first_louder),
                             rt_ms=(now - self.first_end) * 1000, rt_last_ms=(now - self.last_end) * 1000,
                             replays=self.replays, t=now - self.started))

    def on_click(self, event: events.Click):
        """Clicking A or B answers, as ↑ / ↓ do."""
        if event.widget is not None and event.widget.id in ("A", "B"):
            self.action_answer(event.widget.id == "A")

    def action_replay(self):
        if self.trial is None or self.paused or self.busy:
            return
        self.replays += 1
        self.play()

    def action_pause(self):
        if not self.paused:
            self.paused = True
            self.active_s += self.clock() - self.active_since
            self.active_since = None
            self.app.player.stop()
            self.cancel_timers()
            self.accepting = False
            self.light(None)
            self.query_one("#message", Static).update("Paused. Press P to resume (the trial replays).")
        else:
            self.paused = False
            self.active_since = self.clock()
            self.query_one("#message", Static).update("")
            if self.trial is not None and not self.busy:
                self.play()

    def action_quit_session(self):
        self.app.player.stop()
        self.cancel_timers()
        self.dismiss("quit")

    def action_discard(self):
        if not self.paused:
            self.action_pause()
        n = len(self.runner.session.trials)

        def done(yes):
            if yes:
                self.runner.discard()
                self.cancel_timers()
                self.dismiss("discarded")

        self.app.push_screen(ConfirmScreen(
            f"Discard this session? Its {n} answers will be deleted and nothing from it "
            "will be used. Earlier sessions are kept."), done)


class TinCanNoticeScreen(MessageScreen):
    """Before a session, when TinCan is applying an EQ (DESIGN.md, "Measurement context").
    Rechecked every second, so bypassing it in TinCan shows here straight away."""

    BINDINGS = [Binding("enter", "start", "Start"), Binding("escape", "cancel", "Quit")]

    def __init__(self, watch):
        super().__init__()
        self.watch = watch

    def on_mount(self):
        self.refresh_state()
        self.set_interval(1.0, self.refresh_state)

    def refresh_state(self):
        from flatiron.tincan_live import notice

        eq = self.watch.current()
        if eq is None:
            self.say("TinCan", "TinCan is off now (pass-through, or nothing active).", "Enter: start   Esc: quit")
            return
        self.say("TinCan is on",
                 notice(eq) + "\n\nFlatiron records its gain at every burst, so the session still counts. "
                 "To run without it, put TinCan in pass-through now; it's checked again at every burst.",
                 "Enter: start   Esc: quit")

    def action_start(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(None)


class ThresholdIntroScreen(MessageScreen):
    """Before a threshold session: what to do, and to keep the volume where it is."""

    BINDINGS = [Binding("enter", "start", "Start"), Binding("escape", "cancel", "Quit")]

    def on_mount(self):
        how = ("The sound is in one of two marked intervals: ↑ the first, ↓ the second, "
               "N if you heard nothing in either. If you heard something but aren't "
               "sure where, pick the likelier one.")
        self.say("Threshold session",
                 "Very quiet bursts, one ear at a time, at the edge of hearing.\n\n"
                 "A quiet room, the system volume at its usual setting, and don't change it during "
                 "the session.\n\n" + how,
                 "Enter: start   Esc: quit")

    def action_start(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(None)


class ThresholdScreen(TrialScreen, inherit_bindings=False):
    """Detection trials (DESIGN.md, "Threshold sessions"): two marked
    intervals; ↑ the first, ↓ the second, N neither."""

    PANELS = ("A", "B")
    QUESTION = "Which interval had the sound? (N: neither)"
    BINDINGS = [
        Binding("up,1", "answer(True)", "First"),  # as in loudness trials
        Binding("down,2", "answer(False)", "Second"),
        Binding("n,0", "answer(None)", "Neither"),
        Binding("space", "replay", "Replay"),
        Binding("p", "pause", "Pause"),
        Binding("u,backspace", "undo", "Undo"),
        Binding("q", "quit_session", "Quit"),
        Binding("x", "discard", "Discard"),
        Binding("d", "debug", "Debug"),  # only with --debug
    ]

    def panel_label(self, key: str) -> str:
        return {"A": "1", "B": "2"}[key]

    def answer_kwargs(self, first: bool) -> dict:
        return {"first": first, "peeked": self.show_debug}

    def show_readout(self):
        """Debug: the burst's ear, frequency, level, kind and the threshold
        estimate. Answers given while it shows are marked `peeked`."""
        w = self.query_one("#debug", Static)
        tr = self.trial
        if not self.show_debug or tr is None:
            w.update("")
            return
        p = tr.plan
        w.update(f"{p.ear} · {p.freq:,.0f} Hz at {tr.played_db:+.1f} dBFS in interval {p.interval} · {p.kind}   "
                 f"(estimate {tr.estimate_db:+.1f} ± {tr.estimate_sd:.1f} dBFS)")

    def show_status(self):
        p = self.runner.progress()
        t, lim = int(self.active_seconds()), int(self.limit_s)
        start = " · finding the level" if p["starting"] else ""
        self.query_one("#status", Static).update(
            f"Trial {p['session_trials'] + 1} this session · "
            + " / ".join(f"{e} {p['session_trials_ear'][e]}" for e in EARS)
            + f"{start} · {t // 60:02d}:{t % 60:02d} of {lim // 60:02d}:{lim % 60:02d}")


class CentringIntroScreen(MessageScreen):
    """Before a centring session (DESIGN.md, "Centring sessions")."""

    BINDINGS = [Binding("enter", "start", "Start"), Binding("escape", "cancel", "Quit")]

    def on_mount(self):
        self.say("Centring session",
                 "Each burst plays in both ears at once. Say where it sounds: ← left of centre, "
                 "→ right of centre. If it seems centred, pick the side it leans to, or guess.",
                 "Enter: start   Esc: quit")

    def action_start(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(None)


class CentringScreen(TrialScreen, inherit_bindings=False):
    """Centring trials: one burst in both ears; ← left of centre, → right.
    Left and right keys are the point here, unlike loudness trials."""

    PANELS = ("listen", "listen")
    QUESTION = "Left or right of centre?"
    BINDINGS = [
        Binding("left,l,1", "answer(True)", "Left"),
        Binding("right,r,2", "answer(False)", "Right"),
        Binding("space", "replay", "Replay"),
        Binding("p", "pause", "Pause"),
        Binding("u,backspace", "undo", "Undo"),
        Binding("q", "quit_session", "Quit"),
        Binding("x", "discard", "Discard"),
        Binding("d", "debug", "Debug"),  # only with centre --debug
    ]

    def panel_label(self, key: str) -> str:
        return "LISTEN"

    def answer_kwargs(self, first: bool) -> dict:
        return {"left": first, "peeked": self.show_debug}

    def show_status(self):
        p = self.runner.progress()
        t, lim = int(self.active_seconds()), int(self.limit_s)
        self.query_one("#status", Static).update(
            f"Trial {p['session_trials'] + 1} this session · {t // 60:02d}:{t % 60:02d} of "
            f"{lim // 60:02d}:{lim % 60:02d}")

    def show_readout(self):
        """Debug: the burst's frequency, each ear's level and the running
        estimate. Answers given while it shows are marked `peeked`."""
        w = self.query_one("#debug", Static)
        tr = self.trial
        if not self.show_debug or tr is None:
            w.update("")
            return
        p = tr.plan
        (l, r) = tr.played
        w.update(f"{p.freq:,.0f} Hz · L {l:+.1f} dBFS, R {r:+.1f} dBFS (L − R {p.ild_db:+.1f} dB)   "
                 f"(balance estimate {tr.estimate_db:+.1f} dB, left louder)")


class SessionEndScreen(MessageScreen):
    BINDINGS = [
        Binding("n", "next", "Next session"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, runner: TrialRunner, reason: str):
        super().__init__()
        self.runner, self.reason = runner, reason

    def on_mount(self):
        r = self.runner
        p = r.progress()
        if getattr(r, "threshold", False):
            self.threshold_end(p)
            return
        if getattr(r, "centring", False):
            self.centring_end(p)
            return
        acc = r.catch_accuracy()
        acc_s = f"catch trials {acc:.0%} right" if acc is not None else "no catch trials yet"
        if r.speaker:
            self.speaker_end(p)
            return
        if self.reason == "discarded":
            head, summary = "Session discarded.", ""
        else:
            head = {"time": "Time for a break.", "done": "Verify session complete."}.get(self.reason,
                                                                                    "Session saved.")
            undone = f", {p['undone']} undone" if p["undone"] else ""
            fast = f", {p['fast']} very fast" if p["fast"] else ""
            summary = f"{p['session_trials']} trials{undone}, {acc_s}{fast}.\n"
        prior = r.prior_trials
        self.say(head,
                 summary + "Totals: " + " / ".join(
                     f"{e} {prior[e] + (0 if self.reason == 'discarded' else p['session_trials_ear'][e])}"
                     for e in EARS) + "\n\n"
                 "Take the headphones off and rest a few minutes before the next session.",
                 "N: next session   Q: quit")

    def centring_end(self, p):
        if self.reason == "discarded":
            head, summary = "Session discarded.", ""
        else:
            head = "Time for a break." if self.reason == "time" else "Session saved."
            summary = f"{p['session_trials']} trials, {p['left']} heard left of centre.\n\n"
        self.say(head, summary + "Rest your ears a few minutes before the next session.", "N: next session   Q: quit")

    def threshold_end(self, p):
        if self.reason == "discarded":
            head, summary = "Session discarded.", ""
        else:
            head = "Time for a break." if self.reason == "time" else "Session saved."
            parts = [f"{p['session_trials']} trials"]
            if p["probes"]:
                parts.append(f"{p['probe_hits']} of {p['probes']} clear bursts picked")
            parts.append(f"neither in {p['neither']}, the wrong interval in {p['wrong']}")
            summary = ", ".join(parts) + ".\n\n"
        self.say(head, summary + "Rest your ears a few minutes before the next session.", "N: next session   Q: quit")

    def speaker_end(self, p):
        from flatiron.session.speaker import next_block, progress

        done, total = progress(self.runner.profile.sessions())
        nxt = next_block(self.runner.profile)
        head = "Block discarded." if self.reason == "discarded" else "Block saved."
        summary = "" if self.reason == "discarded" else f"{p['session_trials']} trials this block. "
        summary += f"{done} of {total} blocks done."
        if nxt is None:
            self.say(head, summary + " That's the whole plan.", "Q: quit")
            return
        ear = "same ear" if nxt.ear == self.runner.speaker["open_ear"] else \
            f"{EAR_NAMES[nxt.ear].lower()} ear (move the earplug)"
        self.say(head, summary + f"\n\nNext: {ear}, {nxt.label}.", "N: next block   Q: quit")

    def action_next(self):
        if self.runner.speaker:
            from flatiron.session.speaker import next_block

            if next_block(self.runner.profile) is None:
                return
        self.dismiss("next")


class FlatironApp(App):
    """Per session: level → channel check → sweep (first session, if asked) →
    trials → end screen (reseat, then the next session)."""

    CSS = CSS
    TITLE = "Flatiron"
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, make_runner: Callable[[], TrialRunner], player, sweep: bool = False,
                 sweep_timing: SweepTiming = SweepTiming(), clock: Callable[[], float] = time.monotonic,
                 sweep_eq=None):
        super().__init__()
        self.sweep_eq = sweep_eq
        self.make_runner = make_runner
        self.player = player
        self.do_sweep = sweep
        self.sweep_timing = sweep_timing
        self.clock = clock
        self.runner: TrialRunner | None = None
        self.sessions_run = 0
        self.tincan_seen = None  # the TinCan EQ last shown in a notice (not shown again for the next session)

    def on_mount(self):
        self.start_session()

    def start_session(self, last: str = ""):
        last_ear = self.runner.speaker["open_ear"] if self.runner is not None and self.runner.speaker else None
        self.runner = self.make_runner()
        from flatiron.session.store import qualified

        self.sub_title = f"{qualified(self.runner.profile)} · session {self.runner.session.id}"
        tc = getattr(self.runner, "tincan", None)
        if tc is not None and tc.at_start is not None and tc.at_start != self.tincan_seen:
            self.tincan_seen = tc.at_start

            def after(ok):
                if ok is None:
                    self.runner.finish()  # nothing recorded: deleted
                    self.exit()
                    return
                tc.at_start = tc.watch.start()  # whatever it is now; changes from here are announced
                self.begin_session(last, last_ear)

            self.push_screen(TinCanNoticeScreen(tc.watch), after)
            return
        self.begin_session(last, last_ear)

    def begin_session(self, last: str = "", last_ear: str | None = None):
        if getattr(self.runner, "threshold", False):
            self.push_screen(ThresholdIntroScreen(), self.after_intro)
            return
        if getattr(self.runner, "centring", False):
            self.push_screen(CentringIntroScreen(), self.after_intro)
            return
        if self.runner.speaker and self.sessions_run:  # the level is set once a run
            self.after_level(self.runner.level_dbfs, switch=last_ear != self.runner.speaker["open_ear"], last=last)
            return
        self.push_screen(self.level_screen(), self.after_level)

    def level_screen(self, mid_session: bool = False) -> LevelScreen:
        r = self.runner
        return LevelScreen(r.level_dbfs, mid_session=mid_session, measured=r.measured, gain=r.match_gain)

    def after_intro(self, ok):
        if ok is None:
            self.runner.finish()  # nothing recorded: deleted
            self.exit()
            return
        if getattr(self.runner, "threshold", False):  # a consistent start for the descending run
            self.push_screen(self.level_screen(), self.after_level)
            return
        self.push_screen(ChannelCheckScreen(self.runner), lambda _: self.start_trials())

    def after_level(self, level, switch: bool = True, last: str = ""):
        if level is None:
            self.runner.finish()  # nothing recorded: deleted
            self.exit()
            return
        self.runner.set_level(level)
        if self.runner.speaker:
            from flatiron.session.speaker import progress

            done, total = progress(self.runner.profile.sessions())
            block = lambda last: self.push_screen(BlockScreen(self.runner, done, total, last),
                                                  lambda _: self.start_trials())
            if switch:  # the earplug gets its own screen, then the position
                first = self.sessions_run == 0
                self.push_screen(EarplugScreen(self.runner.speaker["open_ear"], first, last), lambda _: block(""))
            else:
                block(last)
            return
        self.push_screen(ChannelCheckScreen(self.runner), self.after_channel_check)

    def after_channel_check(self, ok):
        if self.do_sweep and self.sessions_run == 0:
            self.push_screen(SweepScreen(self.runner, self.sweep_timing, self.sweep_eq), lambda _: self.start_trials())
        else:
            self.start_trials()

    def start_trials(self):
        screen = TrialScreen
        if getattr(self.runner, "threshold", False):
            screen = ThresholdScreen
        elif getattr(self.runner, "centring", False):
            screen = CentringScreen
        self.push_screen(screen(self.runner, self.clock), self.after_trials)

    def after_trials(self, reason):
        self.runner.finish()
        self.sessions_run += 1
        if self.runner.speaker and reason != "quit":
            # Straight on to the next block's setup screen, which says how this one went.
            from flatiron.session.speaker import next_block

            if next_block(self.runner.profile) is not None:
                n = self.runner.progress()["session_trials"]
                self.start_session("Block discarded." if reason == "discarded" else f"Block saved: {n} trials.")
                return
        self.push_screen(SessionEndScreen(self.runner, reason), self.after_end)

    def after_end(self, choice):
        if choice == "next":
            self.start_session()

    def on_unmount(self):
        if self.runner is not None and self.runner.session.ended is None:
            self.runner.finish()
        self.player.close()
