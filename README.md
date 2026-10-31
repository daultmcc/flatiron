# Flatiron

By-ear headphone resonance EQ from forced-choice loudness judgments. See
[DESIGN.md](DESIGN.md) for the method.

Status: milestones 1 (audio core), 2 (model and simulator), 3 (adaptive
scheduler) and 4 (trial UI and session files) are in place. Fitting filters,
exports and reports come next.

```sh
uv sync
uv run flatiron devices                          # list outputs, flag Bluetooth/mono/rate issues
uv run flatiron level --profile "HD 600"         # set system volume once; saved to the profile
uv run flatiron run --profile "HD 600" --sweep   # first session: coarse sweep, then trials
uv run flatiron run --profile "HD 600"           # later sessions (reseat in between)
uv run flatiron sessions --profile "HD 600"      # trial counts, catch accuracy, fast answers
uv run flatiron probe                            # hunt by hand: a band at a chosen freq/ear
uv run flatiron simulate                         # scheduler + fit against a simulated listener
uv run pytest
```

`run` options: `--device N`, `--minutes` (at most 15), `--stimulus noise|warble|tone`,
`--bands-per-octave 12|24`, `--active-eq preset.txt` (system EQ in effect while
measuring, as Equalizer APO / AutoEQ text), `--dry-run` (no audio).

Trial keys: ← or 1 = A louder, → or 2 = B louder, Space = replay, P = pause,
Q = save and quit. Every answer is saved as it's given, under
`~/.local/share/flatiron/profiles/<headphone>/sessions/` (macOS:
`~/Library/Application Support/flatiron/`).

Layout: `audio/` (biquads, stimuli, limiter, devices, playback), `model/`
(grid, probit fit, refinement), `session/` (scheduler, runner, storage,
sweep), `ui/` (Textual app), `sim/` (simulated listener, baseline design,
runner), `apo.py` (APO text parsing), `iso226.py` (equal-loudness contours).
