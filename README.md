# Flatiron

By-ear headphone resonance EQ from forced-choice loudness judgments. See
[DESIGN.md](DESIGN.md) for the method.

Status: milestones 1 to 5 are in place (audio core, model and simulator,
adaptive scheduler, trial UI and session files, fit pipeline and report).
Exporters come next. The simulator doesn't yet meet DESIGN.md's strict pass
criteria (see `flatiron simulate` and `src/flatiron/sim/validate.py`).

```sh
uv sync
uv run flatiron devices                          # list outputs, flag Bluetooth/mono/rate issues
uv run flatiron profiles                         # list headphone profiles
uv run flatiron run --profile "HD 600" --sweep   # first session: coarse sweep, then trials
uv run flatiron run --profile "HD 600"           # later sessions (reseat in between)
uv run flatiron sessions --profile "HD 600"      # trial counts, catch accuracy, fast answers
uv run flatiron sessions --profile "HD 600" --delete <id>   # e.g. a session left open by a crash
uv run flatiron fit --profile "HD 600"           # filters per ear + an HTML report
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

`fit` options (all usable without new listening; the perceptual fit is
cached): `--width` (max feature width W, octaves), `--reference iso226|diffuse|none|file.csv`,
`--strength` (broad features, %), `--k`, `--k-broad`, `--bands`, `--boost-narrow`,
`--boost-broad`, `--max-cut`, `--f-lo`, `--f-hi`, `--sessions`, `--no-consistency`.
The design's default diffuse-field reference needs the ISO 389-7 table, which
isn't sourced yet, so `iso226` (free field) is the default for now.

Layout: `audio/` (biquads, stimuli, limiter, devices, playback), `model/`
(grid, probit fit, refinement), `session/` (scheduler, runner, storage,
sweep), `ui/` (Textual app), `fit/` (reference, baseline, features, filters,
pipeline), `report/` (figures, HTML), `sim/` (simulated listener, designs,
validation), `apo.py` (APO text parsing), `iso226.py` (equal-loudness contours).
