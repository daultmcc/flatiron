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
from flatiron.audio.limiter import MAX_LEVEL_DB, MIN_LEVEL_DB, db_to_amp, limit, monaural
from flatiron.audio.stimulus import StimulusSpec, make_stimulus
from flatiron.session.runner import PreparedTrial, TrialRunner
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

    # Bursts like the trials': one ear at a time, across the measured range.
    # A single 4 kHz tone in both ears misleads: both ears sound louder than
    # one, and 4 kHz is where hearing is most sensitive.
    FREQS = (1000, 2000, 3000, 4000, 6000, 8000, 11000, 14000)
    GAP_S = 0.3

    def __init__(self, start_db: float, mid_session: bool = False):
        super().__init__()
        self.start = float(np.clip(start_db, MIN_LEVEL_DB, MAX_LEVEL_DB))
        self.level = self.start
        self.mid_session = mid_session
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
        warning = ("Changing the level partway through a session can shift the results slightly: "
                   "hearing's frequency response changes with loudness (about 0.4 dB at most for a "
                   "2 dB change, 1 dB for 5 dB). Only change it if it's really needed.\n\n"
                   if self.mid_session else "")
        cancel = "Esc: back, no change" if self.mid_session else "Esc: quit"
        self.say("Listening level",
                 warning +
                 "Leave your system volume where you normally listen and don't change it.\n"
                 "These bursts are like the trial sounds: one ear at a time, low to high pitch.\n"
                 "Use ↑ / ↓ until the loudest of them is comfortable and moderate, about as loud "
                 "as quiet conversation. Not loud.\n\n"
                 f"Level: {self.level:+.0f} dBFS   ({'current' if self.mid_session else 'last used'} "
                 f"{self.start:+.0f}; "
                 f"range {MIN_LEVEL_DB:+.0f} to {MAX_LEVEL_DB:+.0f})"
                 + (f"\n\n{self.note}" if self.note else ""),
                 f"↑/↓: 1 dB   PgUp/PgDn: 5 dB   Enter: continue at this level   {cancel}")

    def action_step(self, d: int):
        new = float(np.clip(self.level + d, MIN_LEVEL_DB, MAX_LEVEL_DB))
        self.note = ""
        if new == self.level and d > 0:
            self.note = ("That's the most Flatiron will play: any louder and its peak limiter would turn "
                         "most trials back down. If it's still too quiet, raise the system volume a "
                         "little, and turn it back down when you're done.")
        self.level = new
        self.play()

    def action_accept(self):
        self.app.player.stop()
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
        eq = ("\nYour current correction is applied, so what stands out is what it hasn't fixed. "
              "Q turns it off and on." if self.eq is not None else "")
        self.say(f"Sweep: {EAR_NAMES[ear]} ear",
                 "A slow narrowband sweep plays from 1 to 14 kHz, then back down.\n"
                 "Tap ↑ (or P) whenever it gets suddenly louder, ↓ (or D) when it gets quieter.\n"
                 "Tap on the way down too: averaging the two cancels your reaction time." + eq,
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
    BINDINGS = [
        Binding("left,1", "answer(True)", "A louder"),
        Binding("right,2", "answer(False)", "B louder"),
        Binding("space", "replay", "Replay"),
        Binding("p", "pause", "Pause"),
        Binding("u,backspace", "undo", "Undo"),
        Binding("l", "level", "Level"),
        Binding("q", "quit_session", "Quit"),
        Binding("x", "discard", "Discard"),
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
            f"level {self.runner.level_dbfs:+.0f} dBFS")

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
            self.action_pause()  # resume: the current pair replays
            self.show_status()

        self.app.push_screen(LevelScreen(self.runner.level_dbfs, mid_session=True), done)

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
            head = {"time": "Time for a break.", "done": "Verify session complete."}.get(self.reason,
                                                                                    "Session saved.")
            undone = f", {p['undone']} undone" if p["undone"] else ""
            summary = (f"{p['session_trials']} trials this session{undone}, {acc_s}, "
                       f"{p['fast']} very fast answers.\n")
        prior = r.prior_trials
        self.say(head,
                 summary + "Totals: " + " / ".join(
                     f"{e} {prior[e] + (0 if self.reason == 'discarded' else p['session_trials_ear'][e])}"
                     for e in EARS) + "\n\n"
                 "Before the next session take the headphones off, rest a few minutes, "
                 "and put them back on.\nKeep the system volume where it is.\n\n" + VOLUME_REMINDER,
                 "N: start the next session (level and channel check first)   Q: quit")

    def action_next(self):
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

    def on_mount(self):
        self.start_session()

    def start_session(self):
        self.runner = self.make_runner()
        self.sub_title = f"{self.runner.profile.read()['label']} · session {self.runner.session.id}"
        self.push_screen(LevelScreen(self.runner.level_dbfs), self.after_level)

    def after_level(self, level):
        if level is None:
            self.exit()
            return
        self.runner.set_level(level)
        self.push_screen(ChannelCheckScreen(self.runner), self.after_channel_check)

    def after_channel_check(self, ok):
        if self.do_sweep and self.sessions_run == 0:
            self.push_screen(SweepScreen(self.runner, self.sweep_timing, self.sweep_eq), lambda _: self.start_trials())
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
