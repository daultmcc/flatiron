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
uv run flatiron report -H                        # the current fit's filters; -H opens the HTML report
uv run flatiron config                           # show the profile's fit settings
uv run flatiron config --broad -s 50            # change them (every fit uses them from now on)
uv run flatiron audition                         # the fit on/off: pink noise, sweep, or --source song.wav
uv run flatiron config --checkpoint              # fix the current filters; later fits only add to them
uv run flatiron verify                           # ~150 trials with the correction applied; reports leftovers
uv run flatiron export -t lsp -o sundara.cfg     # or -t apo / easyeffects / pipewire / tincan; -m preset.txt
uv run flatiron tone -p "HD 600"                 # tone generator: glide by hand, mark peaks and dips
uv run flatiron report --compare A B C -H         # several profiles per ear: their curves, what the veto would correct
uv run flatiron config -r consensus:B,C           # use B's and C's raw curves as the reference
uv run flatiron config --veto B,C                 # correct features toward the median of this one's, B's and C's
uv run flatiron report --csv curves.csv           # the level, reference, baseline and residual, per ear
uv run flatiron profiles new "Etude" --kind iem   # or `profiles set NAME --kind ... --label ...`
uv run flatiron simulate                         # scheduler + fit against a simulated listener
uv run flatiron simulate --scenario seating       # or consensus: the multi-session and multi-headphone checks
uv run pytest                                    # in parallel (pytest-xdist); -n0 for one process
```

`run` options: `--device N`, `--minutes` (at most 15), `--stimulus noise|warble|tone`,
`--bands-per-octave 12|24`, `--active-eq preset.txt` (system EQ in effect while
measuring, as Equalizer APO / AutoEQ text), `--dry-run` (no audio), `--sweep`
(tap at peaks and dips in a slow sweep first; once there's a fit, the sweep
plays through it, Q toggling it, so you hear what it hasn't fixed),
`--sweep-seconds` (each way, default 30).

Leave the system volume at your usual listening level. Each session opens on a
level screen (starting from the last session's level) where Flatiron's own
level is adjusted with ↑/↓.

Trial keys: ← or 1 = A louder, → or 2 = B louder, Space = replay, P = pause,
U = undo the last answer (the pair plays again), L = re-adjust the level (with
a warning; changes are logged), Q = save and quit, X = discard the session
(asks first). Every answer is saved as it's given, under
`~/.local/share/flatiron/profiles/<headphone>/sessions/` (macOS:
`~/Library/Application Support/flatiron/`).

There's no separate fit step: `report`, `export`, `audition`, `verify` and the
sweep compute the fit when they need it, from all the profile's trials and its
settings (the perceptual fit is cached, so this takes a few seconds only after
new trials). `config` settings: `--width` (max feature width W, octaves),
`--reference iso226|diffuse|none|file.csv|consensus:A,B`, `--broad` (also correct broad
features toward the fixed target; by default only narrow features are corrected
and the original broad shape is kept, leaving broad tonal changes to you),
`-s/--broad-strength` (how much of each broad feature, %; needs `--broad`), `--k`, `--k-broad`, `--bands`, `--boost-narrow`,
`--boost-broad`, `--max-cut`, `--f-lo`, `--f-hi`, `--no-consistency`,
`--sessions id,id|all`, `--pool OTHER[,OTHER]|none` (include another profile's trials, e.g. the same
headphones measured with tones), `--method smooth` with `--smooth-width OCT` (experimental: correct every
residual against a smoothed curve, no feature detection; corrects some noise too), `--method match`
(experimental: correct the whole curve toward the reference, tonality included),
`--baseline joint` (experimental: a baseline that moves smoothly with the data instead of jumping between two
readings of a broad region well off the reference, shown with its uncertainty in the report; needs
`--method features`);
`--reset [name ...]` returns settings to the defaults.
`-r consensus:A,B` uses the listed profiles' raw curves as the reference instead of a fixed one, smoothed over
`--consensus-smooth OCT` (default 1/2 octave, 0: none). This profile is only in it if you list it, and with three
headphones you probably want to: two others alone give their mean, not a median. `--veto A,B|none` corrects each
feature toward the median feature of this profile and those, among features that overlap it, and `--veto-import DB`
(default 1.5, 0: none) limits what it may add where that median feature is at another frequency.
`--veto-mode pointwise` instead corrects frequency by frequency, by what exceeds the median of the residuals, and
adds nothing. The reference and veto can be used together, and with or without `--broad`; `--veto` needs
`--method features`.
`--checkpoint` fixes the current filters: later fits keep them, whatever the
settings, and only add filters (within `--bands`) for what's still
significant, e.g. after `verify`. `--reset-checkpoint` drops it. The design's
default diffuse-field reference needs the ISO 389-7 table, which isn't sourced
yet, so `iso226` (free field) is the default for now.

`tone` keys: ←/→ frequency (a tap steps; holding glides, speeding up the longer
it's held), ↑/↓ glide speed, G go to a frequency, E ear (left/right/both), S
stimulus (pure tone, warble, 1/24, 1/12 and 1/3-octave noise), +/− level, Q
correction on/off (with `-p`), P/D mark a peak/dip, U undo the last mark, Space
pause, Esc quit. Marks are saved in the profile; the next `run` session
refines its frequency grid around them.

LSP exports use filter mode
"APO (DR)", which matches Flatiron's filters exactly; when LSP's LADSPA build is
installed, every LSP export is checked against the real plugin before it's
written. `--merge` puts an Equalizer APO / AutoEQ preset (your preference EQ)
in front of the correction in the same file, with the preamp computed over both.
The preamp defaults to enough headroom that no boost can clip; `--preamp 0`
keeps the uncorrected level, which is easier to A/B against. `-t pipewire
--check` measures the filters through a private filter-chain on the running
PipeWire (virtual sink and source; nothing plays on your devices).
`-t tincan` saves a TinCan preset named "<profile> Flatiron <fit>" straight
into TinCan's presets (or to `-o FILE`, for TinCan's import). TinCan presets
carry no preamp, since TinCan sets one for everything loaded, so `--preamp`
doesn't apply; filters identical in both channels are written once, as LR.

Layout: `audio/` (biquads, stimuli, limiter, devices, playback), `model/`
(grid, the hierarchical perceptual fit, refinement), `session/` (scheduler, runner, storage,
sweep), `ui/` (Textual apps: trials, audition, tone generator), `fit/` (reference, baseline, features, filters,
pipeline, consensus), `export/` (APO text, LSP, EasyEffects, PipeWire, TinCan, truth checks), `report/` (figures, HTML), `sim/` (simulated listener, designs,
validation), `apo.py` (APO text parsing), `iso226.py` (equal-loudness contours).
