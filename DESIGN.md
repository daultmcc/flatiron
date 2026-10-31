# Flatiron — design doc for a by-ear headphone EQ tool

Oct 30, 2026 · @Dathan

## Summary

Flatiron is a local tool that finds peaks and dips in a headphone's upper mids and treble, separately for each ear, using only the listener's "which is louder?" judgments. It targets everything from narrow resonances up to features about an octave wide, such as an excess in the ear-gain region around 4 kHz. It turns them into up to 32 peaking filters per channel and exports them for PipeWire, LSP and Equaliser (macOS); broad bass/treble tilt is left to the user.

### Goals

- Find peaks and dips from about 1/12 octave wide up to a configurable maximum width (default 1 octave), between 1 kHz and 14 kHz (both limits configurable).
- Tell the listener's normal ear gain apart from excess caused by the headphone, so broad features near 3 to 4 kHz are corrected relative to a reference instead of flattened.
- Measure left and right independently. A feature in one ear doesn't leak into the other.
- Use a two-alternative forced-choice task ("A or B louder?"), never knob-matching.
- Report how confident it is in each feature, and only correct features it is sure of.
- Export per-channel filter sets in PipeWire `param_eq`, LSP Parametric Equalizer x32 LeftRight, EasyEffects JSON (the route into Equaliser) and Equalizer APO text.
- Keep every raw trial, so the curve can be refit with different settings without retesting.

### Non-goals

- Broad tilt, bass or overall target-curve correction: anything wider than the maximum feature width.
- Flattening to equal perceived loudness (see Key design decisions).
- Hearing-loss compensation or anything audiometric.
- Real-time system audio processing. Flatiron generates configs; the user's existing EQ host does the processing.

## Key design decisions

The core idea: measure perceived loudness, remove the part normal hearing explains plus anything broader than about an octave, and correct what's left.

1. **Reference first, then detrend.** What gets measured is headphone response + the listener's normal equal-loudness contour (ear gain included) + fit-dependent resonances. Flattening all of that would cut the listener's natural ear gain. Equal-loudness contours are measured in the sound field with ear gain already included, and they still aren't flat. So perceptually flat is not diffuse-field flat. Flatiron first subtracts a reference contour (default: a diffuse-field equal-loudness contour, matching a flat diffuse-field target). Then it removes anything that varies more slowly than the maximum feature width (default 1 octave, configurable). What's left gets corrected: narrow resonances, and broader excesses like a 4 kHz bump about 1.5 kHz wide.
2. **The ear-gain region is the hard case.** Features 1/2 to 1 octave wide around 2 to 5 kHz overlap the normal contour's own ear-gain peak, and individual ears differ from ISO 226 by several dB there. Corrections in that region depend on the reference, so: the reference is configurable, broad features get their own confidence threshold and a strength control (default 100%), and the report lists them separately from narrow ones.
3. **Compare neighbours, fit globally.** Comparing nearby frequencies is easier and less noisy than comparing far-apart ones. Judgments are pooled into one maximum-likelihood fit over all frequencies at once, which avoids the random-walk error you'd get from chaining neighbour comparisons. The model lets judgment noise grow with frequency separation, so long-range comparisons count for less.
4. **Redundant comparison graph.** Each grid point is compared against points 1, 2 and 3 steps away, plus about 25% longer jumps of up to an octave. The longer jumps pin down broad features that a chain of neighbour comparisons would smear out.
5. **Narrowband stimuli by default.** Pure sines inside headphones are very sensitive to seal and standing waves, and can fall between the peaks of a narrow notch. The default stimulus is band-limited noise. Warble tones and pure tones are options.
6. **Adaptive grid.** Start coarse, then refine where the fitted curve bends sharply, so narrow features don't fall between test points.
7. **Reseat and repeat.** Treble resonances move when the headphone moves. Flatiron asks for several short sessions with the headphones taken off and put back on in between. It corrects only features that show up consistently across reseats.
8. **One filter definition everywhere.** Flatiron's preview, its fit and every export use RBJ-cookbook peaking biquads. Each exporter maps to the target's equivalent mode and is verified against it (see Validation).
9. **Budget: 32 bands per channel.** LSP x32 LeftRight is the tightest target, at 32 bands per channel. PipeWire `param_eq` and Equaliser (up to 64 bands) are looser. Flatiron also counts any broad-EQ bands the user wants merged into the same instance against that 32.

## Measurement method

Each trial plays two short narrowband bursts in one ear and asks which was louder. A probit model fitted to every trial gives a perceived-level curve per ear, with error bars.

### Frequency grid

- Default range 1 kHz to 14 kHz (both configurable). Measure a little past each end (0.85 to 15 kHz) so the detrend baseline isn't distorted at the edges.
- Initial grid: 1/24-octave log spacing, about 100 points per ear.
- Refinement: insert midpoints (down to 1/48 octave) where the fitted curve bends significantly (see Adaptive selection).

### Stimuli

| Parameter | Default | Options |
| --- | --- | --- |
| Type | Band-limited noise, 1/12 octave around the centre | 1/24-octave noise, warble tone (±1/24 octave FM at 8 Hz), pure tone |
| Noise token | Frozen per (frequency, seed); a new seed per session | Fresh per presentation |
| Burst length | 400 ms | 200 to 800 ms |
| Ramps | 20 ms raised cosine | — |
| Gap between A and B | 300 ms | 150 to 600 ms |
| Presentation | Monaural: one ear only, other channel digital silence | — |
| Sample rate | 48 kHz, float32 | Device native |
| Peak level | ≤ −18 dBFS after all offsets | Hard limiter at −12 dBFS |

Generate noise bands in the frequency domain (shape an FFT, then inverse) for steep band edges. Normalise each band to equal RMS before any offset.

### Trial

1. Pick an ear, a pair of grid points (i, j) and a level offset Δ (in dB, applied to one interval).
2. Randomise which stimulus plays first, and add a small whole-trial level rove (±2 dB) so the listener can't anchor on absolute level.
3. Play A, gap, B. The listener presses A or B. Replay is allowed and logged.
4. About 5% of trials are catch trials: the same frequency with a ±6 dB offset. These track attention and the lapse rate.

### Perceptual model

For each ear, the unknowns are the perceived levels L at the grid points (dB), a noise scale σ and a lapse rate λ. The probability that interval A is judged louder:

```latex
P(A) = \frac{\lambda}{2} + (1 - \lambda)\,\Phi\!\left(\frac{L_a - L_b + \Delta_a - \Delta_b}{\sigma}\right)
```

- Fit by MAP with SciPy (L-BFGS). Use a weak Gaussian prior on second differences of L in log-frequency (smoothness) and fix the gauge with mean(L) = 0.
- σ is fit jointly as a function of the pair's separation d in octaves, σ(d) = σ₀·√(1 + (d/d₀)²), with priors around σ₀ = 1 dB and d₀ = 0.5 octave. λ has a prior around 2%, informed by the catch trials.
- Error bars come from the Laplace approximation (inverse Hessian). Every displayed curve carries a ±1 SD band.
- Sessions are fitted jointly, with a free per-session offset per ear. A flag lets a session be fitted on its own so reseats can be compared.

### Adaptive selection

- Candidate pairs: |i − j| ≤ 3 grid steps, plus about 25% longer jumps (up to one octave) to tie the graph together and pin down broad features.
- Score each candidate by the posterior variance of (L\_i − L\_j), weighted up where the current curve bends sharply. Draw from the top candidates at random so the sequence isn't predictable.
- Δ = −(L̂\_i − L̂\_j) plus jitter of ±1 posterior SD. That puts trials near the 50% point, where each answer carries the most information, while still constraining σ.
- Refinement: after each block of about 50 trials, insert a midpoint wherever the second difference of L̂ exceeds 2 SD. Stop refining at 1/48 octave.
- Interleave ears and frequency regions trial by trial.

### Optional coarse sweep (phase 0)

A slow narrowband sweep per ear: 1 to 14 kHz over about 40 s, played up and then down. The listener taps a key at each dip or peak they hear. Averaging the up and down taps cancels reaction-time lag. The marks seed grid refinement before any forced-choice trials.

### Effort estimate (to tune)

At about 6 comparisons per grid point, that's around 1,200 trials for both ears. At roughly 2.5 s per trial, that's about 50 minutes, split into four or more reseat sessions of 10 to 15 minutes, with breaks enforced at 15 minutes. The simulator should show whether the lower octave can get by with a coarser grid.

## From measurements to filters

The correction targets significant deviations from the reference that are narrower than the maximum feature width, and caps boosts harder than cuts.

### Measurement context

Flatiron treats EQ as three layers: whatever system EQ is active while measuring, the correction Flatiron fits, and an optional preference layer the user owns. Both workflows (correcting on top of an existing broad EQ, or correcting first and adding tonality after) use the same data. Only the export mode differs.

- **Active EQ is known, not measured.** Each session records the system EQ in effect (imported as APO text, or "none"). Its gain at each stimulus frequency enters the model as a known offset, the same way verify sessions handle the preview EQ. So L̂ always describes the raw headphone, and intentional broad EQ is never "corrected away", even when it's narrower than W.
- **Overlap check.** Filters in the active EQ narrower than W (common in AutoEQ presets) are flagged. The user chooses to keep each one in the preference layer, or drop it and let Flatiron's correction replace it.
- **Export modes.** *Correction only*: filters to stack after the existing EQ or in a separate instance. *Merged*: preference layer plus correction in one filter set, with the preamp computed over both.

### Pipeline (per ear)

1. **Reference.** D(f) = L̂(f) − E(f), where E is the perceived-level shape a reference-correct headphone would produce: the negated diffuse-field equal-loudness contour at the assumed listening level (default 60 phon), with its mean removed over the range. Build it as ISO 226 (frontal free field) plus the diffuse-minus-free-field difference in reference thresholds from ISO 389-7. Options: ISO 226 free field, no reference, or a user-supplied CSV.
2. **Baseline.** Fit a smooth baseline to D that can't follow anything narrower than about twice the maximum feature width W (default W = 1 octave; options 1/3 to 2 octaves). Use a penalised cubic spline in log2-frequency with knots every 2W, refit with robust (Huber) weights so the features themselves don't drag it. Gaussian smoothing is out, because it eats about half the depth of features close to its width.
3. **Residual.** R(f) = D(f) − baseline(f), with its SD carried through.
4. **Significance mask.** Narrow features (under 1/3 octave): keep regions where |R| > k·SD (default k = 2) over at least two adjacent grid points. Broad features: test the mean residual over the feature's width against its SD, with k\_broad = 2.5 (configurable). Everything else gets a target of 0 dB.
5. **Target.** C(f) = −R(f), with cuts limited to −12 dB. Boosts are capped at +4 dB for narrow features (often fit-dependent cancellations) and +6 dB for broad ones; both caps are configurable. A strength control (0 to 100%, default 100%) scales the broad part of the target only.
6. **Taper.** Fade C to 0 over 1/3 octave outside \[f\_lo, f\_hi\], so nothing is corrected above 14 kHz.
7. **Fit filters.** Greedy initialisation: place a peaking filter at the largest |C| feature, then repeat on what's left. Then do a joint bounded least-squares fit (SciPy `least_squares`) over every filter's (Fc, gain, Q). Evaluate on a 1/96-octave grid, weighted by 1/SD². Bounds: Fc within range, Q from 0.7 to 16, |gain| ≤ 12 dB. Drop any filter under 0.5 dB.
8. **Stop** when the weighted residual is within noise (χ² per point ≤ 1.5) or the band budget is used up.
9. **Preamp.** Compute the combined response (Flatiron filters plus any merged broad EQ) from 20 Hz to 20 kHz. Set the preamp to −max(0, peak) − 0.5 dB.

### Band budget

- Default: 10 correction bands per channel. The maximum is 32 minus the preference-layer bands when exporting merged to LSP.
- The preference layer is imported per channel from Equalizer APO / AutoEQ text and edited outside Flatiron. In merged mode its bands come first, then correction bands sorted by frequency. Exports refuse to go over 32 bands per channel when the LSP target is selected.
- When the budget binds, keep the filters that reduce weighted error the most, and report what was left out.

## Export formats

Four writers cover all three targets. EasyEffects JSON is the route into Equaliser, and APO text doubles as an input for PipeWire and REW.

| Target | File written | Per-channel mechanism | Band limit | Q expressed as |
| --- | --- | --- | --- | --- |
| PipeWire filter-chain, builtin `param_eq` | Drop-in `.conf` for `~/.config/pipewire/pipewire.conf.d/` | `filters1` (L) and `filters2` (R) | No hard limit found; Flatiron caps at 32 | Q |
| LSP Parametric Equalizer x32 LeftRight | LSP settings `.cfg` | Separate left/right port sets | 32 per channel | Q (mode-dependent; verify) |
| Equaliser (macOS, cvknage/equaliser) | EasyEffects preset `.json` | `split-channels: true`, `left` / `right` objects | 64 per channel | `q` in JSON; the app shows bandwidth (verify conversion) |
| Equalizer APO / AutoEQ text | `flatiron_L.txt`, `flatiron_R.txt` | One file per channel | — | Q |

### PipeWire

`param_eq` takes either a filename or a `filters` array. `filenameX` or `filtersX` (X = channel 1 to 8) give per-channel configs. Its filter objects take `type`, `freq`, `gain` and `q`, with types such as `bq_peaking` ([PipeWire filter-chain docs](https://docs.pipewire.org/devel/page_module_filter_chain.html)). Sketch of the generated drop-in:

```
context.modules = [
  { name = libpipewire-module-filter-chain
    args = {
      node.description = "Flatiron resonance EQ"
      audio.channels = 2
      audio.position = [ FL FR ]
      filter.graph = {
        nodes = [
          { type = builtin name = eq label = param_eq
            config = {
              filters1 = [ { type = bq_peaking, freq = 6150.0, gain = -4.2, q = 6.5 } ]
              filters2 = [ { type = bq_peaking, freq = 7020.0, gain = -3.1, q = 8.0 } ]
            }
          }
        ]
        inputs  = [ "eq:In 1" "eq:In 2" ]
        outputs = [ "eq:Out 1" "eq:Out 2" ]
      }
      capture.props  = { node.name = "effect_input.flatiron" media.class = Audio/Sink }
      playback.props = { node.name = "effect_output.flatiron" node.passive = true }
    }
  }
]
```

Preamp: write it as an extra filter if `param_eq` accepts a broadband-gain spelling. If it doesn't, use the file variant (`filename1` / `filename2` pointing at the APO text files), which carries a `Preamp:` line. Also offer an "APO files only" mode for users who already have a filter-chain config.

### LSP Parametric Equalizer x32 LeftRight

Per-band controls exist separately for left and right: filter type, mode, frequency, gain and Q. GStreamer's listing names them like `fl-20` (Frequency Left 20) and `fml-0` / `fmr-0` (Filter mode Left/Right 0) ([GStreamer LV2 listing](https://gstreamer.freedesktop.org/documentation/lv2/lsp-plug-in-plugins-lv2-para-equalizer-x32-lr.html)). The `.cfg` writer fills bands 0 to N−1 per side, sets type Bell and slope x1, and turns unused bands off.

- Key spelling in `.cfg` files (underscore vs hyphen), gain units (dB vs linear) and the off/Bell enum values must come from a golden file. The user exports one from their own LSP install with known settings (see Open questions).
- Mode: pick the mode whose bell matches the RBJ peaking filter, if the installed version has one. Otherwise use RLC (BT), and have the validation step measure the mismatch and refit Q to compensate.

### Equaliser (macOS)

Equaliser has independent left/right EQ, up to 64 parametric bands, and imports both EasyEffects presets and REW filter settings. It needs macOS 15 or later on Apple Silicon ([cvknage/equaliser](https://github.com/cvknage/equaliser)). Flatiron writes an EasyEffects-format preset. That format carries `left` and `right` band objects with `frequency`, `gain`, `q`, `type` ("Bell"), `mode` (for example "RLC (BT)") and `slope` ("x1"), plus `split-channels` and `num-bands` ([example preset](https://gist.github.com/ryuukk/1702e352b4de3217cad1e971cb46e181)). Because EasyEffects' equalizer uses LSP-style band parameters, the same file also works in EasyEffects on Linux.

- Verify that Equaliser's importer honours `split-channels` and maps `q` correctly to its bandwidth control. Fallback: two REW-format text imports, one per channel.

### Equalizer APO / AutoEQ text

One file per channel: a `Preamp: -X dB` line, then lines like `Filter 1: ON PK Fc 6150 Hz Gain -4.2 dB Q 6.50`. PipeWire `param_eq` reads this format directly through `filename` / `filenameX`, and REW-style importers accept it too.

## Architecture, stack, data model

Recommended: a Python 3.11+ package with a keyboard-driven terminal UI, running on Linux (PipeWire) and macOS (CoreAudio) unchanged.

### Stack

| Concern | Choice | Why |
| --- | --- | --- |
| Playback | `sounddevice` (PortAudio) | One API across PipeWire/ALSA/JACK and CoreAudio; exclusive device choice |
| DSP and fitting | `numpy`, `scipy` (`signal.sosfilt`, `optimize`) | Biquads, MAP fit, bounded least squares |
| Trial UI | `textual` TUI | Instant key response, no browser audio stack in the way |
| Plots and report | `matplotlib` → PNG + a static HTML report | Curves with SD bands, filter overlays |
| Packaging | `pyproject.toml` + `uv`; `pytest` | Reproducible local install |

### Modules

- `audio/`: device selection, the output stream, stimulus synthesis, limiter, and the preview EQ (RBJ peaking biquads as SOS).
- `session/`: the trial scheduler, adaptive selector, catch trials, break timer and persistence.
- `model/`: the probit MAP fit, Laplace SDs, grid refinement and joint multi-session fits.
- `fit/`: baseline, residual, significance mask, filter optimiser, budget handling and preamp.
- `export/`: `pipewire.py`, `lsp.py`, `easyeffects.py`, `apo.py`, each with golden-file tests.
- `report/`: plots and the HTML summary.
- `cli.py`: subcommands `devices`, `level`, `sweep`, `run`, `fit`, `preview`, `export`, `report`.

### Data on disk

Everything lives under `~/.local/share/flatiron/` (Linux) or `~/Library/Application Support/flatiron/` (macOS), as plain JSON.

- `profiles/<headphone-slug>/profile.json`: headphone label, fit settings, and the latest fitted curves and filters per ear.
- `profiles/<headphone-slug>/sessions/<timestamp>.json`: device, sample rate, EQ-context note, stimulus settings, level, RNG seed, grid, and every trial (ear, f\_a, f\_b, Δ\_a, Δ\_b, play order, rove, response, response time, replays, catch flag).
- `profiles/<headphone-slug>/exports/`: generated configs, named by target and fit hash.

Fits and exports are pure functions of the session files plus the settings, so re-running `fit` with a different detrend width needs no new listening.

The perceptual fit (L̂ and its SD) depends only on the trials, so it's cached. Every correction setting downstream of it can be changed freely:

| Setting | Change without new trials? | Limit |
| --- | --- | --- |
| Max feature width W | Yes | Features wider than about an octave are only as well constrained as the longest jumps measured (1 octave) |
| Broad-feature strength | Yes | — |
| Reference contour and listening level | Yes | — |
| Significance thresholds k, k\_broad | Yes | — |
| Boost/cut caps, Q bounds, band budget | Yes | — |
| Correction range f\_lo to f\_hi | Yes, inside the measured range | Going below about 1 kHz needs new trials there |
| Which sessions are included | Yes | — |
| Measured range, grid, stimulus type and bandwidth, jump lengths | No | Fixed when the trials are collected |

## UI and session flow

The whole loop runs from the keyboard: set up once, run several short reseat sessions, fit, audition, then verify with the EQ applied.

&#91;embedded content: session flow · 6 steps, 1 decision\]

Each verify pass either confirms the correction or feeds a small incremental refit back into the fit step.

### Setup (once per headphone and device)

1. **Device check.** List output devices and flag Bluetooth, mono, or sample-rate mismatches. Recommend a wired output at 48 kHz.
2. **Channel check.** Play a burst in L, then in R. The listener confirms which side they heard.
3. **Level.** Loop a 4 kHz band at −40 dBFS while the listener raises system volume to a comfortable, moderate level. Store the setting and remind the listener to keep the volume fixed for every session.

### Trial screen

- Two large panels, A and B, each lighting up while its interval plays.
- Keys: ← or 1 = A louder, → or 2 = B louder, Space = replay, P = pause, Q = save and quit.
- Show progress, trials left in the block and the break timer. Don't show the curve during trials, so the listener isn't biased.
- Reaction time is logged for every trial; very fast answers (under 150 ms after B ends) are flagged.

### Between sessions

- Prompt the listener to take the headphones off, put them back on, and rerun the channel check.
- A short quick-look plot is available on request only.

### Fit and audition

- A report per ear: L̂ with its SD band, the reference, the baseline, the residual with its significance mask, the fitted filters and the predicted corrected curve. Broad and narrow corrections are listed separately. A per-session overlay shows whether features hold up across reseats.
- **Tune mode:** live controls for W, broad strength, reference, k and caps. Each change reruns only the correction pipeline on the cached fit (target: under 1 s) and updates the preview EQ immediately, so the effect can be A/B'd on music right away. Settings save as named presets per headphone, and export uses the active preset.
- Audition: toggle Flatiron's preview EQ on and off (preamp applied to both states, so they're level-matched). Sources: a slow narrowband sweep, pink noise, or a WAV the user supplies.

### Verify

A short session (about 150 trials) with the preview EQ applied to every stimulus. The residual should come out flat within noise. Any significant leftovers can be fitted again as an increment, within the band budget.

Verify trials record the exact preview filters that were applied. The model adds their known gain at each stimulus frequency, so verify trials pool with ordinary ones and stay valid after the tune settings change.

## Validation and test plan

Prove the method on a simulated listener before any real listening, then prove each exporter by measuring what the target actually does.

### Simulated listener (before any real listening)

- Ground truth per ear: a smooth tilt, plus the ISO 226 contour, plus four features. For example: a +4 dB excess 1/2 octave wide at 4 kHz, a −5 dB peak at 6.2 kHz (Q 6), a −3 dB dip at 8.4 kHz (Q 10), and a +3 dB peak at 11 kHz (Q 4).
- Simulated responses use the same probit model, with σ₀ = 1 dB, d₀ = 0.5 octave and λ = 3%.
- Pass criteria: each feature recovered within 1/24 octave in frequency and 1 dB in depth, with no false features above 1 dB, after 600 trials per ear or fewer. When the simulated ear matches the reference exactly, nothing should be corrected in the ear-gain region.
- Robustness: add individual deviations from ISO 226 (±3 dB, about an octave wide) and report how much of them shows up as broad correction. That number goes in the report as the broad-feature uncertainty.
- Use the simulator to tune the grid spacing, adaptive weights, refinement thresholds, k, k\_broad, W and the trial budget. Compare against uniform random pairs as a baseline.
- Test a reseat model: shift features by ±1/48 octave per session, and check that inconsistent ones get dropped.

### Unit tests

- Flatiron's peaking biquad matches the RBJ cookbook formulas to 0.01 dB.
- Each stimulus's spectrum stays inside its band (> 40 dB rejection one octave out), RMS is normalised, and the limiter never lets output exceed its ceiling.
- Filter fit recovers a target built from known peaking filters.
- Exporters match golden files byte for byte (fixtures from Open questions).

### Exporter truth checks (on the real targets)

- **PipeWire:** load the generated drop-in, play a log sweep through the filter sink, record the output via a loopback (`pw-record`), and compare with Flatiron's predicted response.
- **LSP:** load the `.cfg` in the user's host and measure the same way, or compare against LSP's own response graph at a dozen spot frequencies.
- **Equaliser:** import the JSON and compare the app's displayed curve (or a loopback capture, if available) for L and R separately.
- Tolerance: within 0.3 dB of prediction from 1 to 14 kHz. If a target's bell differs systematically (Q definition, transform), add a per-target Q mapping and retest.

### Real-listener checks

- Two full runs on different days for the same headphone: features that get corrected should appear in both.
- The verify session after correction shows no significant residual features.

## Milestones

Build in this order. The simulator comes before the trial UI, so the method is proven before anyone spends time listening.

1. **Audio core.** Device listing, a stream, stimulus synthesis, limiter, `flatiron devices` and `flatiron level`. Includes a manual probe mode (play a band at a chosen frequency and ear), which is useful on its own for hunting by hand.
2. **Model and simulator.** Probit MAP fit, Laplace SDs, the simulated listener, and recovery tests passing.
3. **Scheduler.** Adaptive pair and Δ selection, grid refinement and catch trials, all tuned against the simulator.
4. **Trial UI and persistence.** The `textual` trial screen, session JSON, breaks, reseat prompts and the optional sweep (phase 0).
5. **Fit pipeline.** Baseline, mask, filter optimiser, budget, preamp and the HTML report.
6. **Exporters.** APO text first, then PipeWire, EasyEffects/Equaliser and LSP, each with a golden file and a truth check.
7. **Audition and verify.** Preview EQ A/B, the verify session and incremental refits.

- [ ] User supplies the format fixtures listed below before milestone 6 starts.

## Open questions and things to verify locally

### Decisions for the user

- Will Flatiron itself run on the Linux machine, the Mac, or both? (It affects which truth checks are practical.)
- Terminal UI, or a small desktop window?
- Reference contour: diffuse-field (default, for a flat diffuse-field target) or free-field ISO 226? The ISO 389-7 diffuse/free-field threshold table is paywalled, so the values need sourcing before milestone 5.
- Should an optional interaural balance step (L vs R at the same frequency) feed into the correction, or stay off as it is by default?
- Default resonance budget per channel (10 proposed), and the boost caps (+4 dB narrow, +6 dB broad proposed).

### Fixtures to collect before writing exporters

- [ ] LSP x32 LeftRight: save a `.cfg` with band 0 = Bell, 1000 Hz, −3 dB, Q 2 (left) and band 1 = Bell, 5000 Hz, +2 dB, Q 4 (right), the rest off. This fixes key names, gain units and enum values. Note the LSP version and which filter modes it lists.
- [ ] EasyEffects: export a preset with split channels and two bands, as above.
- [ ] Equaliser: import that EasyEffects preset and confirm that L and R come in separately. Note how Q shows up as bandwidth, and export a native preset for reference.
- [ ] PipeWire: version (`pipewire --version`). Confirm `filters1` / `filters2`, and find out whether a broadband-gain filter is accepted in `param_eq`.

### Risks

- Broad corrections in the ear-gain region are only as good as the reference. Individual ears can sit several dB off ISO 226 there. Mitigation: a separate strength control, a separate report section, and a sanity check against a published measurement of the same headphone.
- Fit variability may swamp features above about 8 kHz. Mitigation: reseat sessions and the consistency mask. If it's still too variable, lower the upper limit or widen the stimuli.
- Fatigue makes later trials noisier. Mitigation: 15-minute caps, catch trials, and a lapse rate per session.
- Targets may differ in how they define Q. Mitigation: per-target truth checks and Q mapping.

### Sources

- [PipeWire filter-chain documentation](https://docs.pipewire.org/devel/page_module_filter_chain.html)
- [LSP Parametric Equalizer x32 LeftRight (GStreamer LV2 listing)](https://gstreamer.freedesktop.org/documentation/lv2/lsp-plug-in-plugins-lv2-para-equalizer-x32-lr.html)
- [Equaliser for macOS (cvknage/equaliser)](https://github.com/cvknage/equaliser)
- [EasyEffects preset example with left/right bands](https://gist.github.com/ryuukk/1702e352b4de3217cad1e971cb46e181)
