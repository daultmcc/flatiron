# Flatiron

By-ear headphone resonance EQ from forced-choice loudness judgments. See
[DESIGN.md](DESIGN.md) for the method.

Status: milestones 1 (audio core), 2 (model and simulator), 3 (adaptive
scheduler) and 4 (trial UI and session files) are in place. Fitting filters,
exports and reports come next.

```sh
uv sync
uv run flatiron devices                          # list outputs, flag Bluetooth/mono/rate issues
uv run flatiron profiles                         # list headphone profiles
uv run flatiron run --profile "HD 600" --sweep   # first session: coarse sweep, then trials
uv run flatiron run --profile "HD 600"           # later sessions (reseat in between)
uv run flatiron sessions --profile "HD 600"      # trial counts, catch accuracy, fast answers
uv run flatiron sessions --profile "HD 600" --delete <id>   # e.g. a session left open by a crash
uv run flatiron probe                            # hunt by hand: a band at a chosen freq/ear
uv run flatiron simulate                         # scheduler + fit against a simulated listener
uv run pytest
```

`run` options: `--device N`, `--minutes` (at most 15), `--stimulus noise|warble|tone`,
`--bands-per-octave 12|24`, `--active-eq preset.txt` (system EQ in effect while
measuring, as Equalizer APO / AutoEQ text), `--dry-run` (no audio).

Leave the system volume at your usual listening level. Each session opens on a
level screen (starting from the last session's level) where Flatiron's own
level is adjusted with ↑/↓.

Trial keys: ← or 1 = A louder, → or 2 = B louder, Space = replay, P = pause,
U = undo the last answer (the pair plays again), L = re-adjust the level (with
a warning; changes are logged), Q = save and quit, X = discard the session
(asks first). Every answer is saved as it's given, under
`~/.local/share/flatiron/profiles/<headphone>/sessions/` (macOS:
`~/Library/Application Support/flatiron/`).

Layout: `audio/` (biquads, stimuli, limiter, devices, playback), `model/`
(grid, probit fit, refinement), `session/` (scheduler, runner, storage,
sweep), `ui/` (Textual app), `sim/` (simulated listener, baseline design,
runner), `apo.py` (APO text parsing), `iso226.py` (equal-loudness contours).
