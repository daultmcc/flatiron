# Flatiron

By-ear headphone resonance EQ from forced-choice loudness judgments. See
[DESIGN.md](DESIGN.md) for the method.

Status: all seven milestones in DESIGN.md are in place: audio core, model and
simulator, adaptive scheduler, trial UI and session files, fit pipeline and
report, exporters (Equalizer APO text, LSP x32 LeftRight, EasyEffects for
Equaliser on macOS, PipeWire), and audition and verify. Simulator validation
uses 1.5 dB tolerances rather than DESIGN.md's 1 dB (`src/flatiron/sim/validate.py`);
very narrow (Q 12-16) and near-edge (~11 kHz) features still come out shallow.

```sh
uv sync
uv run flatiron devices                          # list outputs, flag Bluetooth/mono/rate issues
uv run flatiron profiles                         # list headphone profiles
uv run flatiron run -p "HD 600" --sweep          # first session: coarse sweep, then trials
uv run flatiron run                              # later sessions (-p can go when there's one profile)
uv run flatiron sessions                         # trial counts, catch accuracy, fast answers
uv run flatiron sessions --delete <id>           # e.g. a session left open by a crash
uv run flatiron fit --open                       # filters per ear; opens report.html
uv run flatiron audition                         # the saved fit on/off: pink noise, sweep, or --source song.wav
uv run flatiron verify                           # ~150 trials with the correction applied; reports leftovers
uv run flatiron fit --increment                  # keep the filters, add ones for what verify found
uv run flatiron export -t lsp -o sundara.cfg     # or -t apo / easyeffects / pipewire; -m preset.txt
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

`export` uses the last `fit` (its filters are saved in the profile), so refit
with different settings before exporting if you want them. LSP exports use filter mode
"APO (DR)", which matches Flatiron's filters exactly; when LSP's LADSPA build is
installed, every LSP export is checked against the real plugin before it's
written. `--merge` puts an Equalizer APO / AutoEQ preset (your preference EQ)
in front of the correction in the same file, with the preamp computed over both.
The preamp defaults to enough headroom that no boost can clip; `--preamp 0`
keeps the uncorrected level, which is easier to A/B against. `-t pipewire
--check` measures the filters through a private filter-chain on the running
PipeWire (virtual sink and source; nothing plays on your devices).

Layout: `audio/` (biquads, stimuli, limiter, devices, playback), `model/`
(grid, probit fit, refinement), `session/` (scheduler, runner, storage,
sweep), `ui/` (Textual app), `fit/` (reference, baseline, features, filters,
pipeline), `export/` (APO text, LSP, LADSPA truth check), `report/` (figures, HTML), `sim/` (simulated listener, designs,
validation), `apo.py` (APO text parsing), `iso226.py` (equal-loudness contours).
