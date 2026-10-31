"""Keyboard-driven trial UI (DESIGN.md, "UI and session flow").

Flow per session: channel check → optional sweep (first session only) →
trials until the time limit or Q → end screen (reseat, then next session).
The fitted curve is never shown during trials.
"""

import random
import time
from typing import Callable

import numpy as np
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Static

from flatiron import EARS, FS
from flatiron.audio.limiter import (DEFAULT_LEVEL_DB, MAX_LEVEL_DB, MIN_LEVEL_DB, db_to_amp, limit,
                                    monaural)
from flatiron.audio.stimulus import StimulusSpec, make_stimulus
from flatiron.session.runner import PreparedTrial, TrialRunner
from flatiron.session.store import Profile, now_iso
from flatiron.session.sweep import SweepTiming, pair_taps, sweep_signal

ITI_S = 0.5  # pause between an answer and the next trial
VOLUME_REMINDER = ("If you changed your system volume for Flatiron, turn it back down "
                   "before playing anything else.")
EAR_NAMES = {"L": "LEFT", "R": "RIGHT"}

CSS = """
Screen { align: center middle; }
#title { width: 100%; content-align: center middle; text-style: bold; padding: 1 0; }
#message { width: 100%; content-align: center middle; padding: 1 2; }
#status { width: 100%; content-align: center middle; color: $text-muted; padding: 1 0; }
#panels { height: 13; width: 100%; padding: 0 6; }
.panel {
    width: 1fr; height: 100%; margin: 0 2; border: heavy $panel-lighten-2;
    content-align: center middle; text-style: bold;
}
.panel.lit { background: $accent; color: $text; border: heavy $accent-lighten-2; }
.panel.answerable { border: heavy $success; }
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

    Raising the system volume to suit quiet test tones leaves it dangerously
    high for everything played afterwards, so Flatiron moves its own level.
    """

    BINDINGS = [
        Binding("up", "step(1)", "+1 dB"),
        Binding("down", "step(-1)", "−1 dB"),
        Binding("pageup", "step(5)", "+5 dB", show=False),
        Binding("pagedown", "step(-5)", "−5 dB", show=False),
        Binding("enter,s", "save", "Save"),
        Binding("escape", "cancel", "Cancel without saving"),
    ]

    # Bursts like the trials': one ear at a time, across the measured range.
    # A single 4 kHz tone in both ears misleads: both ears sound louder than
    # one, and 4 kHz is where hearing is most sensitive.
    FREQS = (1000, 2000, 3000, 4000, 6000, 8000, 11000, 14000)
    GAP_S = 0.3

    def __init__(self, profile: Profile, start_db: float | None = None):
        super().__init__()
        self.profile = profile
        stored = profile.read().get("level")
        if start_db is None:
            start_db = stored["dbfs"] if stored else DEFAULT_LEVEL_DB
        self.level = float(np.clip(start_db, MIN_LEVEL_DB, MAX_LEVEL_DB))
        self.note = ""

    def on_mount(self):
        self.play()

    def sequence(self) -> np.ndarray:
        gap = np.zeros((int(self.GAP_S * FS), 2), np.float32)
        parts = []
        for i, f in enumerate(self.FREQS):
            x = make_stimulus(float(f), StimulusSpec(), seed=0) * db_to_amp(self.level)
            parts += [monaural(limit(x).signal, "LR"[i % 2]), gap]
        return np.concatenate(parts)

    def play(self):
        self.app.player.start(self.sequence(), loop=True)
        self.say("Listening level",
                 "Leave your system volume where you normally listen and don't change it.\n"
                 "These bursts are like the trial sounds: one ear at a time, low to high pitch.\n"
                 "Use ↑ / ↓ until the loudest of them is comfortable and moderate, about as loud "
                 "as quiet conversation. Not loud.\n\n"
                 f"Level: {self.level:+.0f} dBFS   (range {MIN_LEVEL_DB:+.0f} to {MAX_LEVEL_DB:+.0f})"
                 + (f"\n\n{self.note}" if self.note else ""),
                 "↑/↓: 1 dB   PgUp/PgDn: 5 dB   Enter: save   Esc: cancel without saving")

    def action_step(self, d: int):
        new = float(np.clip(self.level + d, MIN_LEVEL_DB, MAX_LEVEL_DB))
        self.note = ""
        if new == self.level and d > 0:
            self.note = ("That's the most Flatiron will play: any louder and its peak limiter would turn "
                         "most trials back down. If it's still too quiet, raise the system volume a "
                         "little, and turn it back down when you're done.")
        self.level = new
        self.play()

    def action_save(self):
        self.app.player.stop()
        self.profile.update(level={"dbfs": self.level, "calibration": "trial-like sequence", "set": now_iso()})
        self.dismiss(self.level)

    def action_cancel(self):
        self.app.player.stop()
        self.dismiss(None)


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
        burst = limit(np.concatenate([x, np.zeros(int(0.25 * FS)), x]) * db_to_amp(self.runner.settings.level_dbfs)).signal
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
            self.say(message=f"That burst was in the {EAR_NAMES[played].lower()} ear, but you heard it on the "
                             f"{EAR_NAMES[side].lower()}.\n\nCheck that the headphones are on the right way "
                             "round and both channels work, then press Enter to try again.",
                     status="Enter: retry   Esc: quit")
            return
        if self.order:
            self.play_current()
        else:
            self.dismiss(True)

    def action_retry(self):
        if self.failed:
            self.start()


class SweepScreen(MessageScreen):
    """Phase 0: slow sweep per ear; tap at peaks and dips."""

    BINDINGS = [
        Binding("enter", "begin", "Start"),
        Binding("up,p", "tap('peak')", "Peak (louder)"),
        Binding("down,d", "tap('dip')", "Dip (quieter)"),
        Binding("escape", "skip", "Skip sweep"),
    ]

    def __init__(self, runner: TrialRunner, timing: SweepTiming = SweepTiming()):
        super().__init__()
        self.runner = runner
        self.timing = timing
        self.ears = list(EARS)
        self.playing = False
        self.t0 = 0.0
        self.taps: list[tuple[float, str]] = []
        self.timer = None
        self.results: dict[str, int] = {}

    def on_mount(self):
        self.intro()

    def intro(self):
        ear = self.ears[0]
        self.say(f"Sweep: {EAR_NAMES[ear]} ear",
                 "A slow narrowband sweep plays from 1 to 14 kHz, then back down.\n"
                 "Tap ↑ (or P) whenever it gets suddenly louder, ↓ (or D) when it gets quieter.\n"
                 "Tap on the way down too: averaging the two cancels your reaction time.",
                 "Enter: start   Esc: skip the sweep")

    def action_begin(self):
        if self.playing or not self.ears:
            return
        ear = self.ears[0]
        x = sweep_signal(self.timing, self.runner.fs, seed=self.runner.seed)
        res = limit(x * db_to_amp(self.runner.settings.level_dbfs))
        self.app.player.start(monaural(res.signal, ear))
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
                 status=f"{len(self.taps)} taps")

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
    BINDINGS = [
        Binding("left,1", "answer(True)", "A louder"),
        Binding("right,2", "answer(False)", "B louder"),
        Binding("space", "replay", "Replay"),
        Binding("p", "pause", "Pause"),
        Binding("q", "quit_session", "Save and quit"),
        Binding("x", "discard", "Discard session"),
    ]

    def __init__(self, runner: TrialRunner, clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self.runner = runner
        self.clock = clock
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
            yield Static("Which was louder?", id="title")
            with Horizontal(id="panels"):
                yield Static("A", id="A", classes="panel")
                yield Static("B", id="B", classes="panel")
            yield Static("", id="message")
            yield Static("", id="status")
        yield Footer()

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
        tot = " / ".join(f"{e} {p['total'][e]}" for e in EARS)
        self.query_one("#status", Static).update(
            f"Trial {p['session_trials'] + 1} this session · totals {tot} · "
            f"{p['block_left']} to next block · {t // 60:02d}:{t % 60:02d} of {lim // 60:02d}:{lim % 60:02d} · "
            f"level {self.runner.settings.level_dbfs:+.0f} dBFS")

    # -- playback ----------------------------------------------------------

    def light(self, which: str | None, answerable: bool = False):
        for k in ("A", "B"):
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
        at(tr.first_on, lambda: self.light("A", answerable=self.heard_b))
        at(tr.first_off, lambda: self.light(None, answerable=self.heard_b))
        at(tr.second_on, self.second_on)
        at(tr.second_off, lambda: self.light(None, answerable=True))

    def second_on(self):
        if not self.heard_b:
            self.heard_b = True
            self.first_end = self.last_end
        self.light("B", answerable=True)
        self.accepting = True

    # -- trial loop --------------------------------------------------------

    def fetch_next(self, answer: tuple | None):
        """Record an answer (if any) and prepare the next trial off the UI thread."""
        self.busy = True

        def work():
            if answer is not None:
                self.runner.answer(**answer)
            nxt = self.runner.prepare()
            self.app.call_from_thread(self.ready, nxt)

        self.run_worker(work, thread=True, exclusive=True, group="trial")
        self.set_timer(0.7, self.show_busy)

    def show_busy(self):
        # Block-end refits (noise fit, grid refinement) take a few seconds.
        if self.busy:
            self.query_one("#message", Static).update("Updating the model…")

    def ready(self, nxt: PreparedTrial):
        self.busy = False
        self.show_status()
        if self.active_seconds() >= self.limit_s:
            self.dismiss("time")
            return
        self.trial, self.replays = nxt, 0
        self.heard_b, self.first_end = False, None
        self.query_one("#message", Static).update("")
        if not self.paused:
            self.timers.append(self.set_timer(ITI_S, self.play))

    def action_answer(self, first_louder: bool):
        if not self.accepting or self.paused or self.busy:
            return
        self.accepting = False
        now = self.clock()
        self.app.player.stop()  # fades out if still playing
        self.cancel_timers()
        self.light(None)
        self.fetch_next(dict(trial=self.trial, first_louder=first_louder,
                             rt_ms=(now - self.first_end) * 1000, rt_last_ms=(now - self.last_end) * 1000,
                             replays=self.replays, t=now - self.started))

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
        acc = r.catch_accuracy()
        acc_s = f"{acc:.0%} of catch trials right" if acc is not None else "no catch trials yet"
        if self.reason == "discarded":
            head, summary = "Session discarded.", "Nothing from it was kept.\n"
        else:
            head = "Time for a break." if self.reason == "time" else "Session saved."
            summary = (f"{p['session_trials']} trials this session, {acc_s}, "
                       f"{p['fast']} very fast answers.\n")
        prior = r.prior_trials
        self.say(head,
                 summary + "Totals: " + " / ".join(
                     f"{e} {prior[e] + (0 if self.reason == 'discarded' else p['session_trials_ear'][e])}"
                     for e in EARS) + "\n\n"
                 "Before the next session take the headphones off, rest a few minutes, "
                 "and put them back on.\nKeep the system volume where it is.\n\n" + VOLUME_REMINDER,
                 "N: start the next session (channel check first)   Q: quit")

    def action_next(self):
        self.dismiss("next")


class FlatironApp(App):
    CSS = CSS
    TITLE = "Flatiron"

    def __init__(self, make_runner: Callable[[], TrialRunner] | None, player, sweep: bool = False,
                 sweep_timing: SweepTiming = SweepTiming(), clock: Callable[[], float] = time.monotonic,
                 profile: Profile | None = None, level: str = "if_unset"):
        """level: "only" (just set the level), "if_unset" (set it first if the
        profile has none) or "skip"."""
        super().__init__()
        self.profile = profile
        self.level_mode = level if profile is not None else "skip"
        self.make_runner = make_runner
        self.player = player
        self.do_sweep = sweep
        self.sweep_timing = sweep_timing
        self.clock = clock
        self.runner: TrialRunner | None = None
        self.sessions_run = 0

    def on_mount(self):
        if self.level_mode == "only" or (self.level_mode == "if_unset" and self.profile.read().get("level") is None):
            self.push_screen(LevelScreen(self.profile), self.after_level)
        else:
            self.start_session()

    def after_level(self, level):
        if self.level_mode == "only" or level is None:
            self.exit(level)
        else:
            self.start_session()

    def start_session(self):
        self.runner = self.make_runner()
        self.sub_title = f"{self.runner.profile.read()['label']} · session {self.runner.session.id}"
        self.push_screen(ChannelCheckScreen(self.runner), self.after_channel_check)

    def after_channel_check(self, ok):
        if self.do_sweep and self.sessions_run == 0:
            self.push_screen(SweepScreen(self.runner, self.sweep_timing), lambda _: self.start_trials())
        else:
            self.start_trials()

    def start_trials(self):
        self.push_screen(TrialScreen(self.runner, self.clock), self.after_trials)

    def after_trials(self, reason):
        self.runner.finish()
        self.sessions_run += 1
        self.push_screen(SessionEndScreen(self.runner, reason), self.after_end)

    def after_end(self, choice):
        if choice == "next":
            self.start_session()

    def on_unmount(self):
        if self.runner is not None and self.runner.session.ended is None:
            self.runner.finish()
        self.player.close()
