# Flatiron — by-ear headphone resonance EQ

Flatiron is a local tool that finds peaks and dips in a headphone's upper mids and treble, separately for each ear, using only the listener's "which is louder?" judgments. It fits a perceived-level curve per ear, picks out the features that are significant and narrower than about an octave, turns them into RBJ peaking filters, and exports them for an EQ host: Equalizer APO / AutoEQ text, LSP Parametric Equalizer x32 LeftRight, EasyEffects (and through it Equaliser on macOS), a PipeWire filter-chain drop-in, and TinCan.

This document describes how Flatiron works as built, plus the design of the one part that isn't built yet. The README covers day-to-day use.

**Anything not implemented is marked NOT BUILT**, so `grep 'NOT BUILT' DESIGN.md` finds every instance. Everything else describes the code as it stands.

| Part | Status | Where |
| --- | --- | --- |
| Seating as a hierarchical model | Built | Perceptual model; pipeline step 5 |
| Profile comparison | Built | Comparing profiles |
| Consensus reference, veto, match method | Built | Consensus reference; Veto against other profiles; Experimental methods |
| Speaker sessions | **NOT BUILT** | Speaker profiles |
| Speaker evidence in a joint model | **NOT BUILT** | Speaker profiles |

## Scope

Flatiron does:

- Find features from about 1/12 octave wide up to a configurable maximum width W (default 1 octave), between 1 kHz and 13 kHz by default.
- Measure left and right independently. A feature in one ear doesn't leak into the other.
- Use a two-alternative forced-choice task ("A or B louder?"), never knob-matching.
- Report how confident it is in each feature, and correct only features it is sure of.
- Keep every raw trial, so the correction can be recomputed with different settings without retesting.
- Compare several headphones on one listener, to separate what belongs to the listener from what belongs to one headphone.

Flatiron doesn't:

- Correct broad tilt, bass or overall target curve. By default it keeps the headphone's broad shape and only removes narrow features (see Tonality).
- Flatten to equal perceived loudness.
- Do hearing-loss compensation or anything audiometric.
- Process system audio. It writes configs; an EQ host does the processing.

## Key design decisions

1. **Reference, then detrend.** What gets measured is headphone response + the listener's equal-loudness contour (ear gain included) + fit-dependent resonances. Flattening all of that would cut the listener's natural ear gain. Flatiron subtracts a reference contour, then removes anything that varies more slowly than W. What's left is a candidate for correction.
2. **Keep the tonality by default.** Broad features (wider than 1/2 octave) in the ear-gain region depend heavily on the reference, and individual ears differ from ISO 226 by several dB there. Narrow features depend on it less, but not nothing: the baseline is too stiff to follow the contour, so a narrow peak on the contour's slope still changes height with the reference. By default Flatiron corrects narrow features only, including narrow features riding on a broad one, and adds broad compensation filters so the correction averages to zero over about W. The headphone's broad tonal balance stays as it was, and broad changes are left to the user's own EQ. `--broad` switches to correcting broad features toward the fixed target as well, scaled by a strength control.
3. **Compare neighbours, fit globally.** Comparing nearby frequencies is easier and less noisy than comparing far-apart ones. Every judgment goes into one maximum-likelihood fit per ear, which avoids the random-walk error of chaining neighbour comparisons. Judgment noise grows with frequency separation, so long-range comparisons count for less.
4. **Redundant comparison graph.** Each grid point is compared against points 1 to 3 steps away, plus about 25% longer jumps of up to an octave. The longer jumps pin down broad features that a chain of neighbour comparisons would smear out.
5. **Narrowband noise by default.** Pure sines inside headphones are very sensitive to seal and standing waves, and can fall between the peaks of a narrow notch. The default stimulus is 1/12-octave band noise; 1/24-octave noise, warble tones and pure tones are options. The model accounts for each trial's stimulus bandwidth, so sessions with different stimuli pool.
6. **Adaptive grid.** Start at 1/24 octave and refine to 1/48 where the fitted curve bends sharply (or around frequencies the listener marked), so narrow features don't fall between test points.
7. **Reseat and repeat.** Treble resonances move when the headphone moves. Sessions are short, with a reseat between them, and each session is a group in a hierarchical model: more trials in a session say more about that seating and little about seatings in general.
8. **One filter definition everywhere.** The fit, Flatiron's own preview EQ and every export use RBJ-cookbook peaking biquads. Each exporter writes the target's matching mode, and the formats that can be run locally are checked against the real implementation before they're written.
9. **Lazy fits from per-profile settings.** There is no separate fit step and no saved fit. Every command that needs the correction recomputes it from the profile's trials, settings and checkpoint. The expensive part, the perceptual fit, depends only on the trials and is cached, so a correction takes about 0.1 s once the cache is warm, and a few seconds after new trials.
10. **Separate the listener from the headphone.** The listener's own ear and hearing appear in every source, headphone or speaker. A headphone's aberrations appear only in that headphone. Over-ear coupling artifacts appear in every headphone but no speaker. The design separates those three with as little trust in any single setup as possible. Comparison, consensus and veto make the first split, between what's shared by headphones and what's one headphone's. The second, using speakers to find coupling artifacts, is **NOT BUILT**.

## Notation

| Name | Meaning |
| --- | --- |
| L, u\_g, L\_g | The average curve, group g's deviation from it, and group g's curve (L + u\_g). A group is a session. L is called L\_avg in code comments |
| τ(f), or τ\_seat | How much a seating typically differs from the average at frequency f. `tau_lo` and `tau_hi` in the code |
| ℓ | Correlation width of u\_g across frequency |
| λ, κ | The lapse rate, and the soft median's curvature penalty |
| r | Listener noise above 10 kHz as a multiple of σ₀ (`treble_noise`) |
| τ\_hp | Between-headphone spread in the soft median: one value per ear and profile set |
| R\_h, m | Headphone h's residual, and the consensus |
| A\_p | The interpolation matrix from the common grid to profile p's own grid |
| T, cup, hp\_h, spk\_s | **NOT BUILT** (Speaker profiles). The parts of a residual: the listener's own structure, the over-ear coupling shared by all headphones, headphone h's own aberrations, speaker s's colouration plus the room |
| w\_cup, w\_hp, w\_spk | **NOT BUILT** (Speaker profiles). Prior scales: how large each part is assumed to be, typically |
| τ\_pos(f) | **NOT BUILT** (Speaker profiles). How much a speaker position typically differs from the average at frequency f |

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

Sessions are groups in a hierarchical model. A reseat isn't judgment noise: judgment noise is new on every trial, while everything in a session shares one seating, so more trials in a session say more about that seating and little about seatings in general. Modelling that directly also works with a single session, where a consistency check run afterwards can't.

Per ear, the unknowns are the average perceived levels L at the grid points (dB), each session g's deviation from them u\_g, a time-order bias b, and the parameters σ₀, d₀, r, λ and τ. A trial in session g depends on that seating's curve, L\_g = L + u\_g:

```latex
P(A) = \frac{\lambda}{2} + (1 - \lambda)\,\Phi\!\left(\frac{\bar L_{g,a} - \bar L_{g,b} + o + b\cdot s}{\sigma}\right),
\qquad \sigma = \sigma_0 \sqrt{\frac{q(f_a)^2 + q(f_b)^2}{2}} \sqrt{1 + (d/d_0)^2}
```

- o is every known level difference between A and B: Δ\_a − Δ\_b, plus the gain of any EQ in effect (an active system EQ recorded for the session, or the preview EQ of a verify session).
- L̄ is the curve averaged in dB over the trial's stimulus spectrum (11 points across a noise band; one point for tones), interpolated linearly in log-frequency from the grid.
- s = +1 when A played first and −1 when B did; b is how many dB louder the first interval sounds. (The user's real data showed a strong first-interval preference, about +1 dB.) b has a N(0, 3 dB) prior and is a fixed effect, so its uncertainty is part of L's error bars.
- d is the pair's separation in octaves.
- q(f) is 1 below 6 kHz and r above 10 kHz, log-interpolated between: listener noise can rise in the treble. Each tone brings its own variance, so a pair straddling the ramp gets a value in between. r has a log-normal prior centred on 1 (log SD 0.5). On the real data it reads about 2.5 in two left ears and 1.25 to 1.4 in the other four, where it can't be told from 1. The ramp's position is fixed, not fitted: anything from 6 to 12 kHz fits about equally well, and a hard cutoff fits no differently. The ramp only removes dependence on exactly where an edge falls.
- Priors: smoothness, as a grid-independent curvature penalty equivalent to a 1 dB SD on second differences at 1/24 octave; a very weak N(0, 100 dB) prior on each level so the fit is well-posed with few trials; log-normal priors around σ₀ = 1 dB and d₀ = 0.5 octave; r around 1; λ around 2%. The gauge is fixed by mean(L) = 0.
- u\_g is a Gaussian deviation with amplitude τ(f) and correlation width 1/12 octave (a squared-exponential correlation in log frequency), centred across frequency, because a constant offset cancels within a session. It also picks up anything else that changes between sessions: the noise token, the listener's state, or the stimulus when profiles are pooled. A session with few trials is pulled strongly toward the average, and a well-measured one can stand further from it. The width ℓ is fixed: on the real data the evidence shows no clear preference for a value. σ₀, d₀, λ and b are shared across an ear's sessions, so σ₀ doesn't have to absorb seating differences.
- τ has two values, below 5 kHz and above 8 kHz, log-interpolated between them, per ear and profile, with log-normal priors (medians 0.5 and 1.5 dB, log SD 0.7). With fewer than two sessions of at least 60 trials in the ear, τ stays at those medians, so a single session's error bars allow for seatings not yet measured, rather than tightening without limit as trials are added. The medians are judgment calls. On the real data, treble spread is clear for one ear and profile, suggestive for another, and absent for the other two. About three sessions of 110 trials per ear can detect a spread of 2 dB or more but can't resolve one under about 1 dB. The prior decides that range, and it matters little there: τ of 1 dB raised the median SD by 2 to 12% at these trial counts.
- σ₀, d₀, r and λ, and then τ, are set by maximising the Laplace-approximated marginal likelihood with L and the u\_g integrated out (empirical Bayes, Nelder–Mead started from both the prior means and the warm start). A joint MAP over the curve and the noise overfits: with a few trials per point, the curve separates the answers and σ₀ collapses. Below 200 trials per ear the noise, r included, stays at the prior, because the evidence underestimates σ₀ with fewer trials. The noise is fitted with τ at its prior, then τ once with the noise fixed. Searching both together, or repeating the pass, runs away at sessions of about 110 trials: τ grows to several times the truth while σ₀ collapses. One pass leaves some of a large spread in σ₀, so a true 3 dB reads low. The curves are then the MAP given these.
- The solve is Fisher scoring. Each u\_g is held in a whitened basis of the correlation's significant eigenvectors (about 85 unknowns), and sessions connect only through (L, b), so the matrix is block-arrow: eliminating each session in turn leaves a Schur complement on (L, b), and the work grows linearly with the number of sessions.
- Error bars come from the Laplace approximation (inverse Fisher information), projected onto the gauge. L's SD includes τ² divided by the number of sessions, so it stops shrinking in proportion to trials once each seating is well known. Every displayed curve carries a ±1 SD band.
- The two ears' curves are fitted together under a prior that they're alike: see "Channel balance".
- A session selected with `--sessions`, pooled in with `--pool`, or run by `verify` is simply one more group, and checkpoints work as usual. Perceptual fits carry a model version. Bumping it recomputes every fit from the stored trials on next use, so results can change with no new trials, features near the threshold most of all.

### Channel balance

Each ear is first fitted on its own, as above; that fit gives the ear's noise and τ. The two curves are then fitted together, with those held fixed, under a prior on their difference D = L\_L − L\_R. D is Gaussian on a common 1/48-octave grid, with an SD of 1.5 dB below 5 kHz and 3 dB above 8 kHz (log-interpolated between them) and a squared-exponential correlation of width 1/8 octave. Its constant is free, since each ear's level is a gauge. `config --balance` scales both SDs (larger is looser; 0 fits each ear on its own). A profile in a consensus or veto uses its own setting. The joint solve is the same Fisher scoring with both ears' Schur complements in one matrix: sessions belong to one ear, so the coupling is only between the two curves. Error bars and session curves come from the joint fit. The scheduler still fits each ear on its own while it picks trials (`model/hierarchical.py`, `fit_balanced`; `fit/run.py`, `BALANCE`).

Why: at the real profiles' trial counts, most of each L−R difference is within its error bars, and a consensus reference passed one headphone's apparent imbalance on to the others. For Sundara, the reference's L−R was 1.62 dB RMS (4.9 at most, mostly HD560 above 11 kHz) and is about 0.9 with the prior. The size comes from the real profiles. A rough empirical-Bayes fit of D, pooled over FT1, HD560 and Sundara, gave 1 to 1.5 dB below 5 kHz and 2 to 4 dB above 8 kHz, and preferred a width of 1/8 octave to 1/12 or 1/4.

Real differences are kept, but shrunk. They still show where the data are strong: HD560's right ear is 8 to 12 dB down above 11 kHz (about 4 SD). Sundara's own sessions and SundaraTone's, which share no trials, have L−R curves correlated at +0.57. Other headphone pairs correlate at +0.2 to +0.5, so part of the difference may be the listener's ears rather than the drivers. A Gaussian prior pulls hardest on the largest differences: an 8 dB, Q 3 peak in one ear at 600 trials per ear keeps about 55% of its L−R difference. It washes out as trials grow, but slowly.

Thicker tails may be worth trying later: a Student-t or a mixture (a narrow and a wide Gaussian) for D, in place of the Gaussian. It would pull small differences together as much as now, but leave large, well-measured ones nearly alone. The soft local pooling below is close to the mixture, and it kept slightly more of an ear-specific peak in simulation. Not tried in the joint fit: a non-Gaussian D needs an inner iteration (e.g. reweighting) and makes the result depend on the starting point.

Measured in scratch with a prototype that applied the prior to each ear's finished fit as a Gaussian update; on the real profiles it agrees with the joint fit within 0.4 dB. The test used 150 simulated two-ear listeners (seating spread 0.5/1.5 dB, independent between ears) at 1 to 3 sessions of 110 trials per ear. The true L−R was none, a mild Gaussian difference (0.5/1.5 dB), one the real profiles' size (1/2.5 dB), or mild plus a 3 to 5 dB, Q 6 peak in one ear. Against fitting each ear alone, the target error fell 0.12 to 0.24 dB RMS in every scenario, with fewer misses and false corrections and coverage unchanged. The cost is the ear-specific peak: at 3 sessions, 0.47 of it is corrected, against 0.58 with no prior. Also tried, the same way:

- Firmer priors (0.5/1.5 and 1/2 dB) and always pooling the ears gained a little more on the target. They kept less of the ear-specific peak (0.28 to 0.39) and made the error bars overconfident.
- Pooling locally where the ears are consistent: the Bayes factor in half-octave windows, equal against a 1.5/3 dB difference. Blended by the probability of equality, it did about as well as the prior (0.49 of the peak) with slightly overconfident error bars. Pooled outright where that probability is over 1/2, it gained least and added false corrections, from the steps between pooled and separate.

### Adaptive selection

- Candidate pairs: up to 3 grid steps apart, or (with probability 0.25) a longer jump of up to one octave.
- Each candidate is scored by how much one answer would shrink the posterior variance of L\_i − L\_j. An answer is about this session's curve, so that's the squared covariance of the L and L\_g differences over the L\_g difference's variance plus the answer's noise, which falls off once the current seating is well known at a pair. The score is weighted up where the curve L bends (by 0.5 per SD of curvature, capped at 4 SD). Pairs in the same quarter-octave region as the previous trial are skipped. The pair is drawn at random from the top 8.
- Δ aims at the 50% point as the listener hears it, on this session's curve, time-order bias included, plus jitter of ±1 posterior SD of the difference (at most 4 dB). |Δ| is capped at 12 dB.
- The curves are refitted after every answer with the noise and τ fixed (about 10 ms at 8 sessions of 150 trials on a 199-point grid). Every 50 trials per ear, the noise is re-estimated with τ fixed, and a midpoint is inserted on both sides of every point of L whose second difference exceeds 2 SD, down to 1/48 octave. Every ear's block ends together, since ending only one would put the other's slow refit on the very next trial, a second pause in a row. τ is re-estimated with the noise in the cached fit of all the trials, the first time one is wanted after a session (by `report`, `export` or the next `run`), never within a session.

### Sweep and tone-generator marks

Two optional ways to tell the scheduler where to look:

- **Sweep** (`run --sweep`, before the first session of that `run`, whether or not the profile has earlier sessions; later sessions started from the end screen skip it): a slow narrowband sweep (a cluster of nine tones spread over ±1/24 octave) from 1 to 14 kHz and back, 30 s each way by default (`--sweep-seconds`, 10 to 90). The listener taps at each peak or dip. Up and down taps are paired and averaged, which cancels reaction-time lag, and the grid is refined around them. If the profile already has a fit, the sweep plays through it (Q toggles it), so what's left is what the correction hasn't fixed.
- **Tone generator** (`tone`): a continuous tone or noise band, glided by hand. Marks made there are saved in the profile and refine the grid of the next trial session that starts (each mark is stamped with that session, so it's used once; a mark whose session was deleted counts as unused).

## From measurements to filters

### Pipeline (per ear)

1. **Reference.** D(f) = L̂(f) − E(f), where E is the perceived-level shape a reference-correct headphone would produce, with its mean removed. Options: `iso226` (default: the negated ISO 226 frontal free-field contour at 60 phon, configurable), `diffuse` (ISO 226 plus a diffuse-minus-free-field threshold difference, read from a user-supplied CSV at `reference/diffuse_minus_free_db.csv` in the data folder, because the ISO 389-7 table isn't openly available), `none`, a CSV of perceived-level shape (`freq_hz,db` rows), or a consensus of other profiles' raw curves (below).
2. **Baseline.** A penalised cubic B-spline in log2 f with knots every 2W. The curvature penalty sets its stiffness, so it scales as W⁴ (0.1 relative to the data weight at W = 1 octave); a smoothing spline's scale goes as the penalty's fourth root. It is fitted by iteratively reweighted least squares: Huber weights to convergence, then Tukey bisquare, so features don't drag it. The final smoother is linear in the data, so the residual's covariance follows exactly. `--baseline joint` (experimental) replaces it with the posterior mean of the tonality, which can't tip (below, "Experimental: the joint baseline").
3. **Residual.** R = D − baseline, with its full covariance.
4. **Features.** R is split into lobes between sign changes, and a lobe is split again at any valley no deeper than half the smaller peak on either side. A lobe narrower than 1/2 octave at half height is *narrow*: it's kept if at least two adjacent points exceed k·SD (k = 2). A wider lobe is *broad*: it's kept if its mean exceeds k\_broad times the SD of that mean (k\_broad = 2.5). Features outside \[f\_lo, f\_hi\] are dropped.
   **Soft rejection** (`--soft-width`, default 0.5; 0 for the plain test). A feature within that many SDs of k or k\_broad is partly corrected, by a smooth step from 0 at the threshold minus the width to 1 at the threshold plus the width, so it doesn't appear or vanish as its statistic crosses the threshold. The statistic for a narrow feature is the smaller |R|/SD of its best two adjacent points, which is the quantity the plain test compares with k. A partial feature gets a filter once its target passes the filter fit's tolerance.
   **Inside broad regions.** A broad lobe that isn't corrected as a whole (kept as tonality, the default, or not significant) can still carry narrow features. It's searched again: together with the same-sign pieces it was split from at a valley that stays clear of zero (beyond k SD), so the flank beside a narrow dip isn't taken for a feature of its own. The region's residual is taken against a Gaussian-weighted local linear smooth over the region alone (FWHM 1/2 octave; local linear, so it doesn't lag at the region's ends), and every lobe of that is given the narrow test. Features found this way are corrected against the local smooth, with its (smaller) error bars, and checked for consistency the same way.
5. **Seating.** With two or more sessions, each kept feature's level over its half-height core is taken in every session's curve. It's corrected to the median session's level (the sessions weighted by precision), as a share of its level in L, and dropped if that's under 20% (most seatings show little of it). A resonance only some seatings have sits lower in L than in those seatings, so the share can be above 100%. The model has already shrunk the sessions together as far as the data allow, so there's no significance gate and no abrupt switch at a p-value; the share stays near 100% when they agree. `--no-consistency` turns the scaling and dropping off. A Gaussian deviation can shrink an on-or-off resonance toward the average, which criterion 11 tests. The report gives two numbers per feature, even with one session: the SD of its average level, which decides whether it's real, and the spread between seatings, which says how far off the correction will be on a typical seating. The spread is the RMS of each session's difference from the average level, with that deviation's posterior variance included, since the posterior means alone are shrunk together.
6. **Target.** C = −R over kept features, cuts limited to 12 dB. Boosts are capped at +4 dB for narrow features (often fit-dependent cancellations) and +6 dB for broad ones. With `--broad`, the broad part is scaled by the strength (0 to 100%, default 100%). Without it, broad features aren't in the target at all.
7. **Taper.** C fades to 0 with a raised cosine over up to 1/3 octave beyond \[f\_lo, f\_hi\], limited by the measured margin.
8. **Filters.** On a 1/96-octave grid, in plain dB: greedily place a peaking filter on the largest error (Q from its half-height width), then refit all filters jointly with bounded least squares over (log Fc, gain, log Q). Bounds: Fc within \[f\_lo, f\_hi\], Q 0.7 to 16, |gain| ≤ 12 dB. Stop when no point within \[f\_lo, f\_hi\] is off by more than `--fit-tolerance` (default 0.5 dB), or the band budget (default 24 per channel, at most 128) is used up. Filters under 0.5 dB are dropped and the rest refitted. The target's uncertainty has already been dealt with in deciding the target, so the filters just reproduce it. The stop leaves out the fade beyond the range, where no filter can be centred. The budget is spent on the largest errors first, and with tonality kept, up to 4 bands (at most half) are held back for step 9, so narrow corrections give way first, smallest first. If the budget binds, the frequency ranges still off by more than the tolerance are reported.
9. **Tonality compensation** (the default). Broad peaking filters (Q ≤ 1.5, at most 4, from the bands held back in step 8) are fitted to the negative of the narrow filters' own response, smoothed over W. The corrected response then keeps the original broad shape to within about 0.5 dB. Filters at nearly the same frequency and Q are merged.
10. **Preamp.** −max(0, peak) − 0.5 dB, where peak is the highest point of the combined response from 20 Hz to 20 kHz. It's one value for both channels, which keeps the L/R balance.

### Settings

Every setting but the channel balance is downstream of the cached perceptual fit, so changing it needs no new trials. The balance changes the joint fit of the two ears, which is quick and cached separately. Settings are stored per profile (`flatiron config`) as only the values that differ from the defaults, so improved defaults reach existing profiles.

| Setting | Default | `config` option |
| --- | --- | --- |
| Max feature width W | 1 octave (1/3 to 2) | `-w/--width` |
| Channel balance | 1 (scale on 1.5/3 dB; 0: off) | `--balance` |
| Reference, listening level | `iso226`, 60 phon | `-r/--reference`, `--phon` |
| Consensus reference, its smoothing | none, 1/2 octave | `-r consensus:A,B`, `--consensus-smooth` |
| Veto against other profiles | none, 1.5 dB import | `--veto A,B`, `--veto-mode feature\|pointwise`, `--veto-import` |
| Soft veto | 0.1 (0: all or nothing) | `--veto-softness` |
| Significance k, k\_broad | 2, 2.5 SD | `-k/--k`, `--k-broad` |
| Soft rejection width | 0.5 SD (0: all or nothing) | `--soft-width` |
| Broad correction, strength | off, 100% | `--broad`, `-s/--broad-strength` (needs `--broad`) |
| Band budget | 24 per channel (at most 128) | `-b/--bands` |
| Filter fit tolerance | 0.5 dB | `--fit-tolerance` |
| Boost caps, cut cap | +4 narrow, +6 broad, −12 dB | `--boost-narrow`, `--boost-broad`, `--max-cut` |
| Correction range | 1 to 13 kHz | `--f-lo`, `--f-hi` |
| Seating step | on | `--consistency` / `--no-consistency` |
| Sessions included | all | `--sessions` |
| Other profiles pooled in | none | `--pool` |
| Method | features | `--method features\|smooth\|match`, `--smooth-width` |
| Baseline | robust | `--baseline robust\|joint` |

`config` with no options lists every setting, starred where changed. `--reset [name ...]` returns the named settings, or all of them, to their defaults. Some combinations are refused: `--veto` and `--baseline joint` need `--method features`, `--method smooth` doesn't combine with `--broad`, and `--smooth-width`, `--veto-mode`, `--veto-import` and `--consensus-smooth` need the setting they modify. Limits: W 1/3 to 2 octaves, `--balance` 0 to 10, `--consensus-smooth` 0 to 2 octaves, `--smooth-width` 1/8 to 1 octave, strength 0 to 100%, 1 to 32 bands.

The correction range can only move within the measured range, and the measured range, grid and stimulus are fixed when the trials are collected. Features wider than about an octave are only as well constrained as the longest jumps (1 octave).

### Pooling

`config --pool OTHER` includes another profile's trials in this profile's fits: the same headphones measured with a different stimulus, for example. The model already accounts for each trial's stimulus, so the sessions pool directly. They stay in their own profile; new sessions there are included as they're run. Each pooled session is one more group in the fit, and the newest session's grid is the one the fit uses. Trial sessions still adapt to the profile's own trials only. `--pool none` stops pooling.

### Comparing profiles

`report --compare A B C …` overlays two to eight profiles' raw curves per ear with their consensus, and says what the veto would correct on each. It fits no filters.

**Inputs.** Each profile's cached perceptual fit, L̂ with its covariance, from its own sessions and pooling. Settings are shared for the comparison: max feature width W (`-w`, default 1 octave), reference (`-r`) and ear (`--ear L|R|both`). Without `-r`, the reference the profiles have in common is used; if they differ, Flatiron asks for `-r`. A consensus reference isn't used here: `iso226` stands in, with a note. The listening level and k come from the profiles when they all agree, and every other setting is at its default, including the veto's import limit. Profiles that share trials through pooling get a warning, since they'll agree more than separate measurements would.

**Grids.** Each fit stays on its own grid of about 100 to 110 points, and detection, flags and the soft median's misfit are computed there. Interpolating a fit onto a finer grid adds no measurements: the new points are mixes of their neighbours, so a 399-point curve still holds only about 106 independent values. Treating those points as separate data would make the two-point rule meaningless and error bars too small. Shared curves such as the consensus live on a common 1/48-octave grid, the finest any fit uses, over the intersection of the measured ranges, and meet a profile on that profile's own grid through A\_p. The comparison figure is drawn on the common grid. The between-headphone spread τ\_hp and Cochran's Q are also computed there, point by point from the interpolated curves and variances, so correlated neighbours in the strip below aren't separate evidence. The 1/96-octave grid is used only for the filter fit, with the target and response curves it produces, and to compare feature shapes in the veto.

**What's reported, per ear:**

1. **Aligned curves.** Each raw L̂, with no reference subtracted whatever `-r` is (as with `-r none`), shifted by its robust offset (see Consensus reference), with ±1 SD bands, and the raw consensus on top with its own. The reference only matters for detection, so the flags and feature tables use it and the figure doesn't. A mean-based alignment would be pulled by a treble difference in one headphone. Broad tonal differences stay visible here, and only here.
2. **Where the curves differ.** A strip shades frequencies where the aligned curves disagree as a whole (Cochran's Q, p < 0.01). It includes broad differences.
3. **Flags.** A feature is flagged when the veto, in its feature mode, would correct it, so a flag always names something a veto would act on, and a broad difference can't produce one. A flag lists the profile, ear, centre, height, correction, and a reading: "peak only here", "peak elsewhere on the others", or "taller here than on the others", with the words for dips to match. With two profiles the reading is "differs from the other". They're sorted by the size of the correction.
4. **All features.** One row per place, for features that all overlap each other, with each profile's height there and what the veto would correct. A feature missing from one headphone isn't flagged on it; it shows up here as a gap, and in the others' rows.

Profiles of kind `iem` are drawn dashed and don't count toward the consensus or the veto unless `--include-kind iem`. They lack the pinna and concha, so a peak they lack says nothing about whether it's the listener's. They're measured against the voters' median without counting toward it, and their offset is a weighted median against the consensus. A comparison needs at least two voters. (Kind `speaker` would be treated the same way, and is **NOT BUILT**.)

This replaced an earlier report of residuals and pointwise z-scores. Each profile's baseline makes its own decision about what to reject, so the residuals disagreed more than the raw curves: on HD560, FT1 and Sundara, RMS spread was 2.97 dB in the residuals against 2.66 dB raw in the left ear, and in the left-ear mids a 4 to 5 dB raw gap became 6 to 10 dB.

**The soft median.** The consensus m(f) minimises a robust misfit across profiles plus a smoothness penalty across frequency:

```latex
m = \arg\min_m \sum_p \sum_{i} w_{p,i}\, \rho_\delta\!\left(\frac{L_{p,i} - o_p - (A_p m)_i}{\mathrm{scale}_{p,i}}\right) + \kappa \lVert D_2\, m \rVert^2
```

- o\_p is profile p's constant offset, fitted with m by the same loss, summing to zero.
- i runs over profile p's own grid points. w\_{p,i} is the octave width each point covers relative to 1/24 octave, so a point at the usual spacing counts as one measurement and a refined region doesn't count for more just because it has more points. The curvature penalty is scaled to that same unit; against raw octave widths it would smooth about 24 times too hard.
- ρ\_δ is the pseudo-Huber loss with δ = 1: close to a precision-weighted mean when profiles agree within their error bars, and close to a median when one is far off. That is the "soft" part.
- scale\_{p,i}² = SD\_{p,i}² + τ\_hp². Measurement noise alone would overweight the most-measured profile.
- τ\_hp is one value per ear and profile set: the median across frequency of the per-frequency DerSimonian–Laird estimates, floored at 0.5 dB. A separate estimate at each frequency would let a headphone's own peak inflate the tolerance at that peak, and the soft median would then act like a mean exactly where it most needs to be robust.
- κ uses the same curvature penalty as the perceptual fit's prior, so m is no smoother than the data it comes from.
- It's solved by iteratively reweighted least squares.
- m's covariance carries each profile's full covariance through the final weights. With W\_p = diag(w\_p / scale\_p²) and H the final system matrix:

```latex
\operatorname{Cov}(m) = H^{-1}\Big(\sum_p A_p^{\mathsf T} W_p \operatorname{Cov}(L_p)\, W_p A_p\Big) H^{-1}
```

  Per-frequency weights alone ignore the correlation between neighbouring points and make the SD too small (see Prototype findings). This treats the final weights as fixed, which criterion 10 tests.

With only two profiles, the consensus is their precision-weighted mean and the report says so. The flags then show differences, not outliers.

### Consensus reference

`config -r consensus:A,B,C` uses a consensus of raw curves as E, in place of a fixed reference (pipeline step 1). Every later step is the same, including the baseline. It and the veto (next section) are separate settings, usable alone or together, with or without `--broad`. Both exist to avoid correcting what's shared across headphones, so they aren't independent in effect: an unsmoothed consensus reference leaves a veto little to do. Smoothed, the reference carries the broad shape and the veto decides the narrow features.

1. **Raw curves.** The sources are the listed profiles' L̂, from their own sessions and pooling, with no reference and no baseline. So a consensus depends only on its members' fits, and every profile using the same list shares one cache entry.
2. **Members.** Exactly the profiles listed, which must be of kind `headphone`. The profile being corrected is included only if it's listed. Leaving it out makes the reference independent of it, but with two others the soft median falls back to their weighted mean, so one's peak reaches the reference at about half height. Listing it as well gives a real median of three. A single profile is allowed (the reference is then that headphone's curve), with a note. Profiles that share trials through pooling can't be used together: `--pool` means the same headphones measured again, and a consensus needs different ones.
3. **Alignment.** Each source gets a constant offset, fitted together with the soft median by the same robust loss and summing to zero, so a treble difference in one headphone doesn't shift its whole curve. A mean-based alignment would be pulled by exactly those differences.
4. **Smoothing.** `--consensus-smooth` (FWHM, default 1/2 octave, 0: none) smooths m with a local linear fit. Unsmoothed, the reference holds the members' narrow features, so a shared peak disappears before detection and another headphone's peak reads as a dip here. Smoothed, the reference carries the broad shape and narrow features go to detection, and to the veto if it's on. About half of a shared peak around 1/4 octave wide stays in the reference at this width, so a feature's height against it is lower than against a fixed reference.
5. **Covariance.** Cov(L − m) adds m's error, and for a member subtracts the correlation its own vote creates. The offsets are treated as fixed.

Raw results are shared at the top of the data folder (`cache/consensus/`), keyed by the ear, the members and each member's perceptual-fit hash, and pruned least-recently-used at 200 MB. A new session in any member changes its fit and so the key. The smoothing is applied after the cache.

### Veto against other profiles

`config --veto A,B,C` corrects each feature toward the median feature at the same place across headphones, rather than toward zero. Detection is unchanged, on this profile and each voter, against this profile's reference and settings. It needs `--method features`. It applies to whatever would be corrected: narrow features, those inside broad regions, and with `--broad` broad ones too. `--veto none` turns it off, and `report` lists what each voter contributed.

1. **Voters.** The profiles listed plus this one, always. They must be of kind `headphone`, at least one must be another profile, and profiles that share trials through pooling can't be used together.
2. **Matching.** Each voter contributes its tallest same-sign feature that overlaps this one, in the literal sense that their half-height widths intersect. A voter with no overlapping feature counts as having nothing there. A voter's narrow lobes of 1 dB or more count even if they aren't significant, since an undetected lobe isn't the same as nothing. There's no matching parameter. Peaks that are clear of each other, however close, are treated as separate features.
3. **The median feature.** The most central of the candidates: the actual feature (or nothing) with the smallest total shape difference to the rest, each drawn as a Gaussian of its height and half-height width. Ties go to this profile. With one other profile there's no middle, so it's their average.
4. **Correction.** With R the feature's residual and b the median feature, C = −(R − b), with any net boost in the feature's own direction limited. The limit is `--veto-import` (default 1.5 dB) if the median feature is more than 1/12 octave away, and 0 otherwise, so at the same place a feature can only shrink. Outside the feature only an import can add anything. A limit of 0 means nothing is ever imported. The result is then held to the usual cut and boost caps, and a feature whose largest correction comes to under 0.5 dB is left alone as shared.

| Case | This feature, then the others (dB) | Median feature | Corrected |
| --- | --- | --- | --- |
| In every headphone, same height | +8, +8, +8 | This one | Nothing |
| Only in this one | +8, nothing, nothing | Nothing | All of it |
| In this one and one other | +8, +8, nothing | This one | Nothing |
| Taller here | +8, +6, +6 | +6 | 2 dB |
| Shorter here | +4, +8, +8 | +8, same place | Nothing; no boost |
| Opposite in the others | +5, −3, −3 | Nothing, since they aren't the same sign | All of it |
| Elsewhere in the others | +7 at 8.2 kHz, +6 at 7 kHz twice | +6 at 7 kHz | All of it, and at most 1.5 dB of net boost at 7 kHz |
| Two profiles | +8, nothing | +4 | Half |

Why not the alternatives:

- **One share per feature**, from a template fit to each curve, was built first and failed. A lobe holding both a shared peak and this headphone's own needs two different shares. The difference of shapes handles it: on noiseless curves, such a lobe got −4.5 dB on its own part and −5.5 dB on the shared one with a share, and −5.3 and −1.4 dB this way.
- **Independent medians** of frequency, width and height can give a feature no headphone has. A peak between two others on either side comes back as itself and reads as shared, which happened on real data at 8.2 kHz.
- **A limit on the bump instead of the net boost** cuts energy this headphone already has at the median feature's place.

This profile's own vote doesn't protect against imports when the other two agree: at 8.2, 7 and 7 kHz the median ignores it. That's why the import limit is explicit.

`--veto-mode pointwise` is the alternative. Features are found on the excess over the soft median m of the voters' residuals, each with its own baseline and this profile's included, and at each frequency what exceeds that median is corrected, never past zero: C = −sign(R) max(0, |R| − max(0, sign(R) m)). A checkpoint's filters are part of R but not of the median, so a peak they already took out leaves nothing to correct. This mode can't import or move anything, and it's sensitive to broad differences between headphones because of the separate baselines. `--veto-import` doesn't apply to it.

**Soft veto** (`--veto-softness`, default 0.1; 0 for the rule above). The steps above are all or nothing, so a small change in a curve can move a correction by decibels: a near-tie flips the median feature, and a voter's feature counts or doesn't. With a softness s above 0, each is gradual:

- **Weights.** A candidate with total shape difference D weighs exp(−(D/D\_min − 1)/s), normalised, where D\_min is the most central candidate's. The correction is the weighted mean of what each candidate would lead to as the median feature. A clear winner gives the rule above, and this profile taking all the weight still gives no correction. R isn't exactly this feature's own Gaussian, and that difference counts only as far as a candidate differs from this feature, so a voter with the same shape leads to no correction either. Near a tie this mixes two shapes, not their parameters, so it doesn't bring back the independent medians' problem: a clear winner comes through as itself.
- **Matching.** A voter's same-sign features count by how far they overlap this one, 1/2 at the literal limit, falling from 1 to 0 across half that limit either side. Among several, a taller one dominates in proportion to height⁴. A voter's feature that isn't significant counts by height from 0 at 0.5 dB to 1 at 1.5 dB, and one that is counts by at least its significance share.
- **Same place.** The import limit, and an import's reach outside the feature, come on from 1/24 to 1/8 octave.
- **Location.** A candidate's centre is weighted over the part of the lobe above half height, and its width is its area over its height, scaled to match FWHM for a Gaussian. The peak frequency can jump between two near-equal extremes of a lobe, and these can't.
- **The 0.5 dB rule** doesn't apply; the filter fit's own smallest filter is what's left.

Lobe splitting, narrow/broad classification and the broad region search keep their hard edges.

### Experimental: the smooth and match methods

`config --method smooth` skips feature detection. The baseline is the curve smoothed with a Gaussian-weighted local linear fit over `--smooth-width` (FWHM, default 1/2 octave), and the whole residual within \[f\_lo, f\_hi\] is the target, with the usual caps, tonality compensation and filter fit. It corrects more, but with no significance test it also corrects noise: on noise drawn from real fits' error bars it fits about 3 filters per ear (1.5 to 3 dB), against about 0.8 for the features method. A smoothing window also takes part of any feature near its own width into the baseline (about 45% of a 1/4-octave resonance at 1/2 octave), which under-corrects it. The features are still reported, as what's significant, and verify uses them. It doesn't combine with `--broad` or `--veto`; a reference, consensus included, still applies.

`config --method match` has no baseline at all. The whole of L̂ − E within the correction range, with only its level free (a precision-weighted median), is the target, tonality included, with the cut cap and the broad boost cap and no tonality compensation. There's no feature test. It's meant for trying a consensus reference; with ISO 226 it would correct the difference between the headphone and the contour, broad shape and all. It doesn't combine with `--veto`.

### Experimental: the joint baseline

`config --baseline joint` replaces step 2's robust spline with the posterior mean of the tonality under an explicit model of D, so the baseline moves continuously with the data and the error bars instead of tipping between two readings (Known limitations, "The baseline can tip"). Everything downstream is unchanged except that the baseline is nonlinear in D. `robust` stays the default until this has earned the switch.

**Model.** Per ear, on the fit's grid: D\_i = s(x\_i) + e\_i, with s = B c the robust baseline's spline (knots every 2W). Each point is tonality plus the fit's error, or that plus a feature: e\_i ~ (1 − π) N(0, sd\_i² + floor²) + π N(0, sd\_i² + floor² + spread²), with π = 0.4, spread 8 dB and floor 0.5 dB. The curvature prior is the robust baseline's stiffness (0.1 relative to the data weight, scaling as W⁴), but taken at a fixed SD of 1.1 dB (the real profiles' median) rather than at the fit's own, so it's fixed in dB. Level and slope are free. As in the robust fit, each point is weighted by the width it stands for, in initial grid steps, and only the SDs (not the correlations) of the fit's covariance are used. The floor is the tonality's own ripple: without it, whether a block is shape or feature turns on its misfit in SDs, which moves fast with the error bars' scale.

**Inference.** The tonality has only a handful of coefficients (5 at W = 1, 9 at W = 1/3), so the posterior mean is integrated directly by importance sampling over fixed draws (4,000 per proposal, seed fixed): five rounds of proposals (multivariate t, 4 dof), the first centred on the Huber fit and as wide as the posterior with the likelihood raised to the power 0.01, each later one matched to the moments of the posterior at a higher power (0.03 to 1), 1.5 times wider. Where a block sits far off the reference, the posterior has several narrow modes far apart (one per reading) that no single proposal covers, so the modes are also found by Newton's method from 16 starts (the Huber fit, the robust fit, and the best draws, chosen farthest-first), and each mode within 20 nats of the best (Laplace evidence) gets a proposal of its own, its Laplace covariance 1.3 times wider. Every draw is weighted against the mixture of all the proposals (multiple importance sampling). Nothing is resampled, so the estimate is a smooth function of the data, except where a mode is found or not: that changes only the proposal, and the estimate by its Monte Carlo error. It takes about 0.3 s per ear, so it isn't cached.

**Residual covariance.** The baseline's Jacobian is exact for a posterior mean: J = Cov(s, ∂ log p(D | c)/∂D | D), from the same weighted draws. The residual is D − b̂, with covariance (I − J) Σ\_D (I − J)ᵀ, and each session's curve is R + (I − J) u\_g. Unlike the robust fit's, which holds its final weights fixed, this includes how the reading itself moves with the data: where the baseline is sensitive (a block near the edge between readings), the residual's SD grows and features there need to be larger to count. A fixed-weights version (each point's posterior feature probability held fixed) was tried, with an earlier sampler and no floor: on known curves it made 0.07 to 0.22 more false corrections per ear, against 0.02 to 0.07 fewer misses.

**Report.** The baseline is drawn with its posterior SD. A corrected feature where that SD exceeds 1 dB is flagged in both reports: part of it may be tonality, or the other way round.

**Results.** Validation used 300 simulated listeners per scenario (3 to 6 features, 3 to 9 dB, Q 2 to 10, three sessions of 200 trials), *standard* (on ISO 226) and *contour* (two broad Gaussians off ISO 226, −6 to 0 dB near 2 to 3.5 kHz and +5 to +13 dB near 7 to 11 kHz, the real profiles' condition), against ISO 226:

| Check | Robust | Joint |
| --- | --- | --- |
| Real ears, SD × 0.8 to 1.2 in 2.5% steps: largest baseline step | 0.10 to 2.14 dB | 0.03 to 0.25 dB |
| Contour listeners (120), same path: median, 90th percentile, largest; share over 1 dB | 0.12, 5.8, 13.1 dB; 23% | 0.04, 0.20, 1.2 dB; 1% |
| Real ears, curve jittered by 0.3 SD (10 draws): largest baseline movement | 0.7 to 6.0 dB | 0.6 to 3.3 dB |
| Real ears, two seeds | n/a | under 0.02 dB |
| Standard: target RMS error; misses, false corrections per ear; median share corrected (peaks, dips) | 1.674 dB; 0.25, 0.50; 0.68, 0.53 | −0.024 ± 0.013 dB; 0.22, 0.49; 0.67, 0.53 |
| Contour: the same | 2.030 dB; 0.75, 1.39; 0.29, 0.46 | +0.004 ± 0.024 dB; 0.76, 1.35; 0.30, 0.46 |

On the real profiles the targets move by 0.03 to 0.5 dB RMS, except SundaraTone R (1.2 dB): its −3 dB dip at 5.3 kHz is dropped, because the residual's SD there doubles with the baseline's sensitivity. SundaraTone L gains its 7.2 kHz peak (+10 dB) as a feature of its own, where the robust baseline merges it with the 11 kHz one. The aims weren't all met: the largest step is still above 0.2 dB on some ears and listeners, and jittering the curve still moves the baseline by up to 3.3 dB (Sundara R; SundaraTone R moves 2.2 dB against 1.95 with the robust fit). Those come from the data, not the sampler: the posterior's weight on each reading moves with them. The floor trades steps against false corrections: at 1 dB, 90% of the contour listeners' steps were under 0.13 dB, but false corrections rose by 0.14 to 0.23 per ear.

**Tried first.** The design proposed a smooth tonality plus a regularised horseshoe over a dictionary of about 490 RBJ-shaped bumps no wider than W, sampled by Gibbs. Its prototype failed on two counts. The sampler didn't mix: chains started with every bump off and every bump on disagreed by 2 to 6 dB on the real ears (9 dB with a sparser prior), against an aim of 0.1 dB. And bumps tile: two or three adjacent bumps no wider than W sum to anything broad, so the baseline sank to the bottom of the curve (SundaraTone L flat at about −3 dB, HD560 L flat at −6), and moved by several dB with the priors and with whether Σ\_D's correlations were used. A single moment-matched importance sampler also collapsed (effective sample size 1 to 10) on a third of the contour listeners, which the tempering, the mode proposals and the multiple importance sampling fix.

### Checkpoints

`config --checkpoint` fixes the current filters per ear. Later fits run the pipeline on the corrected curve (L̂ plus the fixed filters' response) and only add filters for what's still significant, within the bands left over. This holds whatever the settings become. `--reset-checkpoint` drops it. The usual use is to checkpoint before a verify session, so its results add to the filters that were verified instead of reshuffling them.

### Measurement context

Flatiron treats EQ as three layers: any system EQ active while measuring, the correction Flatiron fits, and an optional preference layer the user owns.

- **Active EQ is known, not measured.** A session can record the system EQ in effect (`run --active-eq`, as APO text, with an optional `--eq-note`). Its gain at each stimulus frequency enters the model as a known offset, so L̂ always describes the raw headphone.
- **Export modes.** *Correction only* (default): filters to stack after the existing EQ or in their own instance. *Merged* (`export -m/--merge`, or `--merge-left` and `--merge-right` together): the preference layer's APO text first, then the correction sorted by frequency, with the preamp computed over both.

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

All exporters work from one export plan: per-channel filter lists and a preamp. LSP and EasyEffects output is checked against LSP's own LADSPA build before writing, when it's installed. PipeWire output can be checked with `--check`. The tolerance is 0.3 dB from 1 to 14 kHz, and an export that misses it isn't written.

| Target | File | Per-channel mechanism | Limit | Checked against |
| --- | --- | --- | --- | --- |
| Equalizer APO / AutoEQ text | `<name>_L.txt`, `<name>_R.txt` | One file per channel | — | — |
| LSP Parametric Equalizer x32 LeftRight | LSP settings `.cfg` | Separate left/right bands | 32 per channel | LSP LADSPA, offline |
| EasyEffects (and Equaliser, macOS) | Preset `.json` | `split-channels`, `left`/`right` | 32 per channel | LSP LADSPA, offline |
| PipeWire filter-chain, builtin `param_eq` | Drop-in `.conf` | `filters1` (L), `filters2` (R) | — | A private filter-chain (`--check`) |
| TinCan | Preset `.toml` | `channel` per filter | 256 filters | — |

By default files are written to the current directory, named `<profile>-<fit hash>` (with `-merged` when merged); `-o FILE` picks another name, and APO text then becomes `FILE_L.txt` and `FILE_R.txt`. TinCan presets go straight into TinCan's preset folder. A folder that doesn't exist is an error, never created. `--preamp safe|DB` overrides the preamp (`0` keeps the uncorrected level for A/B comparison; a warning is printed if boosts can then clip).

- **APO text.** A `Preamp:` line, then `Filter n: ON PK Fc … Hz Gain … dB Q …`. PipeWire `param_eq` reads it through `filename1`/`filename2`, and REW-style importers accept it.
- **LSP.** Written over a template exported from LSP 1.2.35, changing only band, gain and mode lines. Bands use filter mode 6, "APO (DR)", which matches the RBJ filters within 0.01 dB (the default RLC (BT) mode differs by up to 1.7 dB on narrow bells). The preamp is the input gain; LSP doesn't auto-balance.
- **EasyEffects.** Written over an EasyEffects 7 split-channel preset, with bands in "APO (DR)" mode and the preamp as input gain. EasyEffects runs LSP's equalizer, so the same LADSPA check applies. Equaliser (macOS) imports this format; how it maps Q and mode on import hasn't been verified.
- **PipeWire.** A filter-chain module with `param_eq` filters inline, `bq_peaking` (and shelves) in RBJ form. The preamp is a `bq_highshelf` at frequency 0, which is a broadband gain. Installed by copying it to `~/.config/pipewire/pipewire.conf.d/` and restarting PipeWire (`systemctl --user restart pipewire`), which adds a "Flatiron <profile>" sink. `--check` plays a log sweep through a private copy (virtual sink and source, nothing on real devices) and compares the recording with the prediction.
- **TinCan.** TinCan's preset format 1: `format`, `name`, and `[[filter]]` tables with `type`, `channel` (L, R or LR), `frequency`, `gain`, `q` and `enabled`. TinCan's bells are RBJ peaking filters. Presets carry no preamp, since TinCan sets one for everything loaded, so `--preamp` is refused. A filter identical in both channels (e.g. a merged preference shelf) is written once as LR. The preset is named "<profile> Flatiron <fit hash>", saved under TinCan's file-name rule into `$XDG_DATA_HOME/tincan/presets/`, or written to `-o FILE` for TinCan's import. Values outside TinCan's ranges are refused rather than clamped.

## Architecture

A Python 3.11+ package with a keyboard-driven terminal UI, managed with `uv`. It's intended to run unchanged on Linux and macOS. Its matrices are small (a few hundred rows), so it limits BLAS to one thread before numpy loads: threading made fits about 8× slower on a 16-core machine. `uv sync` installs it; commands run as `uv run flatiron …`, and tests as `uv run pytest`.

| Concern | Choice |
| --- | --- |
| Playback | `sounddevice` (PortAudio) |
| DSP and fitting | `numpy`, `scipy` |
| UI | `textual` |
| Report | `matplotlib` figures embedded in a static HTML page |
| Tests | `pytest` with `pytest-xdist` (parallel by default; `-n0` for one process) |

### Modules

- `audio/`: biquads, stimuli, limiter, device listing, the persistent output stream and buffer mixer, the tone generator's synth, and the preview EQ.
- `model/`: grids (with the common grid and the interpolation A\_p), the trials and fit types, the hierarchical fit, and grid refinement.
- `session/`: the adaptive scheduler, the trial runner (scheduler + stimuli + session file, without UI), storage, and the sweep.
- `fit/`: reference, baseline (robust and joint), features, filter fitting, the correction pipeline, the consensus, the comparison's flags and rows, the veto, and the per-profile fit (cache, settings, pooling, consensus, checkpoint).
- `export/`: the export plan, the five writers, the LADSPA host and the truth checks.
- `report/`: figures, the HTML report, and the comparison report.
- `ui/`: the trial app (level, channel check, sweep, trials, end screen), audition, and the tone generator.
- `sim/`: the simulated listener, non-adaptive designs, validation, and the seating and multi-headphone scenarios.
- `cli.py`: the commands below.
- `apo.py`: parsing Equalizer APO / AutoEQ text (`Preamp:` and `Filter N: ON` lines of type PK, LSC/LS or HSC/HS; OFF filters are skipped, anything else is an error). Writing it lives with the exporters.
- `iso226.py`: the ISO 226:2003 equal-loudness contours. `paths.py`: the data folder.

### Commands

`-p/--profile` defaults to the only profile, if there's just one, and `run` creates a profile that doesn't exist. `-d/--device` takes an index from `devices`, and `-n/--dry-run` runs with no audio.

| Command | Does | Options |
| --- | --- | --- |
| `devices` | Lists outputs with host API, channels and native rate, marks the default, and flags Bluetooth, mono and rates other than 48 kHz | — |
| `run` | Runs trial sessions | `-p`, `-d`, `-n`, `-m/--minutes` (default 12, at most 15), `--stimulus noise\|warble\|tone`, `--bands-per-octave 12\|24`, `--active-eq APO_TXT`, `--eq-note`, `-s/--sweep`, `--sweep-seconds` |
| `verify` | A short session through the correction | As `run`, without the sweep, plus `--trials` (default 150) |
| `audition` | The correction on and off | `-p`, `-d`, `-n`, `--source pink\|sweep\|FILE.wav` (repeatable; default pink and sweep) |
| `tone` | The tone generator | `-p`, `-d`, `-n`, `-f/--freq` (default 1000), `-e/--ear L\|R\|both` |
| `profiles` | Lists profiles (sessions, trials per ear, last date and level, sessions not closed), or changes them | `new NAME --kind headphone\|iem`; `set NAME --kind … --label …`, which affects no trials |
| `sessions` | Lists sessions: trials per ear, minutes, catch-trial accuracy, fast answers, share choosing the first interval, EQ context, verify and not-closed marks | `-p`, `--delete ID` (e.g. a session left open by a crash), `-y/--yes` |
| `config` | Shows or changes settings (see Settings) | `-p`, the options in Settings, `--sessions ID,ID\|all`, `--pool P,P\|none`, `--reset [NAME ...]`, `--checkpoint`, `--reset-checkpoint` |
| `report` | Prints filters and features, and notes on the seating spread, noise and bias | `-p`, `-H/--html`, `--csv FILE`, `--compare P P …`, and with `--compare`: `-w`, `-r`, `--ear`, `--include-kind iem` |
| `export` | Writes the correction | `-p`, `-t/--target apo\|lsp\|easyeffects\|pipewire\|tincan` (required), `-o/--output`, `-m/--merge`, `--merge-left`, `--merge-right`, `--preamp safe\|DB`, `--check` (PipeWire) |
| `simulate` | Runs a simulated listener (see Validation) | `--trials` (per ear, default 600), `--seed`, `--sigma0`, `--lapse`, `--design adaptive\|uniform`, `--scenario consensus\|seating` |

`report --csv` writes one row per ear and grid point: `ear,freq_hz,level_db,level_sd,reference_db,baseline_db,residual_db,residual_sd`.

### Keys

| Screen | Keys |
| --- | --- |
| Level | ↑/↓ ±1 dB, PgUp/PgDn ±5 dB, Enter continue, Esc cancel |
| Channel check | ←/L left, →/R right, Space replay, Enter retry, Esc quit |
| Sweep | Enter start, ↑/P peak, ↓/D dip, Q correction on/off, Esc skip |
| Trials | ←/1 A louder, →/2 B louder, Space replay, P pause, U/Backspace undo (the pair plays again), L level (with a warning), Q save and quit, X discard (asks first) |
| End of session | N next session, Q quit |
| Audition | Space switch, N next source, B blind, R reveal, Q/Esc quit |
| Tone generator | ←/→ frequency, ↑/↓ glide speed, G go to a frequency, E ear (left, right, both), S stimulus, +/− level, Q correction on/off (with `-p`), P/D mark a peak/dip, U undo the last mark, Space pause, Esc quit |

### Data on disk

Everything lives under `~/.local/share/flatiron/` (Linux, or `$XDG_DATA_HOME`) or `~/Library/Application Support/flatiron/` (macOS), as JSON written atomically.

- `profiles/<slug>/profile.json`: label, kind (`headphone` by default, or `iem`), settings (only changes from the defaults), the sessions used (if not all), profiles pooled in, the checkpoint (filters per ear, when it was made and on how many trials), and tone-generator marks. A consensus reference is stored in the settings as the string `consensus:slug,slug`, and a veto as a list of slugs. The name `none` is reserved, since `--pool none` and `--veto none` use it.
- `profiles/<slug>/sessions/<timestamp>.json`: device, sample rate, EQ context (note and APO text), stimulus, level and level changes, gap, seed, scheduler settings, final grid per ear, channel checks, sweep marks and the correction it played through, tone marks used, the preview EQ for verify sessions, and every trial. A trial records the ear, both frequencies, Δ for each, play order, rove, answer, reaction times, replays, the fast flag, known EQ gains, level, and the undone and retake flags. The file is saved after every answer.
- `profiles/<slug>/cache/`: perceptual fits (`probit-<hash>.npz`, keyed by trials, sessions, grid, priors and model version; they hold τ and each session's deviation), which consensus entries the profile last used, and the last HTML report.
- `reference/diffuse_minus_free_db.csv`: the user-supplied table for the `diffuse` reference.
- `cache/consensus/`: consensus results, shared by the profiles in each set. `cache/comparison.html`: the last comparison report.

## Session flow

1. **Setup.** `devices` flags Bluetooth, mono and sample-rate problems.
2. **Each session** (`run`, at most 15 minutes, 12 by default):
   - The level screen.
   - The channel check: a burst in each ear in random order, and the listener says which side it was in.
   - The sweep (first session of this `run`, if asked).
   - Trials until time is up or the listener quits.
   - The end screen asks for a reseat before the next session.

   The fitted curve is never shown during trials. Trial keys: ←/1 = A louder, →/2 = B louder, Space = replay, U = undo, P = pause, L = level, Q = save and quit, X = discard the session.
3. **Look** (`report`, `report -H`): per ear, L̂ with its SD band, the reference, the baseline, the residual with its significance, the features (kept or not, and why), the filters, and the predicted corrected curve. With two or more sessions, each session's residual is drawn against the average; above five, the band holding the middle 90% of them, weighted by trials. Checkpoint filters are labelled.
4. **Adjust** (`config`), **listen** (`audition`), **verify** (`verify`, ideally after `config --checkpoint`), and **export**.

## Validation

### Simulated listener

A simulated listener exercises the scheduler, perceptual fit and correction pipeline. `flatiron simulate` reports how well the perceptual fit recovers it, and `sim/validate.py` checks the criteria below end to end. The ground truth per ear is a smooth tilt, plus the ISO 226 contour, plus a +4 dB excess 1/2 octave wide at 4 kHz, a +5 dB peak at 6.2 kHz (Q 6), a −3 dB dip at 8.4 kHz (Q 10) and a +3 dB peak at 11 kHz (Q 4). Answers come from the same probit model, with σ₀ = 1 dB, d₀ = 0.5 octave and λ = 3%.

The criteria: each feature recovered within 1/24 octave in frequency and 1.5 dB in depth, and no false correction above 1.5 dB, after 600 trials per ear. The depth tolerances were relaxed from 1 dB, since the listener's own loudness discrimination is the practical limit. These aren't all met yet: very narrow features (Q 12 to 16) and those near the top of the range (around 11 kHz) come out about 1 to 2 dB shallow, and a null ear can get stray filters up to about 1.3 dB.

### Seating and multi-headphone scenarios

`flatiron simulate --scenario seating` and `--scenario consensus` run the criteria below for what's built, through the real scheduler, for one simulated listener (`--seed`). The command runs the standard headphone scenario; the others described below are `SCENARIOS` in `sim/consensus.py`. The rule for passing a multi-headphone scenario: the listener's own features are never corrected, and only headphone-specific ones are.

Ground truth per ear:

| Component | Contents |
| --- | --- |
| T (listener) | +6 dB at 7 kHz (Q 8), +5 dB at 11 kHz (Q 10), −4 dB at 8.4 kHz (Q 12) |
| cup (all three headphones) | +4 dB at 9.5 kHz (Q 6) |
| hp, headphone 1 | +5 dB at 5.8 kHz (Q 5) |
| hp, headphone 2 | −4 dB at 8.0 kHz (Q 10) |
| hp, headphone 3 | +3 dB at 10.5 kHz (Q 4), +3 dB at 3.2 kHz (Q 3) |
| Seatings | `reseat()` moves feature frequencies, and can add a deviation with a set spread and resonances only that seating has |
| spk, speaker 1 | **NOT BUILT.** Random narrow ripple, 3 dB RMS above 3 kHz, plus a +6 dB peak at 6.3 kHz (Q 6) |
| spk, speaker 2 | **NOT BUILT.** Independent random ripple, 3 dB RMS |
| Positions | **NOT BUILT.** u\_g with τ\_pos 1 dB below 5 kHz rising to 4 dB at 12 kHz; 20 positions per ear, 24 trials each |

Each headphone gets its own smooth tilt (−1, +0.5 and −0.3 dB per octave), and every source gets the ISO 226 contour. Headphones get 600 trials per ear, over three sessions of 200, as in the existing validation.

**NOT BUILT:** Speaker 1's own peak at 6.3 kHz is there to check that one speaker's colouration never reaches a headphone correction. A variant moves that peak to 9.5 kHz, on top of cup, to check the split-speakers case: one speaker shows a shared headphone feature and the other doesn't.

Criteria, measured over 14 to 21 simulated listeners for what's built:

| # | Criterion | Result | Status |
| --- | --- | --- | --- |
| 1 | **Comparison report.** Every hp feature is flagged against the right headphone within 1/24 octave. No T or cup feature is flagged as one headphone standing out. False flags stay at the chosen target rate | 11 of 56 headphone features missed; 1.9 flags per ear on shared features and 0.7 elsewhere | Partly met: flags follow the veto, so they share its limits, including features merged with a neighbour |
| 2 | **Headphones-only consensus.** Every hp feature is corrected within 1.5 dB. T and cup are left alone (cup is invisible without speakers, as designed). No false correction above 1.5 dB | See the table below | Not met by any configuration |
| 3 | **Both speakers, medium trust.** As in 2, and cup is also removed from all three headphones within 2 dB. T and speaker 1's 6.3 kHz peak leave no correction | — | **NOT BUILT** |
| 4 | **Split-speakers variant.** With speaker 1's peak moved onto cup, cup is kept, matching the "one shows it, one lacks it" row of the evidence table | — | **NOT BUILT** |
| 5 | **One speaker.** With speaker 2 only, any removal of cup is marked in the report as resting on one device | — | **NOT BUILT** |
| 6 | **Low trust.** Results match criterion 2 within 0.5 dB | — | **NOT BUILT** |
| 7 | **Speaker model.** L recovers the mean over positions within 1.5 dB RMS from 1 to 13 kHz, at both 20 × 24 and 8 × 60 trials. Its reported SD covers the error about 68% of the time over 50 simulated listeners. It also runs with the fixed τ\_pos at half the true curve, to show how much a wrong assumption costs | — | **NOT BUILT** |
| 8 | **Rule and edge cases.** Two-profile consensus runs and warns, and every row of the veto table comes out as specified (unit tests on the rule alone). Three-headphone cases come out near the worked values | Every row of the table; the worked values; the two-profile warning | Met |
| 9 | **Seating spread.** With three sessions of about 110 trials per ear, a true τ of 2 or 3 dB is recovered within a factor of 1.5, and a true τ of zero isn't estimated above 1 dB | True 2 and 3 dB within a factor of 1.5 in 79% of runs. True 0: below 1 dB in 64% above 8 kHz (the prior's median there is 1.5 dB) | Partly met |
| 10 | **Coverage.** L's reported SD covers the error about 68% of the time at 1, 3 and 8 sessions, and m's SD does the same for the consensus | L's 1 SD covers 66%, 78% and 74% at 1, 3 and 8 sessions; the consensus SD is within 1% of Monte Carlo | Met, a little conservative |
| 11 | **Moving resonances.** A 9 dB resonance in two seatings of three is corrected within 2 dB of its median level. A resonance in one seating of three is dropped. With one session, a seating-specific resonance of up to about twice the prior's spread isn't corrected as confident | 9 dB in all three seatings: −8.0 dB. In two of three: −5.5 dB (within 2 dB of −8 in 43% of runs). In one of three: median −2.1 dB, with some correction left in half the runs. 3 dB in one session: left alone in 86% | Partly met: the Gaussian deviation shrinks on-or-off resonances |
| 12 | **Speed.** A per-answer refit takes under 100 ms at 8 sessions of 150 trials on a fully refined 199-point grid, and a block-end pause is no longer than before the hierarchical fit | 10 ms per answer at 8 × 150 trials on 199 points; about 1 s at a block end; about 2 s for a full fit | Met |

For criterion 2, passes out of 42 headphone checks over 14 simulated listeners, each with its own features corrected within 1.5 dB, shared ones left alone, and nothing else corrected above 1.5 dB. *Broad* gives each headphone its own feature about an octave wide and doubled tilts. *Contour* has a listener off ISO 226 by −4 dB near 2.5 kHz and +11 dB near 9 kHz. *Moved* puts the shared 7 kHz peak up to 0.2 octave apart between headphones, and doesn't judge that peak, since moving it is allowed:

| Configuration | Standard | Broad | Contour | Moved |
| --- | --- | --- | --- | --- |
| Pointwise veto, ISO 226 | 29 | 13 | 2 | 12 |
| Veto, ISO 226 | 14 | 9 | 2 | 9 |
| Pointwise veto, consensus of all three | 18 | 7 | 16 | 13 |
| Veto, consensus of all three | 14 | 4 | 13 | 10 |
| Veto, consensus of the other two | 10 | 0 | 12 | 8 |
| Consensus of all three, unsmoothed, no veto | 7 | 3 | 8 | 4 |
| Consensus of all three, smoothed, no veto | 0 | 0 | 0 | 6 |
| ISO 226, no veto | 0 | 0 | 0 | 0 |

The scoring favours leaving things alone and can't tell whether a move is right for a real ear. An import limit of 0 scores the same as 1.5 dB, within one pass.

hp2's 8.0 kHz dip and the listener's 8.4 kHz dip are 0.07 octave apart, inside the 1/12-octave stimulus, so they often can't be told apart.

### Prototype findings

These come from scratch code, not in the repository, run on the `sundara` and `sundaratone` profiles (three or four sessions per ear, 31 to 120 trials each) and on simulated listeners. They guided the design and aren't a substitute for the criteria above. The prototype held noise fixed in its refits and used a looser convergence tolerance than the fit that was current then, a single pooled curve per ear that the hierarchical fit replaced. Its results for speakers are under Speaker profiles.

**Refit after one new answer:**

| Case | Pooled fit | Hierarchical |
| --- | --- | --- |
| Real data: 3 sessions, 108 points | 25 ms | 6 ms |
| 8 sessions × 150 trials, 100 points | 28 ms | 14 ms |
| 8 sessions × 150 trials, 199 points | 151 ms | 33 ms |

**Seating spread on real data:**

- A constant spread of 2 dB or more across frequency is clearly rejected. Spreads under about 1 dB can't be told apart from none.
- Spreads of 0.5 to 1.5 dB change the median SD by −1% to +18%. The pooled fit's SDs were already 1.2 to 2.0 dB at these trial counts, partly because σ₀ absorbed some of the seating differences.
- Treble spread, tested against simulations with no spread: sundaratone R is clear (none of 30 matched it), sundara L suggestive (10% matched), and the other two show none.
- With sundara L and τ fixed at 0.5, 1 and 1.5 dB, the prototype keeps 5, 4 and 3 of the pooled fit's 6 features (with no seating step applied), losing the smallest ones first.

**Grid, on sundara L** (108 points, 399 on a 1/96-octave grid; pure noise, k = 2):

| Flagging rule | False flags per ear |
| --- | --- |
| Own grid, 2 adjacent points | 1.04 |
| Own grid, 1 point | 2.67 |
| 1/96 grid, 2 adjacent points | 2.61 |
| 1/96 grid, width ≥ 1/24 octave | 1.51 |
| 1/96 grid, width ≥ 1/12 octave | 0.57 |

- The covariance has rank 106 on both grids. Neighbour correlation rises from 0.72 to 0.98 on the finer one.
- Consensus SD, measurement noise only, compared with Monte Carlo: per-frequency weights give 0.19 dB against 0.43 dB with τ\_hp = 0, and 0.28 dB against 0.42 dB with τ\_hp = 1 dB. The covariance formula under Comparing profiles matches Monte Carlo in both. This test ran on 1/96-octave curves, without robust reweighting.

### Soft rejection

These come from scratch code, not in the repository, on `ft1`, `hd560` and pooled `sundara`, comparing the fit with and without treble noise (r). The table gives the largest target difference between the two per ear, averaged over six ears on default settings, with "no gate" accepting every narrow lobe.

| Width | 0 | 0.5 | 1 | No gate |
| --- | --- | --- | --- | --- |
| Largest target difference | 0.73 dB | 0.48 dB | 0.43 dB | 0.46 dB |

Marginal flips are about a third of the peak change and about 13% of the RMS change at width 0.5; the rest is the curve moving. With the veto on, the jumps are veto flips, which this doesn't touch. The filter fit, which then stopped within 2 SD of the target, jumped by itself by up to 2.9 dB with no change in the target, and soft rejection made that worse: the mean largest step went from 0.55 to 1.20 dB. Under Filter fit below is what changed.

On known curves (40 simulations for each of six ears), two runs from the same curve differ by 1.78 dB RMS in the target when hard, 1.76 at width 0.5 and 1.74 at width 1, with error against the truth unchanged. Soft rejection doesn't reduce false left/right imbalance: that's measurement noise.

### Soft veto

These come from scratch code, not in the repository, with `sundara`'s real settings (a consensus reference and a veto by `ft1` and `hd560`). The hard veto's target jumps between a fit without treble noise and one with it (r) came from four edges: near-ties in the median (1.24 and 1.09 dB), a voter lost from the overlap check after a peak jumped across a two-dip lobe (2.23 dB), and the 1/12-octave limit (0.52 dB).

Soft rejection at width 0.5 is on in the soft rows. Interpolating the fit between the two models in 40 steps, largest values in dB, left and right:

| Veto | Largest target step | Target, one model to the other | Filters, one model to the other |
| --- | --- | --- | --- |
| Hard, no soft rejection | 2.23, 1.09 | 2.44, 1.29 | 2.35, 1.16 |
| s = 0.05 | 0.02, 0.02 | 0.61, 0.73 | 0.65, 0.70 |
| s = 0.1 | 0.02, 0.02 | 0.47, 0.71 | 0.37, 0.58 |
| s = 0.2 | 0.02, 0.02 | 0.45, 0.68 | 0.45, 0.49 |

That's one path, so it doesn't show the target is continuous everywhere.

On known curves (40 simulations for each ear of all three profiles, averaged over `sundara`'s two ears, no error bars, one fit of 240 not converged), in dB RMS:

| Setup | Two runs from one curve: target | Filters | Target error against the truth | Target size |
| --- | --- | --- | --- | --- |
| Hard | 1.16 | 0.91 | 0.98 | 1.98 |
| Soft rejection only | 1.15 | 0.89 | 0.95 | 1.97 |
| s = 0.1 only | 1.07 | 0.81 | 0.89 | 1.98 |
| Both, s = 0.1 (the default) | 1.05 | 0.80 | 0.86 | 1.97 |
| Both, s = 0.2 | 1.02 | 0.77 | 0.83 | 1.95 |
| Pointwise, soft rejection | 1.01 | 0.74 | 0.79 | 1.75 |

The truth is each setup's own correction of the true curves. So the soft veto helps modestly, and most of the difference between runs is measurement noise. Pointwise scores about as well but corrects about 12% less overall, and it still has a target step of 0.88 dB on the same path.

### Filter fit

These come from scratch code, not in the repository. The fit used to weight by 1/SD and stop within 2 SD, so it could drift up to 2.55 dB from the target in the treble, where the SD is largest. Along the path above, over six ears on default settings and `sundara`'s two on its real settings, with soft rejection at width 0.5 and s = 0.1, 7 of 9 jumps over 0.5 dB in the narrow filters came with the filter count changing by one. Removing the 0.5 dB minimum, or lowering the compensation threshold, made no difference. Starting each step from the last one's filters cut the 9 to 2, so many nearly equal solutions contribute too.

Largest values in dB, in that same setup, just before a last small change to the soft veto (a voter with this feature's shape now leads to no correction):

| Rule | Largest total step, mean over ears, worst | Filters to target, RMS, worst | Narrow filters, most | Opposed pairs per ear |
| --- | --- | --- | --- | --- |
| 1/SD, within 2 SD (before) | 1.06, 2.93 | 0.46, 2.55 | 5.7, 8 | 0.37 |
| Plain dB, within 1 dB | 0.25, 0.77 | 0.26, 0.98 | 7.3, 11 | 0.98 |
| **Plain dB, within 0.5 dB** | 0.34, 0.77 | 0.14, 0.50 | 10.3, 14 | 0.98 |

The 1 dB row has the smaller steps on this path, but a feature's filter then appears only when its target passes 1 dB, about 1.4 dB at once on a single peak, against 0.7 dB at 0.5 dB. An opposed pair is opposite gains within 1/3 octave together reaching under 60% of the larger. They usually follow an asymmetric target, and one of 16 fits on real data had a truly cancelling one (±8 dB together reaching 0.8 dB). Export rounding moves any fit's response by at most 0.02 dB. Replacing a pair with one filter almost never kept the fit.

On known curves (40 simulations per ear, RMS dB; one fit of 240 not converged):

| Settings | Rule | Filters to truth | Two runs from one curve, total | Narrow filters, most |
| --- | --- | --- | --- | --- |
| Default | Before | 1.54 | 0.98 | 5.3, 9 |
| Default | Within 0.5 dB | 1.48 | 1.00 | 9.8, 18 |
| `sundara`'s | Before | 0.89 | 0.87 | 5.5, 8 |
| `sundara`'s | Within 0.5 dB | 0.89 | 0.87 | 8.9, 12 |

So accuracy and false left/right difference don't change: the rule keeps the filters on the target, and the noise is in the target.

The stop once also counted the fade above f\_hi. An error there can't close, so about 9% of runs at 0.75 dB used 14 to 20 narrow filters on a chain of Q 16 filters below 13 kHz, and with all 20 bands gone they lost compensation, moving the total by up to 5 dB. With the stop inside the range and bands held back, no run reaches the budget of 24.

With the budget tightened on the real fits, compensation is about as close to its goal at 6 bands as at 32 (mean RMS 0.12 dB), and only the narrow corrections suffer, with the largest error against the target rising to about 3 dB.

### Treble noise

r was checked on simulated answers from each real profile's own trials and fitted curve, 40 simulations for each of six ears, against the same fit with r held at 1.

| True r | Fit | L RMS error (below 8 kHz, above) | 95% coverage | Median r read |
| --- | --- | --- | --- | --- |
| 1 | r = 1 | 1.47 dB (1.42, 1.54) | 96.6% | |
| 1 | r fitted | 1.48 dB (1.41, 1.60) | 96.4% | 1.13 |
| 2.4 | r = 1 | 1.83 dB (1.73, 2.00) | 96.4% | |
| 2.4 | r fitted | 1.79 dB (1.55, 2.26) | 96.0% | 2.19 |

Fitting r costs almost nothing when it isn't needed, but it doesn't help uniformly when it is: the curve below 8 kHz gains 10%, and the treble loses 13%. The likely reason, not verified, is that noisier treble answers leave more to the smoothness prior. r reads about 13% high when the truth is 1, so the values of 1.25 to 1.4 on the real data aren't evidence of a rise.

### Noise variants not adopted

These come from scratch code, not in the repository, run per ear on `ft1`, `hd560` and pooled `sundara`: six ears and 2,662 trials. ΔlogML is the summed change in evidence, and held-out is the summed change in log-likelihood over 5 folds repeated 4 times, using the posterior-averaged prediction. Treat the numbers as a record of why the model looks as it does: they were not re-run as the model changed.

Against the model without r:

| Variant | ΔlogML | Held-out | Outcome |
| --- | --- | --- | --- |
| Separate noise above 8 kHz | +13.8 | +15.3 ± 4.3 | Adopted as r. Almost all of it comes from `hd560` L and `sundara` L; bootstrap p about 0.05 after searching over cutoffs |
| Noise linear in the gap, octaves | +1.0 | +0.1 ± 1.2 | No effect. Trial gaps cluster at 1/8 and about 0.9 octave, so the form is barely testable |
| Noise linear in the gap, Hz | +9.3 | +4.9 ± 5.9 | Mostly mimics the treble effect and hurts right ears |
| Separate time-order bias on replay trials | −0.5 | +3.0 ± 5.0 | b reads about 0.6 dB lower on replay trials, which looks real, but it doesn't help prediction |
| Separate noise on replay trials | +30.8 | +16.3 ± 6.5 | Not adopted, see below |

**Replay noise.** Noise on replay trials reads 1.5 to 2.6 times higher in all six ears, yet it isn't evidence that replaying adds noise. A replay is chosen after hearing the pair, so replayed trials are selected on the noisy percept. A simulated listener with fixed noise who replays near-zero percepts, with half the noise repeating, reproduces a ratio of about 2. Total time since first playback carries the same information as replay (adding replay to it gains 0.0 held out), and overall noise doesn't rise with a session's replay rate. A causal effect isn't excluded: only randomising whether replay is available on each trial could settle that. Either way, the term barely changes the curve on simulated data: L RMS error goes from 1.38 to 1.40 dB if the cause is selection, and from 1.83 to 1.75 dB if replay really adds noise, with coverage unchanged. It improves prediction of the answers, not the measurement.

**Noise distribution.** The probit link was compared against other shapes, each scaled to the same interquartile range, on the model with r:

| Link | ΔlogML | Held-out | Largest L change |
| --- | --- | --- | --- |
| Logistic | +0.7 | +2.9 ± 1.1 | 0.23 dB |
| Student-t, ν = 5 | +0.6 | +3.3 ± 1.5 | 0.31 dB |
| Student-t, ν = 3 | 0.0 | +3.0 ± 2.6 | 0.48 dB |
| Mixture, 30% of trials 3× wider | −0.7 | +2.0 ± 3.2 | 0.74 dB |
| Cauchy (ν = 1) | +2.3 | −31.3 ± 8.6 | |
| Generalised normal, shape 3 (lighter tails) | −3.4 | −5.6 ± 1.5 | |
| Generalised normal, shape 4 (lighter tails) | −6.5 | −10.2 ± 2.3 | |

Those are 7 of the 14 alternatives tried. Much heavier and lighter tails both predict clearly worse. Moderately heavier tails gain about 3 nats held out, but simulated Gaussian listeners gain as much evidence about half the time (p about 0.55), and no held-out null was run, so that gain isn't calibrated. No tail absorbs the replay ratio, which grows slightly with heavier tails. The link stays Gaussian: at most 0.3 dB of curve change for logistic or ν = 5 doesn't justify the added complexity.

**Hard against easy trials.** A Gaussian listener errs more on hard trials, but that's already in the distribution, so a width fitted separately to each should match. Difficulty was the margin predicted from the other trials, so it's defined by the stimulus rather than by the percept. The ratio of width on hard trials (|z| < 1) to easy ones has a geometric mean over ears of 1.11, against 1.17 (5th to 95th percentile 1.04 to 1.42) from simulated Gaussian listeners, and 1.25 (1.04 to 1.69) from simulated listeners who also replay near-zero percepts. The expected ratio is above 1 because the sorting uses an estimated margin. Pooled by margin, observed agreement tracks the prediction, with one exception out of six bins: at a margin of 0.5 to 1σ there are 16.5 fewer errors than predicted, outside the simulated 5th to 95th percentile range of −7.9 to +16.8.

### Unit and truth tests

- The peaking biquad matches the RBJ cookbook.
- Stimuli stay in band at unit RMS, and nothing leaves above the ceiling.
- The filter fit recovers known filters, and the tone synth glides and ramps without steps.
- Exporters match golden files from real LSP and EasyEffects installs byte for byte.
- When available locally, the LSP and EasyEffects output runs through LSP's LADSPA plugin, and the PipeWire drop-in through a private filter-chain, compared with Flatiron's prediction.

## Known limitations

- The MAP isn't unique: the lapse term makes the posterior non-concave, so two nearby solutions can exist. On one simulated data set, moving σ₀ from 1.12 to 1.15 dB moved the curve by up to 1 dB, with nothing else changed. Which solution a fit lands on can depend on small changes to the noise parameters, which is one way a feature near the threshold can flip. Over 45 seeds this averages out (0.66 dB shape error, 1.12 worst).
- **The baseline can tip.** The robust baseline's Tukey weights depend on residual/SD. When a block wider than W sits far off the reference, the baseline either rises to meet it or rejects it as a feature, and the choice can turn on the error bars' scale. Two solutions coexist near the edge. On SundaraTone L against ISO 226, 6 to 13 kHz sits 8 to 15 dB above 4 kHz. Scaling the SDs by 0.875 against 0.9 moved the baseline at 13 kHz from 3.5 to 7.6 dB, and the target by up to 6 dB, merging the 7 and 11 kHz peaks. Sundara L is as close to the edge (3.5 dB per 2.5% of SD) and HD560 R is moderately sensitive (1 dB); FT1 isn't. Any change that shrinks the error bars can tip it, the channel-balance prior included, and so can a new session. Against the consensus reference the block is mostly in the reference and the baseline is stable (0.4 dB at most per 2.5%). `--baseline joint` (experimental, above) reduces the largest step to 0.25 dB on the real ears. Tried and rejected: scaling the robust weights by the residuals' own spread (MAD, or a lower quantile), held from the Huber pass. That makes the baseline exactly immune to the error bars' scale. But on the real ears even the lowest tenth of the residuals sits 1.2 to 7 SDs off the stiff baseline, so no noise-only scale exists. The cutoff in effect grows several-fold, the baseline bends into the features, and targets move up to 4 dB on ears that never tipped (FT1). On known curves it was slightly worse throughout (target +0.03 dB RMS, +0.08 false corrections per ear). The simulated listeners hardly ever tip (0.5% of them by over 1 dB per 2.5% of SD): they follow ISO 226 closely. The multi-headphone simulator's "contour" scenario has the real condition. Also tried and rejected: a rolling-ball (morphological) baseline, the average of opening∘closing and closing∘opening with a flat element of width 0.5 to 2 W on the lightly smoothed curve, then smoothed. It can't tip: on the real ears it ignores the error bars' scale entirely, and jittering the curve by 0.3 SD moved it at most 0.9 dB (up to 5 dB for the spline). But a flat element only clips a feature where it's narrower than the element, so a feature's skirts stay in the baseline. On known curves, at every width tried, it corrected less of each feature: about 45% against 60%, and dips worst (median 0.33 against 0.69). Feature error rose 0.5 dB and target error 0.1 to 0.2 dB RMS, though with fewer false corrections. In the contour scenario against ISO 226 it was about even with the spline at W (narrow-correction error 2.3 against 2.4 dB) and worse when wider, while the consensus reference brought either to 1.0 to 1.1 dB. The reference matters far more there than the baseline does.
- Broad corrections (with `--broad`) are only as good as the reference. Individual ears can sit several dB off ISO 226 in the ear-gain region, which is why tonality is kept by default.
- Fit variability above about 8 kHz is high. Reseats and the seating model mitigate it; the correction range stops at 13 kHz by default and shouldn't go below 12 kHz.
- The diffuse-field reference needs a user-supplied ISO 389-7 difference table.
- Equaliser's (macOS) handling of imported Q and filter mode is unverified.
- **The veto depends on detection.** Which features exist, and how wide they are, decides what matches, and a feature's own part can stay merged with a neighbour's. The pointwise mode avoids this, but its separate baselines make it sensitive to broad differences between headphones.
- **Nothing says whether a veto's answer is right for a real ear.** The simulator can only check its own assumptions about what's shared.

## Open questions

- **Seating priors and shape.** The τ prior medians and the two-value shape need tuning on more data. Criterion 11 is accepted as partly met for now; if a Gaussian u\_g keeps handling on-or-off resonances badly, even with the median rule, a heavy-tailed u\_g is the next thing to try.
- **Minimum widths.** 1/12 octave may miss the narrowest features at k = 2, so k and the width should be chosen together. The standard narrow test has a related weakness: "two adjacent points" means 1/24 octave on most of a fit's grid and 1/48 where it's refined. Should it move to a width in octaves too? That would change existing corrections.
- **τ\_hp floor.** 0.5 dB is a judgment call, so the most-measured headphone can't dominate where the profiles happen to agree. Tune it in the simulator.
- **IEMs in the consensus.** Excluded by default, because they lack the pinna and concha. They could be allowed as hearing-only evidence, separating hearing from ear acoustics. Not needed for corrections, so deferred.

## Speaker profiles (NOT BUILT)

**NOT BUILT: nothing in this section exists in the code.** There's no `speaker` profile kind, block runner, position model, joint model or speaker simulation, and none of the commands, options or file fields below exist. The only trace is two placeholders, `speaker_trust` and `speaker_response`, always null, in the consensus cache key. This is the design to build from. In this section a group is a speaker block.

Speaker profiles answer the same question as the comparison and consensus: which narrow features belong to the listener, and which to the headphone. They'd add one thing the headphones can't show: a feature every headphone shares may still be an over-ear coupling artifact. Speakers would be downstream of the cached perceptual fits, so like every other setting they could be switched on and off without new trials.

### Speaker sessions (NOT BUILT)

A speaker profile is measured in short one-ear blocks, each at a different position. The hierarchical model averages the positions into one direction-averaged curve per ear instead of rejecting what varies.

**Setup.**

- `flatiron profiles new NAME --kind speaker`, with the speaker's label and which output channel drives it (`--channel L|R`). Only one speaker plays. Two speakers playing the same burst would comb-filter at the ear.
- Distance at least 0.7 m where the level allows (a phone at arm's length is acceptable but logged as near field). The level screen runs as usual. It warns if the set level is unusually high, since speakers need more drive than headphones and the room is open.
- A quiet room. The level screen adds a one-off check: the listener confirms they can't hear the burst with the speaker muted, which catches hum or fan noise near the stimulus level.

**Blocks.**

- One block = one session file = one open ear and one position. The default is 24 answered trials, about a minute (`--block 16–60`).
- Before a block, the screen names the open ear and asks for a foam earplug in the other ear, fully inserted. Ears alternate every block: a block on L, then on R at a new position, and so on.
- Position plan: the screen suggests the next position from a rotation meant to spread directions around the open ear. Front, 30° and 60° toward the open side, side-on, rear quarter (turn the chair), plus one higher and one lower placement. The listener can accept it or type their own label. Optional numbers (azimuth, elevation, distance) are stored when given.
- Prefer turning the head or chair over moving the speaker. The speaker then always faces the listener, so its own on-axis colouration stays constant across positions instead of adding variance.
- Suggested minimum: at least 8 positions and about 400 to 500 trials per ear in total, such as 20 blocks of 24 or 8 of 60. About 200 trials per ear was too few in simulation (see the results below). The report shows how much more trials or positions would tighten the curve.
- Anchor pairs: about a quarter of each block goes to a small fixed set of frequency pairs repeated across blocks, so that differences between positions can be observed directly (see the open questions below).
- No seating step. Positions are supposed to differ, and that variation is modelled instead.

**Trials.**

- Same stimuli, Δ, rove, catch trials, undo and reaction-time logging as headphone trials. Bursts go to the speaker's channel. The trial's ear is the open ear.
- Adaptive selection runs on the pooled speaker model (below), not per block, so a block's trials are spent where the average curve is least certain.
- The channel check is replaced by a plug prompt. A plugged ear can't be verified by listening, so it's a confirmation only.

### Positions as random effects (NOT BUILT)

This is the perceptual model with blocks as the groups. Per ear, the perceived level at block g is L(f) + u\_g(f):

- L is the quantity of interest: the average over the directions the position plan is meant to cover, with each position weighted equally. It has the same priors as a headphone L.
- u\_g is block g's deviation, with amplitude τ\_pos(f) and correlation width ℓ = 1/12 octave.
- With 24 trials a block can't pin down its own u\_g. The model doesn't need it to: L's uncertainty includes τ\_pos² divided by the number of positions. That's what "averaged, not rejected" means in practice.
- τ\_pos is fixed rather than estimated at this block size, by default at 1 dB below 5 kHz rising to 4 dB at 12 kHz. Estimating it alongside L from 24-trial blocks doesn't work: in simulation it ran up to many times the true value, and a prior would only hide that. The fixed curve is an assumption, so the report labels it as one. Estimation (with a log-normal prior, log SD 0.3, around that curve) is enabled only if the simulator shows that anchor pairs make it reliable.
- The result is a perceptual fit like any other (L̂ with covariance), so `report`, `report --compare` and the pipeline can treat it as a profile. Its kind marks it as speaker evidence: it's drawn dashed in comparisons and doesn't vote by default.

The SD covers the sampling of positions and the judgment noise. It doesn't cover bias from the plan itself: the rotation isn't a random draw of directions, so if it misses a region, nothing in the error bars shows that.

Averaging in dB fills in a moving notch less than a true diffuse-field (power) average would. The open questions below cover whether that's worth correcting.

### Folding speakers into the reference (NOT BUILT)

Speakers and headphones go into one joint model. Speakers are evidence about one thing only: whether a feature every headphone shares is really the listener's. They can demote a shared feature to a coupling artifact. They never create a correction target by themselves.

Every residual (narrower than W) is a sum of parts. Hearing sensitivity and the ear canal are in all of them and fold into T. Speakers are the only sources without cup, so that's the one place they change the outcome.

| Source | What its residual contains |
| --- | --- |
| Headphone h | T + cup + hp\_h + noise |
| Speaker s | T + spk\_s + noise (noise includes position spread) |

- **T**: the listener's own structure. Direction-averaged pinna and concha response, canal and hearing. This is what a correct headphone should reproduce.
- **cup**: over-ear coupling structure common to all the headphones (how a cup loads this pinna). Invisible to a headphone-only consensus.
- **hp\_h**: headphone h's own aberrations. Sparse: most frequencies none, a few real peaks and dips.
- **spk\_s**: speaker s's colouration plus the room. Unknown, and for cheap speakers possibly large.

A headphone-only consensus estimates T + cup. Speakers are the only data without cup, but they carry their own spk\_s. So speaker data is useful exactly where it's trusted to say "this shared headphone feature isn't on the listener's side."

**The joint model.** Per ear, estimate T, cup, every hp\_h and every spk\_s together by MAP:

- All these parts live on the common 1/48-octave grid. Each source is compared with them on its own grid through A\_p, with its residual covariance as its noise. On a source's own grid that covariance is full rank except for the level and slope the baseline removes, which a pseudo-inverse excludes. On a finer grid it couldn't be used at all: most patterns would have no modelled noise, and so infinite weight.
- hp\_h and spk\_s get heavy-tailed priors (Student-t, 3 degrees of freedom, scales w\_hp and w\_spk), so a few large features are cheap and broad noise isn't. With several headphones, this is the soft median in a different form.
- cup gets a Gaussian prior with a small scale w\_cup (default 1.5 dB). Without strong evidence, cup stays near zero and shared headphone features are attributed to T and kept.
- T gets only the smoothness prior.
- Prior terms are weighted by the octave width each grid point covers, relative to 1/24 octave as in the soft median, the unit the perceptual fit's curvature penalty is scaled to, so their strength doesn't depend on grid spacing.
- It's solved by IRLS for the Student-t terms, with covariances from the Laplace approximation at the solution.

This part was written before the consensus was split into a reference and a veto, and is to be revisited when speakers are built: the correction for headphone h is the pointwise rule from `--veto-mode pointwise`, with T̂ as the median. What h shows beyond T̂ is cup + hp\_h, and that's what gets removed.

How the evidence plays out:

| Headphones | Speakers | Result |
| --- | --- | --- |
| One shows a peak | Any | Peak attributed to hp\_h: removed from that headphone |
| All show a peak | Both show it | T: kept everywhere |
| All show a peak | Both confidently lack it | cup: removed from every headphone |
| All show a peak | One shows it, one lacks it | Mostly T (the t-prior treats the odd speaker as spk\_s): kept |
| All show a peak | Too noisy to say | cup stays near zero: kept |
| None show a peak | Both show it | T has a peak the headphones lack: not imported under remove-only |

**Trust, and what speakers never decide.**

- `--speaker-trust low|medium|high` scales w\_spk (6, 3 and 1.5 dB). Low means speakers almost never overturn the headphone consensus.
- Speakers inform only structure narrower than W. The broad shape of a speaker in a room isn't trusted at all unless its measured response is supplied (`--speaker-response FILE.csv`, subtracted before detrending). Even then it stays behind `--broad`.
- Two unrelated speakers count for much more than one. A colouration shared by a phone and a SoundSticks satellite is unlikely, so features they agree on are attributed to T. With a single speaker, the report says that shared-feature removals rest on one device.
- IEMs don't count toward T. They bypass the pinna and concha, so a peak they lack says nothing about whether it's the listener's. They're shown in comparisons as a diagnostic: a feature that persists at two insertion depths is in hearing, not in the ear's acoustics.

### What the speaker work adds (NOT BUILT)

| Command | New option | Does |
| --- | --- | --- |
| `profiles new`, `profiles set` | `--kind speaker`; `--channel L\|R` on `new` | Sets the profile kind and the channel that drives the speaker |
| `run` | `--block N` (speaker only, default 24), `--position LABEL` | Runs one speaker block; refuses `--sweep` on speakers |
| `report --compare` | `--include-kind speaker` | Lets speakers count, as `iem` can now |
| `config` | `--speaker-trust low\|medium\|high`, `--speaker-response FILE` | Speaker handling |

On disk:

- `profile.json`: `kind: speaker`, plus `speaker` (label) and `channel`. Settings gain the speaker trust and response.
- Speaker session files: `open_ear`, `position` (`label`, optional `azimuth`, `elevation`, `distance_m`), `speaker_channel`, and the plug confirmation. Trials are unchanged.

Modules:

- `fit/consensus.py`: the joint T/cup/hp/spk model.
- `session/`: a speaker block runner (plug prompt, position plan, anchor pairs, ear alternation).
- `sim/`: the speaker position model and speaker ground truth (see Validation).

Build order, continuing from what's built:

1. Speaker profile kind and block runner: plug prompt, position plan, anchor pairs, ear alternation.
2. The speaker position model, simulator criterion 7.
3. The joint T/cup/hp/spk model with speaker trust, simulator criteria 3 to 6.

### Prototype results for speakers (NOT BUILT)

Scratch code only, with simulated speaker pairs that were random rather than adaptive, and τ\_pos known:

| Positions × trials | RMS error, 1–13 kHz | Median SD | SD, 8–13 kHz | 68% coverage |
| --- | --- | --- | --- | --- |
| 8 × 24 | 2.14 dB | 1.93 dB | 2.80 dB | 0.70 |
| 16 × 24 | 1.47 dB | 1.24 dB | 1.88 dB | 0.66 |
| 8 × 60 | 1.22 dB | 1.15 dB | 1.70 dB | 0.71 |

- At 8 × 24, fixing τ\_pos at half the true curve only lowers coverage to 0.64.
- Estimating τ\_pos at 8 × 24 with L also free: a true 4 dB gives a median at the top of the tested range (14 dB), and a true 1 dB gives a median of 5 dB, ranging from 1 to 14. With L held at the truth, the Laplace approximation agrees with brute-force integration and centres near the true value, so the approximation isn't the cause. At 8 × 60 the median is 5 to 6 dB for a true 4 dB.

### Open questions for speakers (NOT BUILT)

- **dB vs power averaging over positions.** A diffuse field averages power, which fills in moving notches more than averaging perceived dB does. A power average of per-position posteriors is possible, but each position is barely measured, so it'd be noisy. Proposed: dB averaging, with the expected bias at notch frequencies estimated in the simulator before deciding.
- **Default w\_cup, and whether cup's prior should be Gaussian.** 1.5 dB keeps shared features unless both speakers clearly lack them. Too small and real coupling artifacts are never caught; too large and noisy speakers start removing the listener's own features. A rough hand calculation (one frequency, no noise, ignoring T's smoothness prior and hp\_h) suggests the bigger problem is the shape of the prior. With both speakers lacking a 4 dB shared peak at medium trust, the best split is about T = 2.6 and cup = 1.4 dB, which would miss criterion 3. At 8 dB, cup only reaches about 1.7 dB, because the Gaussian penalty keeps growing while the speakers' Student-t penalty levels off. Check this in a simulation before building the joint model, and try a heavier-tailed prior on cup. Then tune it, using the real data with `report --compare` as a sanity check.
- **Why τ\_pos runs away, and whether anchor pairs fix it.** The likely cause is that 24-trial blocks rarely compare the same frequencies, so differences between blocks can't be seen, but this isn't proven. Anchor pairs are untested. If they don't work, τ\_pos stays fixed and anchor trials go back to the adaptive scheduler.
- **Same-ear assumption.** Earplug attenuation and bone conduction leave the plugged ear about 30 dB down in the treble. That should be enough for loudness comparisons in the open ear, but worth a check: one block with the "plugged" ear actually open should look different.
- **Room.** The position spread absorbs room comb filtering along with HRTF variation. A small, reflective room may make τ\_pos large enough that speakers rarely move a decision. With τ\_pos fixed, the model can't see that, so the anchor-pair question matters here too. The report should show weak speaker evidence as weak, not hide it.

## Sources

- [PipeWire filter-chain documentation](https://docs.pipewire.org/devel/page_module_filter_chain.html)
- [LSP Parametric Equalizer x32 LeftRight (GStreamer LV2 listing)](https://gstreamer.freedesktop.org/documentation/lv2/lsp-plug-in-plugins-lv2-para-equalizer-x32-lr.html)
- [Equaliser for macOS (cvknage/equaliser)](https://github.com/cvknage/equaliser)
- [EasyEffects preset example with left/right bands](https://gist.github.com/ryuukk/1702e352b4de3217cad1e971cb46e181)
