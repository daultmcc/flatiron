# Flatiron — by-ear headphone resonance EQ

Flatiron is a local tool that finds peaks and dips in a headphone's upper mids and treble, separately for each ear, using only the listener's "which is louder?" judgments. It fits a perceived-level curve per ear, picks out the features that are significant and narrower than about an octave, turns them into RBJ peaking filters, and exports them for an EQ host: Equalizer APO / AutoEQ text, LSP Parametric Equalizer x32 LeftRight, EasyEffects (and through it Equaliser on macOS), a PipeWire filter-chain drop-in, and TinCan.

This document describes how Flatiron works as built. The README covers day-to-day use.

## Scope

Flatiron does:

- Find features from about 1/12 octave wide up to a configurable maximum width W (default 1 octave), between 1 kHz and 13 kHz by default.
- Measure left and right independently. A feature in one ear doesn't leak into the other.
- Use a two-alternative forced-choice task ("A or B louder?"), never knob-matching.
- Report how confident it is in each feature, and correct only features it is sure of.
- Keep every raw trial, so the correction can be recomputed with different settings without retesting.

Flatiron doesn't:

- Correct broad tilt, bass or overall target curve. By default it keeps the headphone's broad shape and only removes narrow features (see Tonality).
- Flatten to equal perceived loudness.
- Do hearing-loss compensation or anything audiometric.
- Process system audio. It writes configs; an EQ host does the processing.

## Key design decisions

1. **Reference, then detrend.** What gets measured is headphone response + the listener's equal-loudness contour (ear gain included) + fit-dependent resonances. Flattening all of that would cut the listener's natural ear gain. Flatiron subtracts a reference contour, then removes anything that varies more slowly than W. What's left is a candidate for correction.
2. **Keep the tonality by default.** Broad features (wider than 1/2 octave) in the ear-gain region depend on the reference, and individual ears differ from ISO 226 by several dB there. By default Flatiron corrects narrow features only, including narrow features riding on a broad one, and adds broad compensation filters so the correction averages to zero over about W. The headphone's broad tonal balance stays as it was, and broad changes are left to the user's own EQ. `--broad` switches to correcting broad features toward the fixed target as well, scaled by a strength control.
3. **Compare neighbours, fit globally.** Comparing nearby frequencies is easier and less noisy than comparing far-apart ones. Every judgment goes into one maximum-likelihood fit per ear, which avoids the random-walk error of chaining neighbour comparisons. Judgment noise grows with frequency separation, so long-range comparisons count for less.
4. **Redundant comparison graph.** Each grid point is compared against points 1 to 3 steps away, plus about 25% longer jumps of up to an octave. The longer jumps pin down broad features that a chain of neighbour comparisons would smear out.
5. **Narrowband noise by default.** Pure sines inside headphones are very sensitive to seal and standing waves, and can fall between the peaks of a narrow notch. The default stimulus is 1/12-octave band noise; 1/24-octave noise, warble tones and pure tones are options. The model accounts for each trial's stimulus bandwidth, so sessions with different stimuli pool.
6. **Adaptive grid.** Start at 1/24 octave and refine to 1/48 where the fitted curve bends sharply (or around frequencies the listener marked), so narrow features don't fall between test points.
7. **Reseat and repeat.** Treble resonances move when the headphone moves. Sessions are short, with a reseat between them, and features whose level disagrees across sessions are dropped.
8. **One filter definition everywhere.** The fit, Flatiron's own preview EQ and every export use RBJ-cookbook peaking biquads. Each exporter writes the target's matching mode, and the formats that can be run locally are checked against the real implementation before they're written.
9. **Lazy fits from per-profile settings.** There is no separate fit step and no saved fit. Every command that needs the correction recomputes it from the profile's trials, settings and checkpoint. The expensive part, the perceptual fit, depends only on the trials and is cached, so a correction takes about 0.1 s once the cache is warm.

## Measurement

Each trial plays two short narrowband bursts in one ear and asks which was louder.

### Frequency grid

- Measured range 850 Hz to 15 kHz, a little past the default correction range (1 to 13 kHz) so the baseline and taper aren't distorted at the edges.
- Initial grid: 1/24-octave log spacing, about 100 points per ear.
- Refinement: log midpoints, down to 1/48 octave (see Adaptive selection). Each session saves its final grid per ear; the next session starts from it.

### Stimuli

| Parameter | Default | Options |
| --- | --- | --- |
| Type | Band-limited noise, 1/12 octave around the centre | 1/24-octave noise, warble tone (±1/24 octave FM at 8 Hz), pure tone |
| Noise token | Frozen per (frequency, seed); a new random seed per session | — |
| Burst length | 400 ms | — |
| Ramps | 20 ms raised cosine | — |
| Gap between A and B | 300 ms | — |
| Presentation | Monaural: one ear only, other channel digital silence | — |
| Sample rate | 48 kHz, float32 | — |
| Level | Set per session on the level screen (default −30 dBFS RMS, at most −24) | — |

Noise bands are generated in the frequency domain (shaped FFT, then inverse) for steep band edges, and every stimulus is normalised to unit RMS before any level is applied. A trial's whole A-gap-B buffer is scaled under a −12 dBFS ceiling (then hard-clipped there as a last guard), so the limiter never changes the A/B difference.

The system volume stays at the listener's usual setting. Each session opens on a level screen that loops bands across the range and adjusts Flatiron's own level, starting from the last session's. The level can be changed mid-session with a warning; changes are logged and every trial records its level.

Audio goes through one persistent PortAudio output stream with a fixed 40 ms buffer. PortAudio's "low" latency (about 9 ms) underflowed on PipeWire. The trial UI offsets its panel lights and reaction times by the stream's reported latency.

### Trial

1. The scheduler picks an ear (alternating), a pair of grid points and a level offset Δ, applied to one randomly chosen interval.
2. Which stimulus plays first is random, and a whole-trial level rove (±2 dB) keeps the listener from anchoring on absolute level.
3. A, gap, B play; the listener answers A or B. Replays are allowed and logged. The answer can be undone (the trial is kept on file, marked undone, and left out of fits).
4. About 5% of trials are catch trials: the same frequency with a ±6 dB offset. They track attention.
5. Reaction time is logged from the end of B. Answers under 150 ms are flagged.

### Perceptual model

Per ear, the unknowns are the perceived levels L at the grid points (dB), a time-order bias b, and the noise parameters σ₀, d₀ and λ:

```latex
P(A) = \frac{\lambda}{2} + (1 - \lambda)\,\Phi\!\left(\frac{\bar L_a - \bar L_b + o + b\cdot s}{\sigma(d)}\right),
\qquad \sigma(d) = \sigma_0 \sqrt{1 + (d/d_0)^2}
```

- o is every known level difference between A and B: Δ\_a − Δ\_b, plus the gain of any EQ in effect (an active system EQ recorded for the session, or the preview EQ of a verify session).
- L̄ is L averaged in dB over the trial's stimulus spectrum (11 points across a noise band; one point for tones), interpolated linearly in log-frequency from the grid.
- s = +1 when A played first and −1 when B did; b is how many dB louder the first interval sounds. (The user's real data showed a strong first-interval preference, about +1 dB.) b has a N(0, 3 dB) prior and is a fixed effect, so its uncertainty is part of L's error bars.
- d is the pair's separation in octaves.
- Priors: smoothness, as a grid-independent curvature penalty equivalent to a 1 dB SD on second differences at 1/24 octave; a very weak N(0, 100 dB) prior on each level so the fit is well-posed with few trials; log-normal priors around σ₀ = 1 dB and d₀ = 0.5 octave; λ around 2%. The gauge is fixed by mean(L) = 0.
- σ₀, d₀ and λ are set by maximising the Laplace-approximated marginal likelihood with L integrated out (empirical Bayes, Nelder–Mead started from both the prior means and the warm start). A joint MAP over L and the noise overfits: with a few trials per point, L separates the answers and σ₀ collapses. Below 200 trials per ear the noise stays at the prior, because the evidence underestimates σ₀ with fewer trials. L is then the MAP given the noise.
- Error bars come from the Laplace approximation (inverse Fisher information), projected onto the gauge. Every displayed curve carries a ±1 SD band.
- All of an ear's sessions are fitted jointly, with no per-session offset (it would cancel within an ear). Each session with at least 60 trials in an ear is also fitted on its own, with the noise held at the joint fit's values, for the consistency check.

### Adaptive selection

- Candidate pairs: up to 3 grid steps apart, or (with probability 0.25) a longer jump of up to one octave.
- Each candidate is scored by how much one answer would shrink the posterior variance of L\_i − L\_j, weighted up where the curve bends (by 0.5 per SD of curvature, capped at 4 SD). Pairs in the same quarter-octave region as the previous trial are skipped. The pair is drawn at random from the top 8.
- Δ aims at the 50% point as the listener hears it, time-order bias included, plus jitter of ±1 posterior SD of the difference (at most 4 dB). |Δ| is capped at 12 dB.
- L is refitted after every answer with the noise fixed. Every 50 trials per ear, the full fit (noise included) is redone, and a midpoint is inserted on both sides of every point whose second difference exceeds 2 SD, down to 1/48 octave.

### Sweep and tone-generator marks

Two optional ways to tell the scheduler where to look:

- **Sweep** (`run --sweep`, first session only): a slow narrowband sweep (a cluster of nine tones spread over ±1/24 octave) from 1 to 14 kHz and back, 30 s each way by default. The listener taps at each peak or dip. Up and down taps are paired and averaged, which cancels reaction-time lag, and the grid is refined around them. If the profile already has a fit, the sweep plays through it (Q toggles it), so what's left is what the correction hasn't fixed.
- **Tone generator** (`tone`): a continuous tone or noise band, glided by hand. Marks made there are saved in the profile and refine the grid of the next trial session that starts (each mark is stamped with that session, so it's used once).

## From measurements to filters

### Pipeline (per ear)

1. **Reference.** D(f) = L̂(f) − E(f), where E is the perceived-level shape a reference-correct headphone would produce, with its mean removed. Options: `iso226` (default: the negated ISO 226 frontal free-field contour at 60 phon, configurable), `diffuse` (ISO 226 plus a diffuse-minus-free-field threshold difference, read from a user-supplied CSV, because the ISO 389-7 table isn't openly available), `none`, or a CSV of perceived-level shape.
2. **Baseline.** A penalised cubic B-spline in log2 f with knots every 2W. The curvature penalty sets its stiffness, so it scales as W⁴ (0.1 relative to the data weight at W = 1 octave); a smoothing spline's scale goes as the penalty's fourth root. It is fitted by iteratively reweighted least squares: Huber weights to convergence, then Tukey bisquare, so features don't drag it. The final smoother is linear in the data, so the residual's covariance follows exactly.
3. **Residual.** R = D − baseline, with its full covariance.
4. **Features.** R is split into lobes between sign changes, and a lobe is split again at any valley no deeper than half the smaller peak on either side. A lobe narrower than 1/2 octave at half height is *narrow*: it's kept if at least two adjacent points exceed k·SD (k = 2). A wider lobe is *broad*: it's kept if its mean exceeds k\_broad times the SD of that mean (k\_broad = 2.5). Features outside \[f\_lo, f\_hi\] are dropped.
   **Inside broad regions.** A broad lobe that isn't corrected as a whole (kept as tonality, the default, or not significant) can still carry narrow features. It's searched again: together with the same-sign pieces it was split from at a valley that stays clear of zero (beyond k SD), so the flank beside a narrow dip isn't taken for a feature of its own. The region's residual is taken against a Gaussian-weighted local linear smooth over the region alone (FWHM 1/2 octave; local linear, so it doesn't lag at the region's ends), and every lobe of that is given the narrow test. Features found this way are corrected against the local smooth, with its (smaller) error bars, and checked for consistency the same way.
5. **Consistency.** With two or more sessions in the check (each with at least 60 trials in the ear), each kept feature's mean over its half-height core is compared across sessions (Cochran's Q). A feature with p < 0.01 is dropped as fit-dependent.
6. **Target.** C = −R over kept features, cuts limited to 12 dB. Boosts are capped at +4 dB for narrow features (often fit-dependent cancellations) and +6 dB for broad ones. With `--broad`, the broad part is scaled by the strength (0 to 100%, default 100%). Without it, broad features aren't in the target at all.
7. **Taper.** C fades to 0 with a raised cosine over up to 1/3 octave beyond \[f\_lo, f\_hi\], limited by the measured margin.
8. **Filters.** On a 1/96-octave grid, weighted by 1/SD: greedily place a peaking filter on the largest weighted error (Q from its half-height width), then refit all filters jointly with bounded least squares over (log Fc, gain, log Q). Bounds: Fc within \[f\_lo, f\_hi\], Q 0.7 to 16, |gain| ≤ 12 dB. Stop when no point is off by more than 2 SD, or the band budget (default 20 per channel, at most 32) is used up. Filters under 0.5 dB are dropped and the rest refitted. If the budget binds, the frequency ranges still off by more than k SD are reported.
9. **Tonality compensation** (the default). Broad peaking filters (Q ≤ 1.5, at most 4, from the bands left over) are fitted to the negative of the narrow filters' own response, smoothed over W. The corrected response then keeps the original broad shape to within about 0.5 dB. Filters at nearly the same frequency and Q are merged.
10. **Preamp.** −max(0, peak) − 0.5 dB, where peak is the highest point of the combined response from 20 Hz to 20 kHz. It's one value for both channels, which keeps the L/R balance.

### Settings

Every setting is downstream of the cached perceptual fit, so changing it needs no new trials. Settings are stored per profile (`flatiron config`) as only the values that differ from the defaults, so improved defaults reach existing profiles.

| Setting | Default | `config` option |
| --- | --- | --- |
| Max feature width W | 1 octave (1/3 to 2) | `-w` |
| Reference, listening level | `iso226`, 60 phon | `-r`, `--phon` |
| Significance k, k\_broad | 2, 2.5 SD | `-k`, `--k-broad` |
| Broad correction, strength | off, 100% | `--broad`, `-s` (needs `--broad`) |
| Band budget | 20 per channel | `-b` |
| Boost caps, cut cap | +4 narrow, +6 broad, −12 dB | `--boost-narrow`, `--boost-broad`, `--max-cut` |
| Correction range | 1 to 13 kHz | `--f-lo`, `--f-hi` |
| Reseat consistency check | on | `--consistency` / `--no-consistency` |
| Sessions included | all | `--sessions` |
| Other profiles pooled in | none | `--pool` |
| Method | features | `--method features\|smooth`, `--smooth-width` |

The correction range can only move within the measured range, and the measured range, grid and stimulus are fixed when the trials are collected. Features wider than about an octave are only as well constrained as the longest jumps (1 octave).

### Pooling

`config --pool OTHER` includes another profile's trials in this profile's fits: the same headphones measured with a different stimulus, for example. The model already accounts for each trial's stimulus, so the sessions pool directly. They stay in their own profile; new sessions there are included as they're run. Trial sessions still adapt to the profile's own trials only.

### Experimental: the smooth method

`config --method smooth` skips feature detection. The baseline is the curve smoothed with a Gaussian-weighted local linear fit over `--smooth-width` (FWHM, default 1/2 octave), and the whole residual within \[f\_lo, f\_hi\] is the target, with the usual caps, tonality compensation and 2 SD filter-fit stop. It corrects more, but with no significance test it also corrects noise: on noise drawn from real fits' error bars it fits about 3 filters per ear (1.5 to 3 dB), against about 0.8 for the features method. A smoothing window also takes part of any feature near its own width into the baseline (about 45% of a 1/4-octave resonance at 1/2 octave), which under-corrects it. The features are still reported, as what's significant, and verify uses them.

### Checkpoints

`config --checkpoint` fixes the current filters per ear. Later fits run the pipeline on the corrected curve (L̂ plus the fixed filters' response) and only add filters for what's still significant, within the bands left over. This holds whatever the settings become. `--reset-checkpoint` drops it. The usual use is to checkpoint before a verify session, so its results add to the filters that were verified instead of reshuffling them.

### Measurement context

Flatiron treats EQ as three layers: any system EQ active while measuring, the correction Flatiron fits, and an optional preference layer the user owns.

- **Active EQ is known, not measured.** A session can record the system EQ in effect (`run --active-eq`, as APO text). Its gain at each stimulus frequency enters the model as a known offset, so L̂ always describes the raw headphone.
- **Export modes.** *Correction only* (default): filters to stack after the existing EQ or in their own instance. *Merged* (`export -m`, or `--merge-left`/`--merge-right`): the preference layer's APO text first, then the correction sorted by frequency, with the preamp computed over both.

## Audition and verify

- **Preview EQ.** Flatiron's own software copy of a correction, built from the same export plan as the exporters, so what's auditioned or verified is what an export does.
- **Audition** (`audition`): pink noise, the slow sweep, or the user's WAV files, with Space flipping the correction on and off at the same playback position. The preamp applies to both states, so they're level-matched. A blind mode hides which is which until revealed.
- **Verify** (`verify`): a short session (150 answers by default) with the preview EQ applied to every burst. Each burst's level change from the EQ is recorded as a known offset, so verify trials pool with ordinary ones and stay valid if the settings change later. Afterwards Flatiron refits with the applied filters fixed and reports any feature still significant.

## Tone generator

`tone` is a real-time signal generator for hunting by hand. It renders block by block in the audio callback:

- Stimuli: pure tone, warble, and 1/24, 1/12 and 1/3-octave noise. Noise is white noise through a per-block 2nd-order Butterworth band-pass whose state carries over, normalised by its equivalent noise bandwidth.
- Frequency (50 Hz to 18 kHz) glides with a 20 ms one-pole smoother in log-frequency. Level, ear, correction on/off and pauses ramp over 10 ms; a stimulus change fades out, swaps and fades in.
- Motion: terminals report no key releases, so a key counts as held while its auto-repeats keep arriving (a gap over 0.12 s means it was released). A tap steps by speed × 1/8 s. Holding glides at the speed (1/48 to 4/3 octave/s), accelerating smoothly up to 3× after half a second.
- With `-p`, the profile's correction (without its preamp) is applied per channel and can be toggled, and P/D marks are saved to the profile (see Sweep and tone-generator marks).

## Export formats

All exporters work from one export plan: per-channel filter lists and a preamp. LSP and EasyEffects output is checked against LSP's own LADSPA build before writing, when it's installed. PipeWire output can be checked with `--check`. The tolerance is 0.3 dB from 1 to 14 kHz.

| Target | File | Per-channel mechanism | Limit | Checked against |
| --- | --- | --- | --- | --- |
| Equalizer APO / AutoEQ text | `<name>_L.txt`, `<name>_R.txt` | One file per channel | — | — |
| LSP Parametric Equalizer x32 LeftRight | LSP settings `.cfg` | Separate left/right bands | 32 per channel | LSP LADSPA, offline |
| EasyEffects (and Equaliser, macOS) | Preset `.json` | `split-channels`, `left`/`right` | 32 per channel | LSP LADSPA, offline |
| PipeWire filter-chain, builtin `param_eq` | Drop-in `.conf` | `filters1` (L), `filters2` (R) | — | A private filter-chain (`--check`) |
| TinCan | Preset `.toml` | `channel` per filter | 256 filters | — |

By default files are written to the current directory, named `<profile>-<fit hash>`. TinCan presets go straight into TinCan's preset folder. A folder that doesn't exist is an error, never created. `--preamp safe|DB` overrides the preamp (`0` keeps the uncorrected level for A/B comparison; a warning is printed if boosts can then clip).

- **APO text.** A `Preamp:` line, then `Filter n: ON PK Fc … Hz Gain … dB Q …`. PipeWire `param_eq` reads it through `filename1`/`filename2`, and REW-style importers accept it.
- **LSP.** Written over a template exported from LSP 1.2.35, changing only band, gain and mode lines. Bands use filter mode 6, "APO (DR)", which matches the RBJ filters within 0.01 dB (the default RLC (BT) mode differs by up to 1.7 dB on narrow bells). The preamp is the input gain; LSP doesn't auto-balance.
- **EasyEffects.** Written over an EasyEffects 7 split-channel preset, with bands in "APO (DR)" mode and the preamp as input gain. EasyEffects runs LSP's equalizer, so the same LADSPA check applies. Equaliser (macOS) imports this format; how it maps Q and mode on import hasn't been verified.
- **PipeWire.** A filter-chain module with `param_eq` filters inline, `bq_peaking` (and shelves) in RBJ form. The preamp is a `bq_highshelf` at frequency 0, which is a broadband gain. Installed by copying it to `~/.config/pipewire/pipewire.conf.d/` and restarting PipeWire, which adds a "Flatiron <profile>" sink. `--check` plays a log sweep through a private copy (virtual sink and source, nothing on real devices) and compares the recording with the prediction.
- **TinCan.** TinCan's preset format 1: `format`, `name`, and `[[filter]]` tables with `type`, `channel` (L, R or LR), `frequency`, `gain`, `q` and `enabled`. TinCan's bells are RBJ peaking filters. Presets carry no preamp, since TinCan sets one for everything loaded, so `--preamp` is refused. A filter identical in both channels (e.g. a merged preference shelf) is written once as LR. The preset is named "<profile> Flatiron <fit hash>", saved under TinCan's file-name rule into `$XDG_DATA_HOME/tincan/presets/`, or written to `-o FILE` for TinCan's import. Values outside TinCan's ranges are refused rather than clamped.

## Architecture

A Python 3.11+ package with a keyboard-driven terminal UI, managed with `uv`. It's intended to run unchanged on Linux and macOS.

| Concern | Choice |
| --- | --- |
| Playback | `sounddevice` (PortAudio) |
| DSP and fitting | `numpy`, `scipy` |
| UI | `textual` |
| Report | `matplotlib` figures embedded in a static HTML page |
| Tests | `pytest` |

### Modules

- `audio/`: biquads, stimuli, limiter, device listing, the persistent output stream and buffer mixer, the tone generator's synth, and the preview EQ.
- `model/`: grids, the probit fit, and grid refinement.
- `session/`: the adaptive scheduler, the trial runner (scheduler + stimuli + session file, without UI), storage, and the sweep.
- `fit/`: reference, baseline, features, filter fitting, the correction pipeline, and the per-profile fit (cache, settings, pooling, checkpoint).
- `export/`: the export plan, the five writers, the LADSPA host and the truth checks.
- `report/`: figures and the HTML report.
- `ui/`: the trial app (level, channel check, sweep, trials, end screen), audition, and the tone generator.
- `sim/`: the simulated listener, non-adaptive designs, and validation.
- `cli.py`: `devices`, `tone`, `run`, `verify`, `audition`, `profiles`, `sessions`, `config`, `report`, `export`, `simulate`.

### Data on disk

Everything lives under `~/.local/share/flatiron/` (Linux, or `$XDG_DATA_HOME`) or `~/Library/Application Support/flatiron/` (macOS), as JSON written atomically.

- `profiles/<slug>/profile.json`: label, settings (only changes from the defaults), the sessions used (if not all), profiles pooled in, the checkpoint (filters per ear, when it was made and on how many trials), and tone-generator marks.
- `profiles/<slug>/sessions/<timestamp>.json`: device, sample rate, EQ context (note and APO text), stimulus, level and level changes, gap, seed, scheduler settings, final grid per ear, channel checks, sweep marks and the correction it played through, tone marks used, the preview EQ for verify sessions, and every trial. A trial records the ear, both frequencies, Δ for each, play order, rove, answer, reaction times, replays, the fast flag, known EQ gains, level, and the undone and retake flags. The file is saved after every answer.
- `profiles/<slug>/cache/`: perceptual fits (`probit-<hash>.npz`, keyed by trials, grid, priors and model version) and the last HTML report.

## Session flow

1. **Setup.** `devices` flags Bluetooth, mono and sample-rate problems.
2. **Each session** (`run`, at most 15 minutes, 12 by default):
   - The level screen.
   - The channel check: a burst in each ear in random order, and the listener says which side it was in.
   - The sweep (first session, if asked).
   - Trials until time is up or the listener quits.
   - The end screen asks for a reseat before the next session.

   The fitted curve is never shown during trials. Trial keys: ←/1 = A louder, →/2 = B louder, Space = replay, U = undo, P = pause, L = level, Q = save and quit, X = discard the session.
3. **Look** (`report`, `report -H`): per ear, L̂ with its SD band, the reference, the baseline, the residual with its significance, the features (kept or not, and why), the filters, and the predicted corrected curve. With two or more sessions, each session's residual is drawn against the pooled one; above five, the band holding the middle 90% of them, weighted by trials. Checkpoint filters are labelled.
4. **Adjust** (`config`), **listen** (`audition`), **verify** (`verify`, ideally after `config --checkpoint`), and **export**.

## Validation

### Simulated listener

A simulated listener exercises the scheduler, perceptual fit and correction pipeline. `flatiron simulate` reports how well the perceptual fit recovers it, and `sim/validate.py` checks the criteria below end to end. The ground truth per ear is a smooth tilt, plus the ISO 226 contour, plus a +4 dB excess 1/2 octave wide at 4 kHz, a +5 dB peak at 6.2 kHz (Q 6), a −3 dB dip at 8.4 kHz (Q 10) and a +3 dB peak at 11 kHz (Q 4). Answers come from the same probit model, with σ₀ = 1 dB, d₀ = 0.5 octave and λ = 3%.

The criteria: each feature recovered within 1/24 octave in frequency and 1.5 dB in depth, and no false correction above 1.5 dB, after 600 trials per ear. The depth tolerances were relaxed from 1 dB, since the listener's own loudness discrimination is the practical limit. These aren't all met yet: very narrow features (Q 12 to 16) and those near the top of the range (around 11 kHz) come out about 1 to 2 dB shallow, and a null ear can get stray filters up to about 1.3 dB.

### Unit and truth tests

- The peaking biquad matches the RBJ cookbook.
- Stimuli stay in band at unit RMS, and nothing leaves above the ceiling.
- The filter fit recovers known filters, and the tone synth glides and ramps without steps.
- Exporters match golden files from real LSP and EasyEffects installs byte for byte.
- When available locally, the LSP and EasyEffects output runs through LSP's LADSPA plugin, and the PipeWire drop-in through a private filter-chain, compared with Flatiron's prediction.

## Known limitations

- Broad corrections (with `--broad`) are only as good as the reference. Individual ears can sit several dB off ISO 226 in the ear-gain region, which is why tonality is kept by default.
- Fit variability above about 8 kHz is high. Reseats and the consistency check mitigate it; the correction range stops at 13 kHz by default and shouldn't go below 12 kHz.
- The diffuse-field reference needs a user-supplied ISO 389-7 difference table.
- Equaliser's (macOS) handling of imported Q and filter mode is unverified.

### Sources

- [PipeWire filter-chain documentation](https://docs.pipewire.org/devel/page_module_filter_chain.html)
- [LSP Parametric Equalizer x32 LeftRight (GStreamer LV2 listing)](https://gstreamer.freedesktop.org/documentation/lv2/lsp-plug-in-plugins-lv2-para-equalizer-x32-lr.html)
- [Equaliser for macOS (cvknage/equaliser)](https://github.com/cvknage/equaliser)
- [EasyEffects preset example with left/right bands](https://gist.github.com/ryuukk/1702e352b4de3217cad1e971cb46e181)
