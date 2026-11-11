---
title: "Flatiron — by-ear headphone resonance EQ"
subtitle: Design
---

# Overview

Flatiron is a local tool that finds the peaks and dips in a headphone's upper mids and treble, separately for each ear, from nothing but the listener's "which is louder?" judgments. Per ear, it fits a perceived-level curve, picks out the features that are significant and narrower than about an octave, turns them into RBJ peaking filters, and exports them for an EQ host: Equalizer APO / AutoEQ text, LSP Parametric Equalizer x32 LeftRight, EasyEffects (and through it Equaliser on macOS), a PipeWire filter-chain drop-in, and TinCan.

This document describes how Flatiron works as built, the evidence behind its design decisions, and the design of the one part that isn't built yet. The README covers day-to-day use.

**Anything not implemented is marked NOT BUILT**, so `grep 'NOT BUILT' DESIGN.md` finds every instance. Everything else describes the code as it stands.

| Part | Status | Where |
| --- | --- | --- |
| Seating as a hierarchical model | Built | "Perceptual model"; pipeline step 5 |
| The listener's noise, shared by every profile | Built | "Shared noise" |
| Channel-balance prior | Built | "Channel balance" |
| One baseline for both ears | Built | "Shared baseline" |
| Profile comparison | Built | "Comparing profiles" |
| Consensus reference, veto | Built | "Consensus reference"; "Veto against other profiles" |
| Smooth and match methods, joint baseline | Built, experimental | "Experimental methods" |
| Speaker sessions | **NOT BUILT** | "Speaker profiles" |
| Speaker evidence in a joint model | **NOT BUILT** | "Speaker profiles" |

## Scope

Flatiron does:

- Find features from about 1/12 octave wide up to a configurable maximum width $W$ (default 1 octave), between 1 kHz and 13 kHz by default.
- Measure left and right independently, so a feature in one ear doesn't leak into the other. (A prior that the two ears are alike shrinks their difference where the data are weak; see "Channel balance".)
- Use a two-alternative forced-choice task ("A or B louder?"), never knob-matching.
- Report how confident it is in each feature, and correct only features it is sure of.
- Keep every raw trial, so the correction can be recomputed with different settings without retesting.
- Compare several headphones on one listener, to separate what belongs to the listener from what belongs to one headphone.

Flatiron doesn't:

- Correct broad tilt, bass or the overall target curve. By default it keeps the headphone's broad shape and removes only narrow features (key decision 2).
- Flatten to equal perceived loudness.
- Do hearing-loss compensation or anything audiometric.
- Process system audio. It writes configs; an EQ host does the processing.

## How it works, in brief

1. **Measure.** In short sessions, the listener hears pairs of narrowband noise bursts in one ear and says which was louder. An adaptive scheduler picks pairs of nearby frequencies, plus occasional long jumps, where an answer would be most informative. The headphone is reseated between sessions.
2. **Model.** Every answer for an ear goes into one probabilistic model: a smooth perceived-level curve $L(f)$, a deviation from it for each seating, and a judgment-noise model. Its fit gives $\hat L$ with full error bars, and how much seatings differ.
3. **Isolate features.** A reference equal-loudness contour is subtracted, and a stiff, outlier-resistant baseline, one shape for both ears, removes everything broader than $W$. What's left, the residual, is split into lobes, and each lobe is tested for significance.
4. **Decide the target.** Significant features are corrected toward zero, scaled by how consistently they appear across seatings and capped. By default, broad compensation filters restore the headphone's original broad tonal balance. Optionally, other headphones' curves serve as the reference, or vote on which features are this headphone's own.
5. **Fit filters and export.** Peaking filters are fitted to the target and written for the chosen EQ host, checked against the real implementation where possible.

Nothing is fitted ahead of time: every command that needs the correction recomputes it from the stored trials and the profile's settings, and caches the expensive part.

## Key design decisions

These are numbered because code comments cite them.

1. **Reference, then detrend.** What gets measured is the headphone's response, plus the listener's equal-loudness contour (ear gain included), plus resonances that depend on the fit. Flattening all of that would cut the listener's natural ear gain. Flatiron subtracts a reference contour, then removes anything that varies more slowly than $W$. What's left is a candidate for correction.
2. **Keep the tonality by default.** Broad features (wider than 1/2 octave) in the ear-gain region depend heavily on the reference, and individual ears differ from ISO 226 by several dB there. Narrow features depend on it less, but they still depend on it: the baseline is too stiff to follow the contour, so a narrow peak on the contour's slope still changes height with the reference. By default Flatiron corrects narrow features only, including narrow features riding on a broad one, and adds broad compensation filters so the correction averages to zero over about $W$. The headphone's broad tonal balance stays as it was, and broad changes are left to the user's own EQ. `--broad-strength` above 0 switches to correcting broad features toward the fixed target as well, by that share.
3. **Compare neighbours, fit globally.** Comparing nearby frequencies is easier and less noisy than comparing far-apart ones. Every judgment goes into one maximum-likelihood fit per ear, which avoids the random-walk error of chaining neighbour comparisons. Judgment noise grows with frequency separation, so long-range comparisons count for less.
4. **Redundant comparison graph.** Each grid point is compared against points 1 to 3 steps away, plus about 25% longer jumps of up to an octave. The longer jumps pin down broad features that a chain of neighbour comparisons would smear out.
5. **Narrowband noise by default.** Pure sines inside headphones are very sensitive to seal and standing waves, and can fall between the peaks of a narrow notch. The default stimulus is 1/12-octave band noise; other bandwidths (1/24 octave, say), warble tones and pure tones are options. The model accounts for each trial's stimulus bandwidth, so sessions with different stimuli pool.
6. **Adaptive grid.** Start at 1/24 octave and refine to 1/48 where the fitted curve bends sharply (or around frequencies the listener marked), so narrow features don't fall between test points.
7. **Reseat and repeat.** Treble resonances move when the headphone moves. Sessions are short, with a reseat between them, and each session is a group in a hierarchical model: more trials in a session say more about that seating and little about seatings in general.
8. **One filter definition everywhere.** The fit, Flatiron's own preview EQ and every export use RBJ-cookbook peaking biquads. Each exporter writes the target's matching mode, and the formats that can be run locally are checked against the real implementation before they're written.
9. **Lazy fits from per-profile settings.** There is no separate fit step and no saved fit. Every command that needs the correction recomputes it from the profile's trials, settings and checkpoint. The expensive part, the perceptual fit, depends only on the trials and is cached, so a correction takes about 0.1 s once the cache is warm, and a few seconds after new trials.
10. **Separate the listener from the headphone.** The listener's own ear and hearing appear in every source, headphone or speaker. A headphone's aberrations appear only in that headphone. Over-ear coupling artifacts appear in every headphone but no speaker. The design separates those three while trusting any single setup as little as possible. Comparison, consensus and veto make the first split, between what all headphones share and what belongs to one. The second, using speakers to find coupling artifacts, is **NOT BUILT**.
11. **No hard edges where avoidable.** A correction that jumps by decibels when a statistic crosses a threshold is hard to trust and hard to verify by ear. Where a decision would otherwise be all or nothing (significance, the veto's choice of median feature, the baseline's reading of a broad region), Flatiron makes it gradual, and the filter fit tracks the target in plain dB so it doesn't add jumps of its own.

## Notation

Symbols are per ear unless they say otherwise. Frequencies are in Hz, widths and distances in octaves, levels in dB. The code column gives the name used in the source, where one exists.

**Measurement and perceptual model**

| Symbol | Meaning | Code |
| --- | --- | --- |
| $f$, $x = \log_2 f$ | Frequency, and log-frequency in octaves | `freqs`, `grid` |
| $W$ | Maximum feature width: anything broader is tonality | `max_width_oct` |
| $L(f)$, $\hat L$, $\Sigma_L$ | The average perceived-level curve over seatings, its estimate (posterior mode), and the estimate's covariance | `L`, `cov` |
| $g$ | A group in the hierarchical model: a session (a speaker block, in the NOT BUILT speaker design) | `group` |
| $u_g(f)$, $L_g = L + u_g$ | Session $g$'s deviation from $L$, and that session's own curve | `u` |
| $\tau_\text{seat}(f)$; $\tau_\text{lo}$, $\tau_\text{mid}$, $\tau_\text{hi}$ | How far a seating typically departs from $L$ at $f$; its values at 5, 8 and 12 kHz | `tau`; `tau_lo`, `tau_mid`, `tau_hi` |
| $\ell_\text{seat}$ | Correlation width of $u_g$ across frequency (1/12 octave) | `ell_oct` |
| $t$; $f_a$, $f_b$ | A trial; the centre frequencies of its intervals A and B | `f_a`, `f_b` |
| $\Delta_a$, $\Delta_b$ | Level offsets the scheduler applies to A and B | |
| $o_t$ | Every known level difference between A and B in trial $t$ | `offset` |
| $\bar L_{g}(f_a)$ | Session $g$'s curve averaged over the stimulus spectrum centred on $f_a$ | |
| $d_t$ | The pair's separation in octaves | |
| $\sigma_0$ | Judgment noise for a pair at the same frequency, below 6 kHz | `sigma0` |
| $d_0$ | The separation at which judgment noise has grown by $\sqrt 2$ | `d0` |
| $r$, $q(f)$ | Treble noise ratio (noise above 10 kHz as a multiple of $\sigma_0$), and the noise profile it defines | `treble_noise` |
| $\lambda$ | Lapse rate: the share of answers given at random | `lapse` |
| $\beta$; $s_t$ | Time-order bias (how many dB louder the first interval sounds); $+1$ when A played first, $-1$ otherwise | `bias_db`; `order` |
| $\delta(f) = L_\mathrm{L} - L_\mathrm{R}$ | The difference between the two ears' curves, under the channel-balance prior | |
| $\sigma_{\delta}$; $\ell_\delta$ | The prior SD of $\delta$ (1.5 dB below 5 kHz, 3 dB above 8 kHz); its correlation width (1/8 octave) | `BalancePrior` |

**Correction pipeline**

| Symbol | Meaning | Code |
| --- | --- | --- |
| $E(f)$ | The reference: the perceived-level shape a reference-correct headphone would produce, mean removed | |
| $D = \hat L - E$, $\Sigma_D$ | The curve against the reference, and its covariance | `D` |
| $b(f)$ | The baseline: the part of $D$ broader than $W$ (tonality) | `baseline` |
| $B$, $c$ | The baseline spline's basis matrix and coefficients | |
| $R = D - b$, $\Sigma_R$ | The residual, and its covariance | `residual` |
| $k$, $k_\text{broad}$ | Significance thresholds in SDs for narrow and broad features | `k`, `k_broad` |
| $w_\text{soft}$ | Soft-rejection width, in SDs | `soft_width` |
| $C(f)$ | The correction target, in dB (negative cuts) | `target` |
| $f_\text{lo}$, $f_\text{hi}$ | The correction range (1 to 13 kHz) | `f_lo`, `f_hi` |

**Several profiles**

| Symbol | Meaning | Code |
| --- | --- | --- |
| $p$; $\hat L_p$, $\Sigma_p$ | A profile; its curve and covariance on its own grid | |
| $A_p$ | The interpolation matrix from the common 1/48-octave grid to profile $p$'s grid | `interp_matrix` |
| $m(f)$ | The consensus: the soft median of the profiles' curves, on the common grid | |
| $\alpha_p$ | Profile $p$'s constant alignment offset in the consensus | |
| $w_{p,i}$ | The octave width point $i$ of profile $p$ covers, relative to 1/24 octave | |
| $\tau_\text{hp}$ | Between-headphone spread in the soft median: one value per ear and profile set | |
| $\kappa$ | Weight of the consensus's curvature penalty | |
| $R_h$ | Headphone $h$'s residual | |
| $F^\star$ | The veto's median feature, as a shape | |
| $\mathcal D_c$ | A veto candidate's total shape difference from the other candidates | |
| $\gamma$ | Veto softness | `veto_softness` |
| $I$ | Veto import limit, dB | `veto_import_db` |

**Speakers (NOT BUILT)**

| Symbol | Meaning |
| --- | --- |
| $T$ | The listener's own structure, which a correct headphone should reproduce |
| $\mathrm{cup}$ | Over-ear coupling structure shared by all headphones |
| $\mathrm{hp}_h$ | Headphone $h$'s own aberrations |
| $\mathrm{spk}_j$ | Speaker $j$'s colouration, plus the room |
| $\sigma_\mathrm{cup}$, $\sigma_\mathrm{hp}$, $\sigma_\mathrm{spk}$ | Prior scales: how large each of those parts is typically assumed to be |
| $\tau_\text{pos}(f)$ | How far a speaker position typically departs from the average at $f$ |

# Measurement

**In brief.** Each trial plays two short narrowband bursts in one ear and asks which was louder. The answers feed a hierarchical probit model, which estimates each ear's average perceived-level curve, how each seating departs from it, and how noisy the listener's judgments are. A scheduler uses the current fit to choose the next pair, and refines the frequency grid where the curve bends.

## Frequency grid

- Measured range 850 Hz to 15 kHz, a little past the default correction range (1 to 13 kHz) so the baseline and taper aren't distorted at the edges.
- Initial grid: 1/24-octave log spacing, about 100 points per ear.
- Refinement: log midpoints, down to 1/48 octave (see "Adaptive selection"). Each session saves its final grid per ear; the next session starts from it.

## Stimuli

| Parameter | Default | Options |
| --- | --- | --- |
| Type | Band-limited noise, 1/12 octave around the centre (`noise:12`) | Noise of any 1/N-octave band (`noise:N`); warble tone, FM at 8 Hz over 1/N octave (`warble:N`, default ±1/24 octave); pure tone (`tone`) |
| Noise token | Frozen per (frequency, seed); a new random seed per session | — |
| Burst length | 400 ms | — |
| Ramps | 20 ms raised cosine | — |
| Gap between A and B | 300 ms | — |
| Presentation | Monaural: one ear only, the other channel digital silence | — |
| Sample rate | 48 kHz, float32 | — |
| Level | Set per session on the level screen (default −30 dBFS RMS, at most −24) | — |

Noise bands are generated in the frequency domain (a shaped FFT, then its inverse) for steep band edges, and every stimulus is normalised to unit RMS before any level is applied. A trial's whole A-gap-B buffer is scaled under a −12 dBFS ceiling (then hard-clipped there as a last guard), so the limiter never changes the A/B difference.

The system volume stays at the listener's usual setting. Each session opens on a level screen that loops bands across the range and adjusts Flatiron's own level, starting from the last session's. The level can be changed mid-session, with a warning; changes are logged and every trial records its level.

Audio goes through one persistent PortAudio output stream with a fixed 40 ms buffer. PortAudio's "low" latency (about 9 ms) underflowed on PipeWire. The trial UI offsets its panel lights and reaction times by the stream's reported latency.

## Trial

1. The scheduler picks an ear (alternating), a pair of grid points and a level offset, applied to one randomly chosen interval.
2. Which stimulus plays first is random, and a whole-trial level rove (±2 dB) keeps the listener from anchoring on absolute level.
3. A, gap, B play; the listener answers A or B. Replays are allowed and logged. The answer can be undone: the trial is kept on file, marked undone, and left out of fits.
4. About 5% of trials are catch trials: the same frequency with a ±6 dB offset. They track attention.
5. Reaction time is logged from the end of B. Answers under 150 ms are flagged.

## Perceptual model

**In brief.** For each ear, the model's unknowns are a smooth average curve $L$, one deviation $u_g$ per session, and a handful of noise parameters. An answer is a noisy comparison of that session's curve at the two frequencies, so a trial informs both $L$ and its own session's deviation. Treating sessions as groups (a hierarchical, or random-effects, model) is what makes reseating useful: judgment noise is new on every trial, while all of a session's trials share one seating, so more trials in a session say more about that seating and little about seatings in general. Modelling that directly also works with a single session, where a consistency check run afterwards can't. The noise parameters are the listener's, so they're estimated once per ear from every profile together ("Shared noise"); the seating spread is each profile's own. Both are set by maximising the marginal likelihood (empirical Bayes); the curves are then the posterior mode, with error bars from the Laplace approximation.

### The likelihood

A trial $t$ in session $g$ depends on that seating's curve, $L_g = L + u_g$:

$$
P(\text{A louder}) = \frac{\lambda}{2} + (1-\lambda)\,\Phi\!\left(\frac{\bar L_g(f_a) - \bar L_g(f_b) + o_t + \beta\, s_t}{\sigma_t}\right),
\qquad
\sigma_t = \sigma_0 \sqrt{\frac{q(f_a)^2 + q(f_b)^2}{2}}\,\sqrt{1 + \left(\frac{d_t}{d_0}\right)^2}
$$

- $o_t$ is every known level difference between A and B: $\Delta_a - \Delta_b$, plus the gain of any EQ in effect (an active system EQ recorded for the session, or the preview EQ of a verify session).
- $\bar L_g$ is the curve averaged in dB over the trial's stimulus spectrum (11 points across a noise band; one point for tones), interpolated linearly in log-frequency from the grid.
- $s_t = +1$ when A played first and $-1$ when B did, and $\beta$ is how many dB louder the first interval sounds. The user's real data showed a strong first-interval preference, about +1 dB. $\beta$ has a $\mathcal N(0, 3^2)$ dB prior and is a fixed effect, so its uncertainty is part of $L$'s error bars.
- $d_t$ is the pair's separation in octaves, so judgment noise grows with separation (key decision 3).
- $q(f)$ lets listener noise rise in the treble: it's 1 below 6 kHz and $r$ above 10 kHz, log-interpolated between. Each tone brings its own variance, so a pair straddling the ramp gets a value in between. $r$ has a log-normal prior centred on 1 (log SD 0.5). On the real data it reads about 2.5 in two left ears and 1.25 to 1.4 in the other four, where it can't be told from 1 (see "Treble noise" under "Findings"). The ramp's position is fixed, not fitted: anything from 6 to 12 kHz fits about equally well, and a hard cutoff fits no differently. The ramp only removes dependence on exactly where an edge falls.

### Priors

- **Smoothness of $L$**: a grid-independent curvature penalty, equivalent to a 1 dB SD on second differences at 1/24 octave.
- **Level**: a very weak $\mathcal N(0, 100^2)$ dB prior on each point of $L$, so the fit is well-posed with few trials. The gauge is fixed by $\operatorname{mean}(L) = 0$.
- **Noise**: log-normal priors around $\sigma_0 = 1$ dB and $d_0 = 0.5$ octave; $r$ around 1; $\lambda$ around 2%.
- **Seating deviation $u_g$**: Gaussian, with amplitude $\tau_\text{seat}(f)$ and a squared-exponential correlation in log-frequency of width $\ell_\text{seat} = 1/12$ octave, centred across frequency because a constant offset cancels within a session. $u_g$ also picks up anything else that changes between sessions: the noise token, the listener's state, or the stimulus when profiles are pooled. A session with few trials is pulled strongly toward the average, and a well-measured one can stand further from it. $\ell_\text{seat}$ is fixed: on the real data the evidence shows no clear preference for a value. $\sigma_0$, $d_0$, $\lambda$ and $\beta$ are shared across an ear's sessions, so $\sigma_0$ doesn't have to absorb seating differences.
- **Seating spread**: $\tau_\text{seat}$ takes three values, $\tau_\text{lo}$, $\tau_\text{mid}$ and $\tau_\text{hi}$ at 5, 8 and 12 kHz, log-interpolated in log-frequency between them and constant beyond, per ear and profile, with independent log-normal priors (medians 0.5, 1.5 and 1.5 dB, log SD 0.7). The prior curve is the same as the two-value one it replaced, which was constant above 8 kHz; the third value lets a spread that keeps rising into the top octave, as a speaker's does, be represented (see "Three-knot seating spread" under "Findings"). With fewer than two sessions of at least 60 trials in the ear, $\tau_\text{seat}$ stays at those medians, so a single session's error bars allow for seatings not yet measured, rather than tightening without limit as trials are added. The medians are judgment calls. On the real data, treble spread is clear for one ear and profile, suggestive for another, and absent for the other two. About three sessions of 110 trials per ear can detect a spread of 2 dB or more, but can't resolve one under about 1 dB. The prior decides that range, and matters little there: $\tau_\text{seat} = 1$ dB raised the median SD by 2 to 12% at these trial counts.

### Estimating the noise and the seating spread

$\sigma_0$, $d_0$, $r$ and $\lambda$, and then $\tau_\text{lo}$, $\tau_\text{mid}$ and $\tau_\text{hi}$, are set by maximising the Laplace-approximated marginal likelihood, with $L$ and the $u_g$ integrated out (empirical Bayes; Nelder–Mead). The noise is maximised over every profile's evidence together (next section), $\tau_\text{seat}$ over the profile's own. The inner fits start from a fixed point, so the evidence is deterministic. The order and limits matter:

- **Not a joint MAP.** A joint MAP over the curve and the noise overfits: with a few trials per point, the curve separates the answers and $\sigma_0$ collapses.
- **Not with few trials.** A profile with fewer than 200 trials in an ear doesn't inform the noise, because the evidence underestimates $\sigma_0$ with fewer trials. Until some profile has that many, the noise, $r$ included, stays at the prior.
- **One pass, noise first.** The noise is fitted with $\tau_\text{seat}$ at its prior, then $\tau_\text{seat}$ once with the noise fixed. Searching both together, or repeating the pass, runs away at sessions of about 110 trials: $\tau_\text{seat}$ grows to several times the truth while $\sigma_0$ collapses. One pass leaves some of a large spread in $\sigma_0$, so a true 3 dB reads low.

The curves are then the MAP given these.

### Shared noise

**In brief.** How noisy the listener's judgments are, how fast that grows with separation, how much more in the treble, and how often they answer at random is the listener's, not the headphone's. So it's estimated once per ear from every profile together and used by all of them. A new profile starts with it, and its first sessions don't have to supply it.

- **What's shared.** $\sigma_0$, $d_0$, $r$ and $\lambda$, per ear (the right ear is noisier for every headphone so far). Not the seating spread, which belongs to the headphone, and not the time-order bias $\beta$, which differs between devices (see "Shared noise" under "Findings"). $\beta$ stays a fixed effect of each profile's fit.
- **The fit.** The summed Laplace evidence of every member, each profile with its own curve and sessions and with $\tau_\text{seat}$ at its prior, is maximised over the four parameters with one prior on them. It always starts from the prior means, so it depends only on the trials. Each profile's fit then uses the result as fixed and estimates its own $\tau_\text{seat}$.
- **Members.** Every profile of kind `headphone` or `iem` in the data folder, with its own sessions as its fit selects them (`config --sessions`). Pooled sessions count once, toward the profile they belong to. Only a profile with at least 200 trials in the ear counts: in a smaller one the curve absorbs the answers and pulls $\sigma_0$ down, and adding several small profiles together doesn't undo that. Speaker profiles (**NOT BUILT**) won't count, for the same reason at the level of each short block.
- **Caching.** The result is cached in `cache/noise/`, keyed by every member's trials, and each profile's fit is keyed by the noise as well as its own trials. A session in any profile changes the noise, so every profile refits the next time it's used, which takes a few seconds each with the noise given.
- **During a session.** The scheduler holds the listener's noise for the whole session. It no longer re-estimates it at block ends.

If refitting every profile after each session becomes slow (with many more profiles than now), there are two ways to avoid most of it, neither built:

- **Round the noise in the cache keys,** to about 5% in log space. One session moves the noise by 1 to 5% in $\sigma_0$ and up to about 9% in the others, and a 5 to 10% change moves a curve by 0.02 to 0.05 dB RMS, so most sessions would change nothing for the other profiles. Results still depend only on the trials. An estimate near a step's edge can flip now and then. (Keeping the noise until a new estimate drifts past a tolerance flips less, but makes results depend on the order sessions were run in, which this design avoids.)
- **Cache each profile's evidence as a quadratic in the noise,** keyed by its trials, around the current shared value. The shared estimate then sums cached quadratics, and only a changed profile is refitted. Untested.

### Solving, and error bars

The solve is Fisher scoring. Each $u_g$ is held in a whitened basis of the correlation's significant eigenvectors (about 85 unknowns), and sessions connect only through $(L, \beta)$, so the matrix is block-arrow: eliminating each session in turn leaves a Schur complement on $(L, \beta)$, and the work grows linearly with the number of sessions.

Error bars come from the Laplace approximation (the inverse Fisher information), projected onto the gauge. $L$'s SD includes $\tau_\text{seat}^2$ divided by the number of sessions, so it stops shrinking in proportion to trials once each seating is well known. Every displayed curve carries a ±1 SD band.

The two ears' curves are then fitted together under a prior that they're alike (next section).

A session selected or pooled in with `--sessions`, or run by `verify`, is simply one more group, and checkpoints work as usual. Perceptual fits carry a model version. Bumping it recomputes every fit from the stored trials on next use, so results can change with no new trials, features near the threshold most of all.

## Channel balance

**In brief.** At the real profiles' trial counts, most of each ear-to-ear difference is within its error bars, and a consensus reference passed one headphone's apparent imbalance on to the others. So the two ears are fitted together under a prior that their curves are alike. Small differences are shrunk toward zero; large, well-measured ones survive, though shrunk.

Each ear is first fitted on its own, as above, with the listener's noise; that fit gives the ear's $\tau_\text{seat}$. The two curves are then fitted together, with those held fixed, under a prior on their difference $\delta = L_\mathrm{L} - L_\mathrm{R}$:

- $\delta$ is Gaussian on the common 1/48-octave grid, with SD $\sigma_\delta$ of 1.5 dB below 5 kHz and 3 dB above 8 kHz (log-interpolated between them), and a squared-exponential correlation of width $\ell_\delta = 1/8$ octave.
- Its constant is free, since each ear's level is a gauge.
- `config --balance` scales both SDs: larger is looser, and 0 fits each ear on its own. A profile in a consensus or veto uses its own setting.
- The joint solve is the same Fisher scoring, with both ears' Schur complements in one matrix: sessions belong to one ear, so the coupling is only between the two curves.
- Error bars and session curves come from the joint fit. The scheduler still fits each ear on its own while it picks trials (`model/hierarchical.py`, `fit_balanced`; `fit/run.py`, `BALANCE`).

The prior's size comes from the real profiles. A rough empirical-Bayes fit of $\delta$, pooled over FT1, HD560 and Sundara, gave 1 to 1.5 dB below 5 kHz and 2 to 4 dB above 8 kHz, and preferred a width of 1/8 octave to 1/12 or 1/4. For Sundara, the consensus reference's L−R was 1.62 dB RMS (4.9 at most, mostly HD560 above 11 kHz), and is about 0.9 with the prior.

Real differences are kept, but shrunk. They still show where the data are strong: HD560's right ear is 8 to 12 dB down above 11 kHz (about 4 SD). Sundara's own sessions and SundaraTone's, which share no trials, have L−R curves correlated at +0.57. Other headphone pairs correlate at +0.2 to +0.5, so part of the difference may be the listener's ears rather than the drivers. A Gaussian prior pulls hardest on the largest differences: an 8 dB, Q 3 peak in one ear at 600 trials per ear keeps about 55% of its L−R difference. It washes out as trials grow, but slowly.

Thicker tails may be worth trying later: a Student-t or a mixture (a narrow and a wide Gaussian) for $\delta$, in place of the Gaussian. It would pull small differences together as much as now, but leave large, well-measured ones nearly alone. The soft local pooling tried in simulation (see "Channel balance" under "Findings") is close to the mixture, and kept slightly more of an ear-specific peak. It hasn't been tried in the joint fit: a non-Gaussian $\delta$ needs an inner iteration (reweighting, for example) and makes the result depend on the starting point.

## Adaptive selection

**In brief.** The scheduler asks the question whose answer would most reduce the uncertainty in the curve, aims the level offset where the listener is closest to 50/50, and periodically refines the grid.

- **Candidate pairs**: up to 3 grid steps apart, or (with probability 0.25) a longer jump of up to one octave.
- **Scoring**: each candidate is scored by how much one answer would shrink the posterior variance of $L_i - L_j$. An answer is about this session's curve, so the score is the squared covariance of the $L$ and $L_g$ differences over the $L_g$ difference's variance plus the answer's noise; it falls off once the current seating is well known at a pair. The score is weighted up where $L$ bends (by 0.5 per SD of curvature, capped at 4 SD). Pairs in the same quarter-octave region as the previous trial are skipped. The pair is drawn at random from the top 8.
- **Level offset**: aims at the 50% point as the listener hears it, on this session's curve, time-order bias included, plus jitter of ±1 posterior SD of the difference (at most 4 dB). The offset is capped at 12 dB in magnitude.
- **Refits**: the curves are refitted after every answer with the noise and $\tau_\text{seat}$ fixed (about 10 ms at 8 sessions of 150 trials on a 199-point grid). The noise is the listener's ("Shared noise"), fixed for the session. Every 50 trials per ear, the curves are refitted fully and a midpoint is inserted on both sides of every point of $L$ whose second difference exceeds 2 SD, down to 1/48 octave. Every ear's block ends together, since ending only one would put the other's slow refit on the very next trial, a second pause in a row.
- **Seating spread**: $\tau_\text{seat}$ is re-estimated in the cached fit of all the trials, the first time one is wanted after a session (by `report`, `export` or the next `run`), never within a session.

## Sweep and tone-generator marks

Two optional ways to tell the scheduler where to look:

- **Sweep** (`run --sweep`): a slow narrowband sweep (a cluster of nine tones spread over ±1/24 octave) from 1 to 14 kHz and back, 30 s each way by default (`--sweep-seconds`, 10 to 90). It runs before the first session of that `run`, whether or not the profile has earlier sessions; later sessions started from the end screen skip it. The listener taps at each peak or dip. Up and down taps are paired and averaged, which cancels reaction-time lag, and the grid is refined around them. If the profile already has a fit, the sweep plays through it (Q toggles it), so what's left is what the correction hasn't fixed.
- **Tone generator** (`tone`): a continuous tone or noise band, glided by hand. Marks made there are saved in the profile and refine the grid of the next trial session that starts. Each mark is stamped with that session, so it's used once; a mark whose session was deleted counts as unused.

# From measurements to filters

**In brief.** The pipeline turns an ear's fitted curve into peaking filters in four stages:

- **Separate shape from features** (steps 1 to 3). Subtract a reference contour $E$, then fit a stiff baseline $b$ that follows everything broader than $W$ and ignores narrower bumps. The residual $R$ is what's left, with its full covariance.
- **Decide what's real** (steps 4 and 5). Split $R$ into lobes and keep those that are significant, ramping in features near the threshold rather than switching them on. Scale each kept feature by how consistently the seatings show it.
- **Set the target** (steps 6 and 7). Cut each kept feature back to zero, within caps on cuts and boosts, and fade the target out at the edges of the range.
- **Fit filters** (steps 8 to 10). Place and jointly refit peaking filters until they match the target within a tolerance in dB, add broad filters that cancel the narrow ones' effect on the broad shape, and set a preamp.

A "feature" throughout is a lobe of the residual: a region between sign changes (or valleys), described by its centre, height and half-height width.

## Pipeline

The steps run per ear. Code comments cite them by number.

1. **Reference.** $D = \hat L - E$, where $E$ is the perceived-level shape a reference-correct headphone would produce, with its mean removed. Options:
    - `iso226` (the default): the negated ISO 226 frontal free-field contour at 60 phon (configurable).
    - `diffuse`: ISO 226 plus a diffuse-minus-free-field threshold difference, read from a user-supplied CSV at `reference/diffuse_minus_free_db.csv` in the data folder, because the ISO 389-7 table isn't openly available.
    - `none`.
    - A CSV of perceived-level shape (`freq_hz,db` rows).
    - A consensus of other profiles' raw curves (see "Consensus reference").
2. **Baseline.** A penalised cubic B-spline in $\log_2 f$ with knots every $2W$. The curvature penalty sets its stiffness; it scales as $W^4$ (0.1 relative to the data weight at $W = 1$ octave), since a smoothing spline's length scale goes as the penalty's fourth root. It is fitted by iteratively reweighted least squares: Huber weights to convergence, then Tukey bisquare, so features don't drag it. The final smoother is linear in the data, so the residual's covariance follows exactly. `--baseline joint` (experimental) replaces it with the posterior mean of the tonality, which can't tip (see "The joint baseline"). By default the two ears share one baseline, each with its own level (see "Shared baseline").
3. **Residual.** $R = D - b$, with its full covariance $\Sigma_R$.
4. **Features.** $R$ is split into lobes between sign changes, and a lobe is split again at any valley no deeper than half the smaller peak on either side. Then:
    - A lobe narrower than 1/2 octave at half height is *narrow*. It's kept if at least two adjacent points exceed $k$ SD ($k = 2$).
    - A wider lobe is *broad*. It's kept if its mean exceeds $k_\text{broad}$ times the SD of that mean ($k_\text{broad} = 2.5$).
    - Features outside $[f_\text{lo}, f_\text{hi}]$ are dropped.

    **Soft rejection** (`--soft-margin`, $w_\text{soft} = 0.5$ by default; 0 for the plain test). A feature whose statistic is within $w_\text{soft}$ SDs of $k$ (or $k_\text{broad}$) is partly corrected, by a smooth step from 0 at the threshold minus $w_\text{soft}$ to 1 at the threshold plus $w_\text{soft}$, so it doesn't appear or vanish as its statistic crosses the threshold. The statistic for a narrow feature is the smaller $\lvert R\rvert/\text{SD}$ of its best two adjacent points, which is the quantity the plain test compares with $k$. A partial feature gets a filter once its target passes the filter fit's tolerance.

    **Inside broad regions.** A broad lobe that isn't corrected as a whole (because it's kept as tonality, the default, or because it isn't significant) can still carry narrow features, so it's searched again:
    - It's searched together with the same-sign pieces it was split from at a valley that stays clear of zero (beyond $k$ SD), so the flank beside a narrow dip isn't taken for a feature of its own.
    - The region's residual is taken against a Gaussian-weighted local linear smooth over the region alone (FWHM 1/2 octave; local linear, so it doesn't lag at the region's ends), and every lobe of that is given the narrow test.
    - Features found this way are corrected against the local smooth, with its (smaller) error bars, and checked for consistency the same way.
5. **Seating.** With two or more sessions, each kept feature's level over its half-height core is taken in every session's curve $L_g$. The feature is corrected to the median session's level (sessions weighted by precision), as a share of its level in $L$, and dropped if that share is under 20% (most seatings show little of it). A resonance only some seatings have sits lower in $L$ than in those seatings, so the share can be above 100%. The model has already shrunk the sessions together as far as the data allow, so there's no significance gate and no abrupt switch at a p-value; the share stays near 100% when they agree. `--no-seating` turns the scaling and dropping off. A Gaussian deviation can shrink an on-or-off resonance toward the average, which criterion 11 tests.

    The report gives two numbers per feature, even with one session:
    - The SD of its average level, which decides whether it's real.
    - The spread between seatings, which says how far off the correction will be on a typical seating. It's the RMS of each session's difference from the average level, with that difference's posterior variance included, since the posterior means alone are shrunk together.
6. **Target.** $C = -R$ over kept features, with cuts limited to 12 dB. Boosts are capped at +4 dB for narrow features (often fit-dependent cancellations) and +6 dB for broad ones. With `--broad-strength` above 0, the broad part is scaled by that share (up to 100%). At 0, the default, broad features aren't in the target at all.
7. **Taper.** $C$ fades to 0 with a raised cosine over up to 1/3 octave beyond $[f_\text{lo}, f_\text{hi}]$, limited by the measured margin.
8. **Filters.** On a 1/96-octave grid, in plain dB: greedily place a peaking filter on the largest error (Q from its half-height width), then refit all filters jointly with bounded least squares over $(\log F_c, \text{gain}, \log Q)$.
    - Bounds: $F_c$ within $[f_\text{lo}, f_\text{hi}]$, Q 0.7 to 16, gain at most 12 dB in magnitude.
    - Stop when no point within $[f_\text{lo}, f_\text{hi}]$ is off by more than `--fit-tolerance` (default 0.5 dB), or the band budget (default 24 per channel, at most 128) is used up. The stop leaves out the fade beyond the range, where no filter can be centred.
    - Filters under 0.5 dB are dropped and the rest refitted.
    - The target's uncertainty has already been dealt with in deciding the target, so the filters just reproduce it.
    - The budget is spent on the largest errors first. With tonality kept, up to 4 bands (at most half) are held back for step 9, so narrow corrections give way first, smallest first. If the budget binds, the frequency ranges still off by more than the tolerance are reported.
9. **Tonality compensation** (the default). Broad peaking filters (Q at most 1.5, at most 4 of them, from the bands held back in step 8) are fitted to the negative of the narrow filters' own response, smoothed over $W$. The corrected response then keeps the original broad shape to within about 0.5 dB. Filters at nearly the same frequency and Q are merged.
10. **Preamp.** $-\max(0, \text{peak}) - 0.5$ dB, where the peak is the highest point of the combined response from 20 Hz to 20 kHz. It's one value for both channels, which keeps the L/R balance.

## Shared baseline

**In brief.** The baseline is meant to capture the headphone's broad tonality, and that shouldn't differ between the ears. Fitted per ear, it could read the same region differently in each: on Sundara, the right ear's broad excess at 10 to 14 kHz went into its baseline, while the left ear's narrower 11.6 kHz resonance stood out of its own. The two corrections then looked quite different. So by default (`config --shared-baseline`) both ears are fitted with one baseline shape, and each ear has a level of its own. The reading of a region can still be wrong, but both ears now get the same one.

**Model.** The two ears' $D$ are stacked, and one spline (step 2's basis, over the union of the two grids) is fitted to all their points at once, with a free level for the right ear, since each ear's level is a gauge. The robust fit is unchanged otherwise: the same weights per point, the same Huber and Tukey passes, with the stiffness relative to all the data, so two identical ears give each ear's own baseline. The joint baseline is likewise the posterior mean over the shared coefficients and the level, with a flat prior on the level. The baseline itself doesn't change shape between the ears; there's no per-ear tilt either (see "Shared baseline" under "Findings").

**Residual covariance.** Each ear's residual now moves with the other ear's data too: $R_e = D_e - b_e(D_e, D_o)$, so to first order $\mathrm{d}R_e = (I - J_{ee})\,\mathrm{d}D_e - J_{eo}\,\mathrm{d}D_o$, with $J$ the stacked smoother (robust) or the stacked exact Jacobian (joint). The residual's covariance is $(I - J_{ee})\,\Sigma_e\,(I - J_{ee})^{\mathsf T} + J_{eo}\,\Sigma_o\,J_{eo}^{\mathsf T}$, which takes the ears' errors as independent. The fits don't keep the covariance between the ears that the balance prior gives them. Each session's curve adds the other ear's deviation in the same session, $-J_{eo}\,u_{o,g}$, and its covariance. For a session the other ear doesn't have, the other ear's average stands in, with its covariance.

**Where it applies.** Everywhere a profile's baseline is fitted with the features method: its own ears, each veto voter (with that voter's two ears), the pointwise veto's residuals, and the comparison report. The smooth and match methods have no such baseline and are unchanged. A single ear on its own (as in the one-ear simulations) has its own baseline. `--no-shared-baseline` gives each ear its own, which also takes up broad differences between the ears. That may be useful with a fixed reference, where an ear's broad offset could come from the listener's own HRTF and shouldn't be corrected.

**What it decides.** The shared shape follows the ear with the smaller error bars where they differ. On Sundara that's the right ear in the treble (SD 0.6 to 0.9 dB against 1.7 to 1.9), so both ears now take about 3 dB at 11 to 12 kHz as tonality: the left ear's cut there shrinks, and the right ear's excess is still kept. Whether that region is tonality or a feature is a separate question from whether the ears agree.

## Settings

Every setting but the channel balance is downstream of the cached perceptual fit, so changing it needs no new trials. The balance changes the joint fit of the two ears, which is quick and cached separately. Settings are stored per profile (`flatiron config`) as only the values that differ from the defaults, so improved defaults reach existing profiles.

| Setting | Default | `config` option |
| --- | --- | --- |
| Max feature width $W$ | 1 octave (1/3 to 2) | `-w`/`--width` |
| Channel balance | 1 (scale on 1.5 and 3 dB; 0: off) | `--balance` |
| Reference; listening level | `iso226`; 60 phon | `-r`/`--reference`; `--phon` |
| Consensus reference; its smoothing | none; 1/2 octave | `-r consensus:A,B`; `--consensus-width` |
| Veto against other profiles; mode; import limit | none; `feature`; 1.5 dB | `--veto A,B` (`none`: off); `--veto-mode feature` or `pointwise`; `--veto-import` |
| Soft veto $\gamma$ | 0.1 (0: all or nothing) | `--veto-softness` |
| Significance $k$, $k_\text{broad}$ | 2, 2.5 SD | `-k`/`--k-narrow`, `--k-broad` |
| Soft rejection margin $w_\text{soft}$ | 0.5 SD (0: all or nothing) | `--soft-margin` |
| Broad correction strength | 0% (off: tonality kept) | `--broad-strength` |
| Band budget | 24 per channel (at most 128) | `-b`/`--bands` |
| Filter fit tolerance | 0.5 dB | `--fit-tolerance` |
| Boost caps; cut cap | +4 narrow, +6 broad; −12 dB | `--max-boost-narrow`, `--max-boost-broad`; `--max-cut` |
| Correction range $[f_\text{lo}, f_\text{hi}]$ | 1 to 13 kHz | `--f-lo`, `--f-hi` (Hz, or kHz with a `k`: `12.5k`) |
| Seating step | on | `--seating` / `--no-seating` |
| Sessions and pooled profiles | all of this profile's | `--sessions ID,PROFILE,…` |
| Method | `features` | `--method features`, `smooth` or `match`; `--smooth-width` |
| Baseline | `robust` | `--baseline robust` or `joint` |
| One baseline for both ears | on | `--shared-baseline` / `--no-shared-baseline` |

`config` with no options lists every setting, starred where changed. `--reset NAME,NAME` returns the named settings to their defaults; it also takes `sessions`, `checkpoint`, and `all` (every setting and the session selection, but not the checkpoint, which has to be named). `--reset` can't be combined with any other change, which catches a reset and a change meant for different profiles or settings; `--veto none` can.

Some combinations are refused:

- `--veto` and `--baseline joint` need `--method features`.
- `--method smooth` needs `--broad-strength` 0.
- `--smooth-width`, `--veto-mode`, `--veto-import` and `--consensus-width` need the setting they modify.

`--shared-baseline` is on by default, so it isn't refused with the smooth or match method; it just has no effect there.

Limits: $W$ 1/3 to 2 octaves, `--balance` 0 to 10, `--soft-margin` 0 to 2 SD, `--veto-softness` 0 to 1, `--consensus-width` 0 to 2 octaves, `--smooth-width` 1/8 to 1 octave, `--fit-tolerance` 0.1 to 6 dB, `--broad-strength` 0 to 100%, 1 to 128 bands, and $f_\text{lo}$ below $f_\text{hi}$.

The correction range can only move within the measured range, and the measured range, grid and stimulus are fixed when the trials are collected. Features wider than about an octave are only as well constrained as the longest jumps (1 octave).

## Checkpoints

`config --checkpoint` fixes the current filters per ear. Later fits run the pipeline on the corrected curve ($\hat L$ plus the fixed filters' response) and only add filters for what's still significant, within the bands left over. This holds whatever the settings become. `--reset-checkpoint` drops it. The usual use is to checkpoint before a verify session, so its results add to the filters that were verified instead of reshuffling them.

## Pooling

`config --sessions THIS,OTHER` includes another profile's trials in this profile's fits: the same headphones measured with a different stimulus, for example. `--sessions` takes a comma list of session ids and profile names; a profile name stands for all of that profile's sessions, new ones included, so the list can also mix a few of this profile's sessions with all of another's. The model already accounts for each trial's stimulus, so the sessions pool directly. They stay in their own profile, and new sessions there are included as they're run. Each pooled session is one more group in the fit, and the newest session's grid is the one the fit uses. Trial sessions still adapt to the profile's own trials only. `--reset sessions` goes back to this profile's own sessions, all of them.

## Measurement context

Flatiron treats EQ as three layers: any system EQ active while measuring, the correction Flatiron fits, and an optional preference layer the user owns.

- **Active EQ is known, not measured.** A session can record the system EQ in effect (`run --active-eq`, as APO text, with an optional `--eq-note`). Its gain at each stimulus frequency enters the model as a known offset, so $\hat L$ always describes the raw headphone.
- **Export modes.** *Correction only* (the default): filters to stack after the existing EQ or in their own instance. *Merged* (`export --merge FILE`, or `--merge L=FILE --merge R=FILE` for a preset per channel): the preference layer's APO text first, then the correction sorted by frequency, with the preamp computed over both.

# Several headphones on one listener

**In brief.** A feature every headphone shows on this listener is probably the listener's own (pinna, concha, canal, hearing) and shouldn't be corrected. A feature only one headphone shows probably belongs to that headphone. Flatiron uses that in three separate tools, all built on one robust average of several profiles' curves, the *soft median*:

- **Comparison** (`flatiron compare`) shows the profiles' curves side by side and flags where one stands out. It fits no filters.
- **Consensus reference** (`config -r consensus:…`) uses the soft median of other profiles' curves in place of ISO 226 as the reference $E$, so whatever is shared is subtracted before detection.
- **Veto** (`config --veto …`) keeps detection as it is, but corrects each feature only by as much as it exceeds the most typical matching feature across the headphones.

The reference and the veto are separate settings, usable alone or together, with or without broad correction. Both exist to avoid correcting what's shared across headphones, so they aren't independent in effect: an unsmoothed consensus reference leaves a veto little to do. Smoothed (the default), the reference carries the broad shape and the veto decides the narrow features.

Profiles of kind `iem` don't count toward a consensus or a veto by default. They lack the pinna and concha, so a peak they lack says nothing about whether it's the listener's.

## Grids

Each fit stays on its own grid of about 100 to 110 points, and detection, flags and the soft median's misfit are computed there. Interpolating a fit onto a finer grid adds no measurements: the new points are mixes of their neighbours, so a 399-point curve still holds only about 106 independent values. Treating those points as separate data would make the two-point rule meaningless and the error bars too small (see "Grid" under "Findings").

Shared curves such as the consensus live on a common 1/48-octave grid, the finest any fit uses, over the intersection of the measured ranges, and meet a profile on that profile's own grid through $A_p$. The comparison figure is drawn on the common grid. The between-headphone spread $\tau_\text{hp}$ and Cochran's Q are also computed there, point by point from the interpolated curves and variances, so correlated neighbours in the strip below aren't separate evidence. The 1/96-octave grid is used only for the filter fit (with the target and response curves it produces) and to compare feature shapes in the veto.

## The soft median

**In brief.** The consensus is a robust, smooth average of the profiles' curves: close to a precision-weighted mean where they agree within their error bars, and close to a median where one is far off. Each profile also gets a constant offset, fitted robustly so that one headphone's treble difference doesn't shift its whole curve.

The consensus $m(f)$ minimises a robust misfit across profiles plus a smoothness penalty across frequency:

$$
m = \operatorname*{arg\,min}_m \; \sum_p \sum_i w_{p,i}\, h\!\left(\frac{\hat L_{p,i} - \alpha_p - (A_p m)_i}{\sigma_{p,i}}\right) + \kappa \left\lVert \nabla^2 m \right\rVert^2
$$

- $\alpha_p$ is profile $p$'s constant offset, fitted with $m$ by the same loss, the offsets summing to zero.
- $i$ runs over profile $p$'s own grid points. $w_{p,i}$ is the octave width each point covers relative to 1/24 octave, so a point at the usual spacing counts as one measurement and a refined region doesn't count for more just because it has more points. The curvature penalty is scaled to that same unit; against raw octave widths it would smooth about 24 times too hard.
- $h$ is the pseudo-Huber loss with unit scale, $h(z) = \sqrt{1 + z^2} - 1$: quadratic for small misfits, linear for large ones. That is the "soft" part.
- $\sigma_{p,i}^2 = \mathrm{SD}_{p,i}^2 + \tau_\text{hp}^2$, where $\mathrm{SD}_{p,i}$ is $\hat L_p$'s SD at point $i$. Measurement noise alone would overweight the most-measured profile.
- $\tau_\text{hp}$ is one value per ear and profile set: the median across frequency of the per-frequency DerSimonian–Laird estimates, floored at 0.5 dB. A separate estimate at each frequency would let a headphone's own peak inflate the tolerance at that peak, and the soft median would then act like a mean exactly where it most needs to be robust.
- $\nabla^2$ is the second difference across the common grid, and $\kappa$ uses the same curvature penalty as the perceptual fit's smoothness prior, so $m$ is no smoother than the data it comes from.
- It's solved by iteratively reweighted least squares.
- $m$'s covariance carries each profile's full covariance through the final weights. With $\Omega_p = \operatorname{diag}(w_{p,i}/\sigma_{p,i}^2)$ and $H$ the final system matrix,

    $$
    \operatorname{Cov}(m) = H^{-1}\left(\sum_p A_p^{\mathsf T}\, \Omega_p\, \Sigma_p\, \Omega_p\, A_p\right) H^{-1}.
    $$

    Per-frequency weights alone ignore the correlation between neighbouring points and make the SD too small (see "Grid" under "Findings"). This treats the final weights as fixed, which criterion 10 tests.

With only two profiles, the consensus is their precision-weighted mean, and the report says so. The flags then show differences, not outliers.

## Comparing profiles

`compare A B C …` overlays two to eight profiles' raw curves per ear with their consensus, and says what the veto would correct on each. It fits no filters.

**Inputs.** Each profile's cached perceptual fit, $\hat L_p$ with its covariance, from its own sessions and pooling. Settings are shared for the comparison: max feature width $W$ (`-w`, default 1 octave), reference (`-r`) and ear (`--ear L`, `R` or `both`). Without `-r`, the reference the profiles have in common is used; if they differ, Flatiron asks for `-r`. A consensus reference isn't used here: `iso226` stands in, with a note. The listening level and $k$ come from the profiles when they all agree, and every other setting is at its default, including the veto's import limit. Profiles that share trials through pooling get a warning, since they'll agree more than separate measurements would.

**What's reported, per ear:**

1. **Aligned curves.** Each raw $\hat L_p$, with no reference subtracted whatever `-r` is (as with `-r none`), shifted by its robust offset $\alpha_p$, with ±1 SD bands, and the raw consensus on top with its own. The reference only matters for detection, so the flags and feature tables use it and the figure doesn't. A mean-based alignment would be pulled by a treble difference in one headphone. Broad tonal differences stay visible here, and only here.
2. **Where the curves differ.** A strip shades frequencies where the aligned curves disagree as a whole (Cochran's Q, $p < 0.01$). It includes broad differences.
3. **Flags.** A feature is flagged when the veto, in its feature mode, would correct it, so a flag always names something a veto would act on, and a broad difference can't produce one. A flag lists the profile, ear, centre, height, correction, and a reading: "peak only here", "peak elsewhere on the others", or "taller here than on the others", with the words for dips to match. With two profiles the reading is "differs from the other". Flags are sorted by the size of the correction.
4. **All features.** One row per place, for features that all overlap each other, with each profile's height there and what the veto would correct. A feature missing from one headphone isn't flagged on it; it shows up here as a gap, and in the others' rows.

Profiles of kind `iem` are drawn dashed and don't count toward the consensus or the veto unless `--include-kind iem`. They're measured against the voters' median without counting toward it, and their offset is a weighted median against the consensus. A comparison needs at least two voters. (Kind `speaker` would be treated the same way, and is **NOT BUILT**.)

This replaced an earlier report of residuals and pointwise z-scores. Each profile's baseline makes its own decision about what to reject, so the residuals disagreed more than the raw curves: on HD560, FT1 and Sundara, the RMS spread was 2.97 dB in the residuals against 2.66 dB raw in the left ear, and in the left-ear mids a 4 to 5 dB raw gap became 6 to 10 dB.

## Consensus reference

`config -r consensus:A,B,C` uses a consensus of raw curves as $E$, in place of a fixed reference (pipeline step 1). Every later step is the same, including the baseline.

1. **Raw curves.** The sources are the listed profiles' $\hat L_p$, from their own sessions and pooling, with no reference and no baseline. So a consensus depends only on its members' fits, and every profile using the same list shares one cache entry.
2. **Members.** Exactly the profiles listed, which must be of kind `headphone`. The profile being corrected is included only if it's listed. Leaving it out makes the reference independent of it, but with two others the soft median falls back to their weighted mean, so one's peak reaches the reference at about half height. Listing it as well gives a real median of three. A single profile is allowed (the reference is then that headphone's curve), with a note. Profiles that share trials through pooling can't be used together: pooling means the same headphones measured again, and a consensus needs different ones.
3. **Alignment.** Each source gets the offset $\alpha_p$, fitted together with the soft median by the same robust loss, so a treble difference in one headphone doesn't shift its whole curve. A mean-based alignment would be pulled by exactly those differences.
4. **Smoothing.** `--consensus-width` (FWHM, default 1/2 octave; 0: none) smooths $m$ with a local linear fit. Unsmoothed, the reference holds the members' narrow features, so a shared peak disappears before detection, and another headphone's peak reads as a dip here. Smoothed, the reference carries the broad shape, and narrow features go to detection, and to the veto if it's on. About half of a shared peak around 1/4 octave wide stays in the reference at this width, so a feature's height against it is lower than against a fixed reference.
5. **Covariance.** $\operatorname{Cov}(\hat L - m)$ adds $m$'s error and, for a member, subtracts the correlation its own vote creates. The offsets are treated as fixed.

Raw results are shared at the top of the data folder (`cache/consensus/`), keyed by the ear, the members and each member's perceptual-fit hash, and pruned least-recently-used at 200 MB. A new session in any member changes its fit, and so the key. The smoothing is applied after the cache.

## Veto against other profiles

**In brief.** For each feature this profile would correct, the veto asks what the most typical headphone, this one included, shows at the same place: the *median feature*. Only the excess over that is corrected. A peak every headphone shares is left alone; a peak only this headphone has is corrected in full; a peak that's merely taller here is corrected by the difference. The choice of median feature is made over whole feature shapes, so it's always a feature some headphone actually has, and it's softened so that near-ties don't flip the correction.

`config --veto A,B,C` turns it on. Detection is unchanged, on this profile and each voter, against this profile's reference and settings. It needs `--method features`. It applies to whatever would be corrected: narrow features, those inside broad regions, and with broad correction on, broad ones too. `--veto none` turns it off, and `report` lists what each voter contributed.

### The rule

1. **Voters.** The profiles listed plus this one, always. They must be of kind `headphone`, at least one must be another profile, and profiles that share trials through pooling can't be used together.
2. **Matching.** Each voter contributes its tallest same-sign feature that overlaps this one, in the literal sense that their half-height widths intersect. A voter with no overlapping feature counts as having nothing there. A voter's narrow lobes of 1 dB or more count even if they aren't significant, since an undetected lobe isn't the same as nothing. There's no matching parameter. Peaks that are clear of each other, however close, are treated as separate features.
3. **The median feature.** $F^\star$ is the most central of the candidates: the actual feature (or nothing) with the smallest total shape difference $\mathcal D_c$ to the rest, each drawn as a Gaussian of its height and half-height width. Ties go to this profile. With one other profile there's no middle, so $F^\star$ is their average.
4. **Correction.** With $R$ the feature's residual, $C = -(R - F^\star)$, with any net boost in the feature's own direction limited. The limit is $I$ (`--veto-import`, default 1.5 dB) if $F^\star$ is more than 1/12 octave away, and 0 otherwise, so at the same place a feature can only shrink. Outside the feature only an import can add anything. $I = 0$ means nothing is ever imported. The result is then held to the usual cut and boost caps, and a feature whose largest correction comes to under 0.5 dB is left alone as shared.

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

This profile's own vote doesn't protect against imports when the other two agree: at 8.2, 7 and 7 kHz the median ignores it. That's why the import limit is explicit.

Why not the alternatives:

- **One share per feature**, from a template fitted to each curve, was built first and failed. A lobe holding both a shared peak and this headphone's own needs two different shares. The difference of shapes handles it: on noiseless curves, such a lobe got −4.5 dB on its own part and −5.5 dB on the shared one with a share, and −5.3 and −1.4 dB this way.
- **Independent medians** of frequency, width and height can give a feature no headphone has. A peak between two others on either side comes back as itself and reads as shared, which happened on real data at 8.2 kHz.
- **A limit on the bump instead of the net boost** cuts energy this headphone already has at the median feature's place.

### Soft veto

`--veto-softness` ($\gamma$, default 0.1; 0 for the rule above). The rule's steps are all or nothing, so a small change in a curve can move a correction by decibels: a near-tie flips the median feature, and a voter's feature counts or doesn't. With $\gamma > 0$, each step is gradual:

- **Weights.** A candidate $c$ weighs $\exp\!\big(-(\mathcal D_c/\mathcal D_\text{min} - 1)/\gamma\big)$, normalised, where $\mathcal D_\text{min}$ is the most central candidate's shape difference. The correction is the weighted mean of what each candidate would lead to as the median feature. A clear winner gives the rule above, and this profile taking all the weight still gives no correction. $R$ isn't exactly this feature's own Gaussian, and that difference counts only as far as a candidate differs from this feature, so a voter with the same shape leads to no correction either. Near a tie this mixes two shapes, not their parameters, so it doesn't bring back the independent medians' problem: a clear winner comes through as itself.
- **Matching.** A voter's same-sign features count by how far they overlap this one: 1/2 at the literal limit, falling from 1 to 0 across half that limit either side. Among several, a taller one dominates in proportion to $\text{height}^4$. A voter's feature that isn't significant counts by height, from 0 at 0.5 dB to 1 at 1.5 dB, and one that is counts by at least its significance share.
- **Same place.** The import limit, and an import's reach outside the feature, come on from 1/24 to 1/8 octave.
- **Location.** A candidate's centre is weighted over the part of the lobe above half height, and its width is its area over its height, scaled to match the FWHM for a Gaussian. The peak frequency can jump between two near-equal extremes of a lobe, and these can't.
- **The 0.5 dB rule** doesn't apply; the filter fit's own smallest filter is what's left.

Lobe splitting, narrow/broad classification and the broad-region search keep their hard edges.

### Pointwise mode

`--veto-mode pointwise` is the alternative. Features are found on the excess over the soft median $m$ of the voters' residuals (each with its own baseline, this profile's included), and at each frequency what exceeds that median is corrected, never past zero:

$$
C = -\operatorname{sign}(R)\,\max\!\big(0,\; \lvert R\rvert - \max(0,\, \operatorname{sign}(R)\, m)\big)
$$

A checkpoint's filters are part of $R$ but not of the median, so a peak they already took out leaves nothing to correct. This mode can't import or move anything, and it's sensitive to broad differences between headphones because of the separate baselines. `--veto-import` doesn't apply to it.

# Experimental methods

These are settings, off by default, that change how the pipeline treats the residual or the baseline. They stay experimental until they've earned a switch.

## The smooth and match methods

`config --method smooth` skips feature detection. The baseline is the curve smoothed with a Gaussian-weighted local linear fit over `--smooth-width` (FWHM, default 1/2 octave), and the whole residual within $[f_\text{lo}, f_\text{hi}]$ is the target, with the usual caps, tonality compensation and filter fit.

- It corrects more, but with no significance test it also corrects noise: on noise drawn from real fits' error bars it fits about 3 filters per ear (1.5 to 3 dB), against about 0.8 for the features method.
- A smoothing window also takes part of any feature near its own width into the baseline (about 45% of a 1/4-octave resonance at 1/2 octave), which under-corrects it.
- The features are still reported, as what's significant, and `verify` uses them.
- It doesn't combine with broad correction or `--veto`; a reference, consensus included, still applies.

`config --method match` has no baseline at all. The whole of $D = \hat L - E$ within the correction range, with only its level free (a precision-weighted median), is the target, tonality included, with the cut cap and the broad boost cap and no tonality compensation. There's no feature test. It's meant for trying a consensus reference; with ISO 226 it would correct the difference between the headphone and the contour, broad shape and all. It doesn't combine with `--veto`.

## The joint baseline

**In brief.** The robust baseline makes a hard choice for a broad region far off the reference: follow it as tonality, or reject it as a feature. Near the edge between the two, small changes in the data or the error bars can tip it from one to the other (see "Known limitations"). `config --baseline joint` replaces the choice with an average: the posterior mean of the tonality under a model in which each point is tonality plus either noise or a feature. The baseline then moves continuously with the data and the error bars. Everything downstream is unchanged, except that the baseline is nonlinear in $D$. `robust` stays the default until this has earned the switch.

**Model.** On the fit's grid (both ears' points stacked, with the baseline shared; see "Shared baseline"), $D_i = b(x_i) + e_i$, with $b = Bc$ the robust baseline's spline (knots every $2W$). Each point is tonality plus the fit's error, or that plus a feature:

$$
e_i \sim (1 - \pi)\,\mathcal N\!\left(0,\; \mathrm{SD}_i^2 + \sigma_\text{floor}^2\right) + \pi\,\mathcal N\!\left(0,\; \mathrm{SD}_i^2 + \sigma_\text{floor}^2 + \sigma_\text{spread}^2\right)
$$

with $\pi = 0.4$, $\sigma_\text{spread} = 8$ dB and $\sigma_\text{floor} = 0.5$ dB.

- The curvature prior on $c$ is the robust baseline's stiffness (0.1 relative to the data weight, scaling as $W^4$), but taken at a fixed SD of 1.1 dB (the real profiles' median) rather than at the fit's own, so it's fixed in dB. Level and slope are free.
- As in the robust fit, each point is weighted by the width it stands for, in initial grid steps, and only the SDs (not the correlations) of $\Sigma_D$ are used.
- The floor is the tonality's own ripple. Without it, whether a block is shape or feature turns on its misfit in SDs, which moves fast with the error bars' scale.

**Inference.** The tonality has only a handful of coefficients (5 at $W = 1$, 9 at $W = 1/3$), so the posterior mean is integrated directly, by importance sampling over fixed draws (4,000 per proposal, seed fixed):

- Five rounds of proposals (multivariate t, 4 degrees of freedom). The first is centred on the Huber fit and as wide as the posterior with the likelihood raised to the power 0.01; each later one is matched to the moments of the posterior at a higher power (0.03 up to 1), 1.5 times wider.
- Where a block sits far off the reference, the posterior has several narrow modes far apart (one per reading) that no single proposal covers. So the modes are also found by Newton's method from 16 starts (the Huber fit, the robust fit, and the best draws, chosen farthest-first), and each mode within 20 nats of the best (by Laplace evidence) gets a proposal of its own, its Laplace covariance 1.3 times wider.
- Every draw is weighted against the mixture of all the proposals (multiple importance sampling).
- Nothing is resampled, so the estimate is a smooth function of the data, except where a mode is found or not: that changes only the proposal, and moves the estimate only by its Monte Carlo error.
- It takes about 0.3 s per ear, so it isn't cached.

**Residual covariance.** The baseline's Jacobian is exact for a posterior mean: $J = \operatorname{Cov}\!\big(b,\ \partial \log p(D \mid c)/\partial D \;\big|\; D\big)$, from the same weighted draws. The residual is $R = D - \hat b$, with covariance $(I - J)\,\Sigma_D\,(I - J)^{\mathsf T}$, and each session's curve is $R + (I - J)\,u_g$. Unlike the robust fit's, which holds its final weights fixed, this includes how the reading itself moves with the data: where the baseline is sensitive (a block near the edge between readings), the residual's SD grows, and features there need to be larger to count.

**Report.** The baseline is drawn with its posterior SD. A corrected feature where that SD exceeds 1 dB is flagged in both reports: part of it may be tonality, or the other way round.

The validation results, and the designs tried first, are under "Joint baseline" in "Findings".

# Listening tools

## Audition and verify

- **Preview EQ.** Flatiron's own software copy of a correction, built from the same export plan as the exporters, so what's auditioned or verified is what an export does.
- **Audition** (`audition`): pink noise, the slow sweep, or the user's WAV files, with Space flipping the correction on and off at the same playback position. The preamp applies to both states, so they're level-matched. A blind mode hides which is which until revealed.
- **Verify** (`verify`): a short session (150 answers by default) with the preview EQ applied to every burst. Each burst's level change from the EQ is recorded as a known offset, so verify trials pool with ordinary ones and stay valid if the settings change later. Afterwards Flatiron refits with the applied filters fixed and reports any feature still significant.

## Tone generator

`tone` is a real-time signal generator for hunting by hand. It renders block by block in the audio callback:

- **Stimuli**: pure tone, warble, and 1/24, 1/12 and 1/3-octave noise. Noise is white noise through a per-block 2nd-order Butterworth band-pass whose state carries over, normalised by its equivalent noise bandwidth.
- **Smoothing**: frequency (50 Hz to 18 kHz) glides with a 20 ms one-pole smoother in log-frequency. Level, ear, correction on/off and pauses ramp over 10 ms; a stimulus change fades out, swaps and fades in.
- **Motion**: built around tapping, since terminals delay a held key's auto-repeat and report no releases. Each ←/→ press adds a bump to the glide speed: a Hann window over ±0.5 s whose area is one tap size (↑/↓, 1/96 to 1/6 octave, 1/24 by default), so tapping steadily at $r$ per second glides at $r$ tap sizes per second. A press enters the bump at its peak when the tone is still, so it moves at once (half a tap size in all), and at its start once the speed reaches a lone tap's peak (tap size / 0.5 s), so the speed then changes smoothly; between the two the entry point is linear in the speed. Tapping slower than about 2/s gives separate steps, and from about 3/s the speed ripples by under 20%. A press against the motion stops it and adds nothing; the next press moves. Auto-repeat from a held key counts as presses at no more than 10/s, about the fastest steady tapping.
- **Profile**: given a profile, its correction (without its preamp) is applied per channel and can be toggled, and P/D marks are saved to the profile (see "Sweep and tone-generator marks").

# Export formats

All exporters work from one export plan: per-channel filter lists and a preamp. LSP and EasyEffects output is checked against LSP's own LADSPA build before writing, when it's installed. PipeWire output is checked through a private filter-chain whenever PipeWire is running. The tolerance is 0.3 dB from 1 to 14 kHz, and an export that misses it isn't written.

| Target | File | Per-channel mechanism | Limit | Checked against |
| --- | --- | --- | --- | --- |
| Equalizer APO / AutoEQ text | `<name>_L.txt`, `<name>_R.txt` | One file per channel | — | — |
| LSP Parametric Equalizer x32 LeftRight | LSP settings `.cfg` | Separate left/right bands | 32 per channel | LSP LADSPA, offline |
| EasyEffects (and Equaliser, macOS) | Preset `.json` | `split-channels`, `left`/`right` | 32 per channel | LSP LADSPA, offline |
| PipeWire filter-chain, builtin `param_eq` | Drop-in `.conf` | `filters1` (L), `filters2` (R) | — | A private filter-chain, when PipeWire is running |
| TinCan | Preset `.toml` | `channel` per filter | 256 filters | — |

By default files are written to the current directory, named `<profile>-<fit hash>` (with `-merged` when merged); `-o FILE` picks another name, and APO text then becomes `FILE_L.txt` and `FILE_R.txt`. TinCan presets go straight into TinCan's preset folder. A folder that doesn't exist is an error, never created. `--preamp safe` or `--preamp DB` overrides the preamp (`0` keeps the uncorrected level for A/B comparison; a warning is printed if boosts can then clip).

- **APO text.** A `Preamp:` line, then `Filter n: ON PK Fc … Hz Gain … dB Q …`. PipeWire's `param_eq` reads it through `filename1`/`filename2`, and REW-style importers accept it.
- **LSP.** Written over a template exported from LSP 1.2.35, changing only band, gain and mode lines. Bands use filter mode 6, "APO (DR)", which matches the RBJ filters within 0.01 dB (the default RLC (BT) mode differs by up to 1.7 dB on narrow bells). The preamp is the input gain; LSP doesn't auto-balance.
- **EasyEffects.** Written over an EasyEffects 7 split-channel preset, with bands in "APO (DR)" mode and the preamp as input gain. EasyEffects runs LSP's equalizer, so the same LADSPA check applies. Equaliser (macOS) imports this format; how it maps Q and mode on import hasn't been verified.
- **PipeWire.** A filter-chain module with `param_eq` filters inline, `bq_peaking` (and shelves) in RBJ form. The preamp is a `bq_highshelf` at frequency 0, which is a broadband gain. It's installed by copying it to `~/.config/pipewire/pipewire.conf.d/` and restarting PipeWire (`systemctl --user restart pipewire`), which adds a "Flatiron <profile>" sink. The check plays a log sweep through a private copy (virtual sink and source, nothing on real devices) and compares the recording with the prediction. It pins `pw-play` and `pw-record` with `node.dont-move`, because a running TinCan moves new streams onto its own sink.
- **TinCan.** TinCan's preset format 1: `format`, `name`, and `[[filter]]` tables with `type`, `channel` (L, R or LR), `frequency`, `gain`, `q` and `enabled`. TinCan's bells are RBJ peaking filters. Presets carry no preamp, since TinCan sets one for everything loaded, so `--preamp` is refused. A filter identical in both channels (a merged preference shelf, for example) is written once, as LR. The preset is named "<profile> Flatiron <fit hash>", saved under TinCan's file-name rule into `$XDG_DATA_HOME/tincan/presets/`, or written to `-o FILE` for TinCan's import. Values outside TinCan's ranges are refused rather than clamped.

# Architecture

A Python 3.11+ package with a keyboard-driven terminal UI, managed with `uv`. It's intended to run unchanged on Linux and macOS. Its matrices are small (a few hundred rows), so it limits BLAS to one thread before numpy loads: threading made fits about 8× slower on a 16-core machine. `uv sync` installs it; commands run as `uv run flatiron …`, and tests as `uv run pytest`.

| Concern | Choice |
| --- | --- |
| Playback | `sounddevice` (PortAudio) |
| DSP and fitting | `numpy`, `scipy` |
| UI | `textual` |
| Report | `matplotlib` figures embedded in a static HTML page |
| Tests | `pytest` with `pytest-xdist` (parallel by default; `-n0` for one process) |

## Modules

- `audio/`: biquads, stimuli, limiter, device listing, the persistent output stream and buffer mixer, the tone generator's synth, and the preview EQ.
- `model/`: grids (with the common grid and the interpolation $A_p$), the trials and fit types, the hierarchical fit (one ear, and both under the balance prior), and grid refinement.
- `session/`: the adaptive scheduler, the trial runner (scheduler, stimuli and session file, without UI), storage, and the sweep.
- `fit/`: reference, baseline (robust, and joint in `decompose.py`), features, filter fitting, the correction pipeline, the consensus, the comparison's flags and rows, the veto, and the per-profile fit (cache, settings, pooling, consensus, checkpoint).
- `export/`: the export plan, the five writers, the LADSPA host and the truth checks.
- `report/`: figures, the HTML report, and the comparison report.
- `ui/`: the trial app (level, channel check, sweep, trials, end screen), audition, and the tone generator.
- `sim/`: the simulated listener, non-adaptive designs, validation, and the seating and multi-headphone scenarios.
- `cli.py`: the commands below.
- `apo.py`: parsing Equalizer APO / AutoEQ text (`Preamp:` and `Filter N: ON` lines of type PK, LSC/LS or HSC/HS; OFF filters are skipped, anything else is an error). Writing it lives with the exporters.
- `iso226.py`: the ISO 226:2003 equal-loudness contours.
- `paths.py`: the data folder.

## Commands

Commands that work on one profile take it as an optional first argument, `PROFILE` below, which defaults to the only profile if there's just one (`tone` uses a profile only if one is named). `run` adds sessions to an existing profile; `run NAME --new` starts one. `-d`/`--device` takes an index from `devices` or part of a device's name, and `-n`/`--dry-run` runs with no audio. Frequencies can be given in Hz or with a `k` for kHz (`13k`).

| Command | Does | Options |
| --- | --- | --- |
| `devices` | Lists outputs with host API, channels and native rate, marks the default, and flags Bluetooth, mono and rates other than 48 kHz | — |
| `run` | Runs trial sessions | `PROFILE`, `--new`, `-d`, `-n`, `-m`/`--minutes` (default 12, at most 15), `--stimulus noise:N`, `warble:N` or `tone` (default `noise:12`: a 1/N-octave band; a warble swings over 1/N octave, default 12), `--active-eq APO_TXT`, `--eq-note`, `-s`/`--sweep`, `--sweep-seconds` |
| `verify` | A short session through the correction | As `run`, without `--new` and the sweep, plus `--trials` (answers, both ears; default 150) |
| `audition` | The correction on and off | `PROFILE`, `-d`, `-n`, `--source pink`, `sweep` or `FILE.wav` (repeatable; default pink and sweep) |
| `tone` | The tone generator | `PROFILE`, `-d`, `-n`, `-f`/`--freq` (default 1000), `-e`/`--ear L`, `R` or `both` |
| `profiles` | Lists profiles (sessions, trials per ear, last date and level, sessions not closed), or changes them | `new NAME --kind headphone` or `iem`; `set NAME --kind … --label …`, which affects no trials |
| `sessions` | Lists sessions: trials per ear, minutes, catch-trial accuracy, fast answers, share choosing the first interval, EQ context, verify and not-closed marks | `PROFILE`, `--delete ID` (a session left open by a crash, say), `-y`/`--yes` |
| `config` | Shows or changes settings (see "Settings") | `PROFILE`, the options in "Settings", `--reset NAME,NAME` or `all`, `--checkpoint` |
| `report` | Prints filters and features, and notes on the seating spread, noise and bias | `PROFILE`, `-H`/`--html`, `--csv FILE` |
| `compare` | Compares profiles (see "Comparing profiles") | Two to eight profiles, `-H`/`--html`, `-w`, `-r`, `--ear`, `--include-kind iem` |
| `export` | Writes the correction | `PROFILE`, `-t`/`--target` (required: `apo`, `lsp`, `easyeffects`, `pipewire` or `tincan`), `-o`/`--output`, `--merge FILE` or `--merge L=FILE --merge R=FILE`, `--preamp safe` or `DB` |
| `simulate` | Runs a simulated listener (see "Validation") | `--trials-per-ear` (default 600), `--seed`, `--design adaptive` or `uniform`, `--scenario consensus` or `seating`; hidden from help: `--sigma0`, `--lapse` |

`report --csv` writes one row per ear and grid point: `ear,freq_hz,level_db,level_sd,reference_db,baseline_db,residual_db,residual_sd`.

## Keys

| Screen | Keys |
| --- | --- |
| Level | ↑/↓ ±1 dB, PgUp/PgDn ±5 dB, Enter continue, Esc cancel |
| Channel check | ←/L left, →/R right, Space replay, Enter retry, Esc quit |
| Sweep | Enter start, ↑/P peak, ↓/D dip, Q correction on/off, Esc skip |
| Trials | ←/1 A louder, →/2 B louder, Space replay, P pause, U/Backspace undo (the pair plays again), L level (with a warning), Q save and quit, X discard (asks first) |
| End of session | N next session, Q quit |
| Audition | Space switch, N next source, B blind, R reveal, Q/Esc quit |
| Tone generator | ←/→ frequency, ↑/↓ tap size, G go to a frequency, E ear (left, right, both), S stimulus, +/− level, Q correction on/off (given a profile), P/D mark a peak/dip, U undo the last mark, Space pause, Esc quit |

## Data on disk

Everything lives under `~/.local/share/flatiron/` (Linux, or `$XDG_DATA_HOME`) or `~/Library/Application Support/flatiron/` (macOS), as JSON written atomically.

- `profiles/<slug>/profile.json`: label, kind (`headphone` by default, or `iem`), settings (only changes from the defaults), the session selection (if not all of its own: session ids and profile slugs), profiles pooled in, the checkpoint (filters per ear, when it was made and on how many trials), and tone-generator marks. A consensus reference is stored in the settings as the string `consensus:slug,slug`, and a veto as a list of slugs. The name `none` is reserved, since `--veto none` uses it.
- `profiles/<slug>/sessions/<timestamp>.json`: device, sample rate, EQ context (note and APO text), stimulus, level and level changes, gap, seed, scheduler settings, final grid per ear, channel checks, sweep marks and the correction it played through, tone marks used, the preview EQ for verify sessions, and every trial. A trial records the ear, both frequencies, the offset for each, play order, rove, answer, reaction times, replays, the fast flag, known EQ gains, level, and the undone and retake flags. The file is saved after every answer.
- `profiles/<slug>/cache/`: perceptual fits (`probit-<hash>.npz`, keyed by trials, sessions, grid, priors, the listener's noise and model version; they hold $\tau_\text{seat}$ and each session's deviation), which consensus entries the profile last used, and the last HTML report.
- `reference/diffuse_minus_free_db.csv`: the user-supplied table for the `diffuse` reference.
- `cache/noise/`: the listener's noise per ear, keyed by every member profile's trials, newest 200 kept.
- `cache/consensus/`: consensus results, shared by the profiles in each set. `cache/comparison.html`: the last comparison report.

## Session flow

1. **Setup.** `devices` flags Bluetooth, mono and sample-rate problems.
2. **Each session** (`run`, at most 15 minutes, 12 by default):
    - The level screen.
    - The channel check: a burst in each ear in random order, and the listener says which side it was in.
    - The sweep (the first session of this `run`, if asked).
    - Trials until time is up or the listener quits. The fitted curve is never shown during trials.
    - The end screen asks for a reseat before the next session.
3. **Look** (`report`, `report -H`, and `compare` across headphones): per ear, $\hat L$ with its SD band, the reference, the baseline, the residual with its significance, the features (kept or not, and why), the filters, and the predicted corrected curve. With two or more sessions, each session's residual is drawn against the average; above five, the band holding the middle 90% of them, weighted by trials. Checkpoint filters are labelled.
4. **Adjust** (`config`), **listen** (`audition`), **verify** (`verify`, ideally after `config --checkpoint`), and **export**.

# Validation

**In brief.** There's no ground truth for a real listener, so the code is checked in three ways: unit and truth tests for the parts with a right answer (filters, stimuli, exporters); simulated listeners, who answer from the same probit model, for the end-to-end behaviour; and the real profiles, for whether choices hold up on real data. The simulated criteria are guides rather than gates: they can only test the simulator's own assumptions, so a criterion not met is recorded, not necessarily fixed.

## Unit and truth tests

- The peaking biquad matches the RBJ cookbook.
- Stimuli stay in band at unit RMS, and nothing leaves above the ceiling.
- The filter fit recovers known filters, and the tone synth glides and ramps without steps.
- Exporters match golden files from real LSP and EasyEffects installs byte for byte.
- When available locally, the LSP and EasyEffects output runs through LSP's LADSPA plugin, and the PipeWire drop-in through a private filter-chain, compared with Flatiron's prediction.

## Simulated listener

A simulated listener exercises the scheduler, perceptual fit and correction pipeline. `flatiron simulate` reports how well the perceptual fit recovers it, and `sim/validate.py` checks the criteria below end to end. The ground truth per ear is a smooth tilt, plus the ISO 226 contour, plus a +4 dB excess 1/2 octave wide at 4 kHz, a +5 dB peak at 6.2 kHz (Q 6), a −3 dB dip at 8.4 kHz (Q 10) and a +3 dB peak at 11 kHz (Q 4). Answers come from the same probit model, with $\sigma_0 = 1$ dB, $d_0 = 0.5$ octave and $\lambda = 3\%$.

The criteria: each feature recovered within 1/24 octave in frequency and 1.5 dB in depth, and no false correction above 1.5 dB, after 600 trials per ear. The depth tolerances were relaxed from 1 dB, since the listener's own loudness discrimination is the practical limit. These aren't all met yet: very narrow features (Q 12 to 16) and those near the top of the range (around 11 kHz) come out about 1 to 2 dB shallow, and a null ear can get stray filters up to about 1.3 dB.

## Seating and multi-headphone scenarios

`flatiron simulate --scenario seating` and `--scenario consensus` run the criteria below for what's built, through the real scheduler, for one simulated listener (`--seed`). The command runs the standard headphone scenario; the others described below are `SCENARIOS` in `sim/consensus.py`. The rule for passing a multi-headphone scenario: the listener's own features are never corrected, and only headphone-specific ones are.

Ground truth per ear:

| Component | Contents |
| --- | --- |
| $T$ (listener) | +6 dB at 7 kHz (Q 8), +5 dB at 11 kHz (Q 10), −4 dB at 8.4 kHz (Q 12) |
| $\mathrm{cup}$ (all three headphones) | +4 dB at 9.5 kHz (Q 6) |
| $\mathrm{hp}_1$ | +5 dB at 5.8 kHz (Q 5) |
| $\mathrm{hp}_2$ | −4 dB at 8.0 kHz (Q 10) |
| $\mathrm{hp}_3$ | +3 dB at 10.5 kHz (Q 4), +3 dB at 3.2 kHz (Q 3) |
| Seatings | `reseat()` moves feature frequencies, and can add a deviation with a set spread and resonances only that seating has |
| $\mathrm{spk}_1$ | **NOT BUILT.** Random narrow ripple, 3 dB RMS above 3 kHz, plus a +6 dB peak at 6.3 kHz (Q 6) |
| $\mathrm{spk}_2$ | **NOT BUILT.** Independent random ripple, 3 dB RMS |
| Positions | **NOT BUILT.** $u_g$ with $\tau_\text{pos}$ 1 dB below 5 kHz rising to 4 dB at 12 kHz; 20 positions per ear, 24 trials each |

Each headphone gets its own smooth tilt (−1, +0.5 and −0.3 dB per octave), and every source gets the ISO 226 contour. Headphones get 600 trials per ear, over three sessions of 200, as in the standard validation.

**NOT BUILT:** speaker 1's own peak at 6.3 kHz is there to check that one speaker's colouration never reaches a headphone correction. A variant moves that peak to 9.5 kHz, on top of $\mathrm{cup}$, to check the split-speakers case: one speaker shows a shared headphone feature and the other doesn't.

Criteria, measured over 14 to 21 simulated listeners for what's built. Code and tests cite them by number.

| # | Criterion | Result | Status |
| --- | --- | --- | --- |
| 1 | **Comparison report.** Every $\mathrm{hp}$ feature is flagged against the right headphone within 1/24 octave. No $T$ or $\mathrm{cup}$ feature is flagged as one headphone standing out. False flags stay at the chosen target rate | 11 of 56 headphone features missed; 1.9 flags per ear on shared features and 0.7 elsewhere | Partly met: flags follow the veto, so they share its limits, including features merged with a neighbour |
| 2 | **Headphones-only consensus.** Every $\mathrm{hp}$ feature is corrected within 1.5 dB. $T$ and $\mathrm{cup}$ are left alone ($\mathrm{cup}$ is invisible without speakers, as designed). No false correction above 1.5 dB | See the table below | Not met by any configuration |
| 3 | **Both speakers, medium trust.** As in 2, and $\mathrm{cup}$ is also removed from all three headphones within 2 dB. $T$ and speaker 1's 6.3 kHz peak leave no correction | — | **NOT BUILT** |
| 4 | **Split-speakers variant.** With speaker 1's peak moved onto $\mathrm{cup}$, $\mathrm{cup}$ is kept, matching the "one shows it, one lacks it" row of the evidence table | — | **NOT BUILT** |
| 5 | **One speaker.** With speaker 2 only, any removal of $\mathrm{cup}$ is marked in the report as resting on one device | — | **NOT BUILT** |
| 6 | **Low trust.** Results match criterion 2 within 0.5 dB | — | **NOT BUILT** |
| 7 | **Speaker model.** $L$ recovers the mean over positions within 1.5 dB RMS from 1 to 13 kHz, at both 20 × 24 and 8 × 60 trials. Its reported SD covers the error about 68% of the time over 50 simulated listeners. It also runs with the fixed $\tau_\text{pos}$ at half the true curve, to show how much a wrong assumption costs | — | **NOT BUILT** |
| 8 | **Rule and edge cases.** A two-profile consensus runs and warns, and every row of the veto table comes out as specified (unit tests on the rule alone). Three-headphone cases come out near the worked values | Every row of the table; the worked values; the two-profile warning | Met |
| 9 | **Seating spread.** With three sessions of about 110 trials per ear, a true $\tau_\text{seat}$ of 2 or 3 dB is recovered within a factor of 1.5, and a true $\tau_\text{seat}$ of zero isn't estimated above 1 dB | Over 40 simulated listeners each, each of the three values within a factor of 1.5 of a true 2 or 3 dB in 63% of cases (the two-value fit, measured the same way: 70%). True 0: $\tau_\text{lo}$ below 1 dB in 82% of runs, $\tau_\text{mid}$ in 32% and $\tau_\text{hi}$ in 52% (the prior's median above 5 kHz is 1.5 dB; the two-value fit's treble value: 67%) | Partly met |
| 10 | **Coverage.** $L$'s reported SD covers the error about 68% of the time at 1, 3 and 8 sessions, and $m$'s SD does the same for the consensus | $L$'s 1 SD covers 69%, 71% and 73% at 1, 3 and 8 sessions (30 listeners each); the consensus SD is within 1% of Monte Carlo | Met, a little conservative |
| 11 | **Moving resonances.** A 9 dB resonance in two seatings of three is corrected within 2 dB of its median level. A resonance in one seating of three is dropped. With one session, a seating-specific resonance of up to about twice the prior's spread isn't corrected as confident | 9 dB in all three seatings: −8.0 dB. Over 20 listeners each: in two of three, median −5.0 dB (within 2 dB of −8 in 25% of runs; the two-value fit, −5.7 dB and 45%). In one of three: median 0.0 dB, with 1 dB or more of correction left in 15% of runs (two-value: 35%). 3 dB in one session: left alone in 80% | Partly met: the Gaussian deviation shrinks on-or-off resonances |
| 12 | **Speed.** A per-answer refit takes under 100 ms at 8 sessions of 150 trials on a fully refined 199-point grid, and a block-end pause is no longer than before the hierarchical fit | 10 ms per answer at 8 × 150 trials on 199 points; about 1 s at a block end; about 2 s for a full fit. The third seating-spread value made a full fit about 20% slower and changed nothing else | Met |

For criterion 2, the table gives passes out of 42 headphone checks over 14 simulated listeners. A check passes when the headphone's own features are corrected within 1.5 dB, shared ones are left alone, and nothing else is corrected above 1.5 dB. *Standard* is the ground truth above. *Broad* gives each headphone its own feature about an octave wide, and doubled tilts. *Contour* has a listener off ISO 226 by −4 dB near 2.5 kHz and +11 dB near 9 kHz. *Moved* puts the shared 7 kHz peak up to 0.2 octave apart between headphones, and doesn't judge that peak, since moving it is allowed.

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

The scoring favours leaving things alone, and can't tell whether a move is right for a real ear. An import limit of 0 scores the same as 1.5 dB, within one pass.

$\mathrm{hp}_2$'s 8.0 kHz dip and the listener's 8.4 kHz dip are 0.07 octave apart, inside the 1/12-octave stimulus, so they often can't be told apart.

# Findings

The experiments behind the design decisions, grouped by topic. Unless a section says otherwise, they come from scratch code, not in the repository, and ran on some of the real profiles (`sundara` and `sundaratone`, the same headphones measured with noise and with tones; `ft1`; `hd560`), on simulated listeners, or on simulated answers drawn from real profiles' own fits ("known curves"). They guided the design and aren't a substitute for the criteria above. They weren't re-run as the model changed, so treat the numbers as a record of why the design looks as it does.

## Hierarchical model

From a prototype run on `sundara` and `sundaratone` (three or four sessions per ear, 31 to 120 trials each) and on simulated listeners. The prototype held the noise fixed in its refits and used a looser convergence tolerance than the fit that was current then: a single pooled curve per ear, which the hierarchical fit replaced. Its results for speakers are under "Speaker profiles".

**Refit after one new answer:**

| Case | Pooled fit | Hierarchical |
| --- | --- | --- |
| Real data: 3 sessions, 108 points | 25 ms | 6 ms |
| 8 sessions × 150 trials, 100 points | 28 ms | 14 ms |
| 8 sessions × 150 trials, 199 points | 151 ms | 33 ms |

**Seating spread on real data:**

- A constant spread of 2 dB or more across frequency is clearly rejected. Spreads under about 1 dB can't be told apart from none.
- Spreads of 0.5 to 1.5 dB change the median SD by −1% to +18%. The pooled fit's SDs were already 1.2 to 2.0 dB at these trial counts, partly because $\sigma_0$ absorbed some of the seating differences.
- Treble spread, tested against simulations with no spread: `sundaratone` R is clear (none of 30 matched it), `sundara` L suggestive (10% matched), and the other two show none.
- With `sundara` L and $\tau_\text{seat}$ fixed at 0.5, 1 and 1.5 dB, the prototype keeps 5, 4 and 3 of the pooled fit's 6 features (with no seating step applied), losing the smallest ones first.

## Three-knot seating spread

The seating spread had two values, below 5 kHz and above 8 kHz. A third, at 12 kHz, was added so that a speaker's position spread, which keeps rising into the top octave, can be represented exactly, and so that headphones and speakers share one model. The prior curve didn't change. Compared with the two-value fit:

- **Real profiles** (all four, at default settings and at each profile's own): $L$ moved by at most 0.17 dB RMS over 1 to 13 kHz in any ear, the target by at most 0.22 dB RMS and 1.5 dB at any frequency (`ft1` L), and SDs above 8 kHz by −6% to +5%. Two borderline features changed: `hd560` L gained one at 4.07 kHz at default settings, and `ft1` R lost one at 1.75 kHz with its own. The three values read real shapes: `hd560` L rises (0.27, 1.06, 2.73 dB) where `ft1` L peaks at 8 kHz (0.63, 2.78, 1.55).
- **Simulated listeners** (three sessions of 110 trials): with a true spread of 0.5, 1 and 4 dB at the three knots, the 12 kHz value read 3.2 dB (median), against 2.45 for the two-value fit, which can't rise above 8 kHz. Coverage was unchanged (criterion 10). The cost is with no true spread: the 8 kHz value sits nearer its prior (criterion 9), since each value now has less data behind it.

## Grid

On `sundara` L (108 points, 399 on a 1/96-octave grid; pure noise, $k = 2$):

| Flagging rule | False flags per ear |
| --- | --- |
| Own grid, 2 adjacent points | 1.04 |
| Own grid, 1 point | 2.67 |
| 1/96 grid, 2 adjacent points | 2.61 |
| 1/96 grid, width at least 1/24 octave | 1.51 |
| 1/96 grid, width at least 1/12 octave | 0.57 |

- The covariance has rank 106 on both grids. Neighbour correlation rises from 0.72 to 0.98 on the finer one.
- Consensus SD, measurement noise only, compared with Monte Carlo: per-frequency weights give 0.19 dB against 0.43 dB with $\tau_\text{hp} = 0$, and 0.28 dB against 0.42 dB with $\tau_\text{hp} = 1$ dB. The covariance formula under "The soft median" matches Monte Carlo in both. This test ran on 1/96-octave curves, without robust reweighting.

## Channel balance

Measured with a prototype that applied the prior to each ear's finished fit as a Gaussian update; on the real profiles it agrees with the joint fit within 0.4 dB. The test used 150 simulated two-ear listeners (seating spread 0.5 and 1.5 dB, independent between ears) at 1 to 3 sessions of 110 trials per ear. The true L−R difference was one of: none; a mild Gaussian difference (0.5 and 1.5 dB); one of the real profiles' size (1 and 2.5 dB); or mild plus a 3 to 5 dB, Q 6 peak in one ear.

Against fitting each ear alone, the target error fell 0.12 to 0.24 dB RMS in every scenario, with fewer misses and false corrections and coverage unchanged. The cost is the ear-specific peak: at 3 sessions, 0.47 of it is corrected, against 0.58 with no prior. Also tried, the same way:

- Firmer priors (0.5 and 1.5 dB, and 1 and 2 dB) and always pooling the ears gained a little more on the target. They kept less of the ear-specific peak (0.28 to 0.39) and made the error bars overconfident.
- Pooling locally where the ears are consistent: the Bayes factor in half-octave windows, equality against a 1.5 and 3 dB difference. Blended by the probability of equality, it did about as well as the prior (0.49 of the peak) with slightly overconfident error bars. Pooled outright where that probability is over 1/2, it gained least and added false corrections, from the steps between pooled and separate.

## Shared noise

On the four real profiles' own sessions (each counted once), compared with each profile estimating its own noise:

- **One noise per ear fits.** Per profile, $\sigma_0$ ranged 1.08 to 1.37 dB in the left ear and 1.50 to 2.11 in the right, with $d_0$, $\lambda$ and $r$ similar apart from `hd560` L's $r$ of 2.55. Shared, the left ear reads $\sigma_0 = 1.19$ dB, $d_0 = 0.34$ octave, $\lambda = 1.4\%$, $r = 1.97$, and the right ear 1.97 dB, 0.53, 1.4% and 1.48. Letting each profile have its own improves the evidence by only 4.1 (L) and 4.4 (R) nats for 12 more parameters, against about 10.5 for chance at 5%. Each profile's own optimum, without the prior, is poorly pinned down: $\lambda$ runs to 0, $d_0$ to 1.73 octave, $r$ to 3.5. Noise bands and tones share without trouble (`sundaratone` costs 0.3 to 1.5 nats).
- **Small profiles bias it.** Through the real scheduler, with a true $\sigma_0$ of 1.5 dB (16 simulated listeners each), the shared estimate's median was 1.36 dB from one profile of 2 × 100 trials and 1.46 from three, but 0.49 from four profiles of one 100-trial session each. A profile of 3 × 200 trials gave 1.27 with one new 60-trial profile added and 0.65 with three. Hence the 200-trial minimum for each member, the same minimum a profile used to need for its own noise.
- **Effect on corrections** (with the third seating-spread value as well, against both off, at default settings and each profile's own): $L$ moves by at most 0.29 dB RMS over 1 to 13 kHz, the target by at most 0.35 dB RMS and 1.04 dB at any frequency, and SDs above 8 kHz by −7% to +10%. Borderline features change, most near 1 kHz. With its own settings, `hd560` L loses three (1.01, 1.45 and 4.98 kHz) and `hd560` R one (5.13 kHz); `ft1` loses one in each ear (1.04 and 7.06 kHz); `sundara` R gains one at 2.71 kHz.
- **How much one session moves it.** Leaving out any profile's last session moves the shared $\sigma_0$ by 1 to 5% and $d_0$, $\lambda$ or $r$ by up to about 9%. Moving $\sigma_0$ by 5%, or $d_0$ or $r$ by 10%, moves a profile's $L$ by 0.02 to 0.05 dB RMS (0.13 dB at most) and its SDs by about 3%.
- **The time-order bias isn't shared.** Per profile, $\beta$ in the left ear is +0.22 ± 0.09 dB for `ft1` against +0.89 to +1.28 for the others (Cochran's Q 58 on 3 degrees of freedom), and in the right ear +1.16 to +1.76 (Q 14, $p = 0.003$). It isn't drift: `ft1`'s sessions interleave in time with `hd560`'s and `sundara`'s, and its left ear reads low in every one. Each profile's $\beta$ is already well determined (SD 0.09 to 0.27 dB), so sharing it would gain little and remove a real difference.
- **Why speakers need it.** With 24- or 40-trial blocks, each block's deviation absorbs its answers and a speaker profile's own noise estimate collapses (see "Simulations for speakers").

## Treble noise

$r$ was checked on simulated answers from each real profile's own trials and fitted curve, 40 simulations for each of six ears, against the same fit with $r$ held at 1.

| True $r$ | Fit | $L$ RMS error (below 8 kHz, above) | 95% coverage | Median $r$ read |
| --- | --- | --- | --- | --- |
| 1 | $r = 1$ | 1.47 dB (1.42, 1.54) | 96.6% | |
| 1 | $r$ fitted | 1.48 dB (1.41, 1.60) | 96.4% | 1.13 |
| 2.4 | $r = 1$ | 1.83 dB (1.73, 2.00) | 96.4% | |
| 2.4 | $r$ fitted | 1.79 dB (1.55, 2.26) | 96.0% | 2.19 |

Fitting $r$ costs almost nothing when it isn't needed, but it doesn't help uniformly when it is: the curve below 8 kHz gains 10%, and the treble loses 13%. The likely reason, not verified, is that noisier treble answers leave more to the smoothness prior. $r$ reads about 13% high when the truth is 1, so the values of 1.25 to 1.4 on the real data aren't evidence of a rise.

## Noise variants not adopted

Run per ear on `ft1`, `hd560` and pooled `sundara`: six ears and 2,662 trials. $\Delta \log \mathrm{ML}$ is the summed change in evidence, and "held-out" is the summed change in log-likelihood over 5 folds repeated 4 times, using the posterior-averaged prediction.

Against the model without $r$:

| Variant | $\Delta \log \mathrm{ML}$ | Held-out | Outcome |
| --- | --- | --- | --- |
| Separate noise above 8 kHz | +13.8 | +15.3 ± 4.3 | Adopted as $r$. Almost all of it comes from `hd560` L and `sundara` L; bootstrap $p$ about 0.05 after searching over cutoffs |
| Noise linear in the gap, octaves | +1.0 | +0.1 ± 1.2 | No effect. Trial gaps cluster at 1/8 and about 0.9 octave, so the form is barely testable |
| Noise linear in the gap, Hz | +9.3 | +4.9 ± 5.9 | Mostly mimics the treble effect, and hurts right ears |
| Separate time-order bias on replay trials | −0.5 | +3.0 ± 5.0 | $\beta$ reads about 0.6 dB lower on replay trials, which looks real, but it doesn't help prediction |
| Separate noise on replay trials | +30.8 | +16.3 ± 6.5 | Not adopted, see below |

**Replay noise.** Noise on replay trials reads 1.5 to 2.6 times higher in all six ears, yet that isn't evidence that replaying adds noise. A replay is chosen after hearing the pair, so replayed trials are selected on the noisy percept. A simulated listener with fixed noise who replays near-zero percepts, with half the noise repeating, reproduces a ratio of about 2. Total time since first playback carries the same information as replay (adding replay to it gains 0.0 held out), and overall noise doesn't rise with a session's replay rate. A causal effect isn't excluded: only randomising whether replay is available on each trial could settle that. Either way, the term barely changes the curve on simulated data: $L$'s RMS error goes from 1.38 to 1.40 dB if the cause is selection, and from 1.83 to 1.75 dB if replay really adds noise, with coverage unchanged. It improves prediction of the answers, not the measurement.

**Noise distribution.** The probit link was compared against other shapes, each scaled to the same interquartile range, on the model with $r$:

| Link | $\Delta \log \mathrm{ML}$ | Held-out | Largest $L$ change |
| --- | --- | --- | --- |
| Logistic | +0.7 | +2.9 ± 1.1 | 0.23 dB |
| Student-t, $\nu$ = 5 | +0.6 | +3.3 ± 1.5 | 0.31 dB |
| Student-t, $\nu$ = 3 | 0.0 | +3.0 ± 2.6 | 0.48 dB |
| Mixture, 30% of trials 3× wider | −0.7 | +2.0 ± 3.2 | 0.74 dB |
| Cauchy ($\nu$ = 1) | +2.3 | −31.3 ± 8.6 | |
| Generalised normal, shape 3 (lighter tails) | −3.4 | −5.6 ± 1.5 | |
| Generalised normal, shape 4 (lighter tails) | −6.5 | −10.2 ± 2.3 | |

Those are 7 of the 14 alternatives tried. Much heavier and much lighter tails both predict clearly worse. Moderately heavier tails gain about 3 nats held out, but simulated Gaussian listeners gain as much evidence about half the time ($p$ about 0.55), and no held-out null was run, so that gain isn't calibrated. No tail absorbs the replay ratio, which grows slightly with heavier tails. The link stays Gaussian: at most 0.3 dB of curve change for logistic or $\nu$ = 5 doesn't justify the added complexity.

**Hard against easy trials.** A Gaussian listener errs more on hard trials, but that's already in the distribution, so a width fitted separately to each should match. Difficulty was the margin predicted from the other trials, so it's defined by the stimulus rather than by the percept. The ratio of width on hard trials ($\lvert z\rvert < 1$) to easy ones has a geometric mean over ears of 1.11, against 1.17 (5th to 95th percentile 1.04 to 1.42) from simulated Gaussian listeners, and 1.25 (1.04 to 1.69) from simulated listeners who also replay near-zero percepts. The expected ratio is above 1 because the sorting uses an estimated margin. Pooled by margin, observed agreement tracks the prediction, with one exception out of six bins: at a margin of 0.5 to $1\sigma$ there are 16.5 fewer errors than predicted, outside the simulated 5th to 95th percentile range of −7.9 to +16.8.

## Soft rejection

On `ft1`, `hd560` and pooled `sundara`, comparing the fit with and without treble noise ($r$). The table gives the largest target difference between the two per ear, averaged over six ears on default settings, with "no gate" accepting every narrow lobe.

| Width $w_\text{soft}$ | 0 | 0.5 | 1 | No gate |
| --- | --- | --- | --- | --- |
| Largest target difference | 0.73 dB | 0.48 dB | 0.43 dB | 0.46 dB |

Marginal flips are about a third of the peak change and about 13% of the RMS change at width 0.5; the rest is the curve moving. With the veto on, the jumps are veto flips, which this doesn't touch. The filter fit, which then stopped within 2 SD of the target, jumped by itself by up to 2.9 dB with no change in the target, and soft rejection made that worse: the mean largest step went from 0.55 to 1.20 dB. "Filter fit" below describes what changed.

On known curves (40 simulations for each of six ears), two runs from the same curve differ by 1.78 dB RMS in the target when hard, 1.76 at width 0.5 and 1.74 at width 1, with the error against the truth unchanged. Soft rejection doesn't reduce false left/right imbalance: that's measurement noise.

## Soft veto

With `sundara`'s real settings (a consensus reference and a veto by `ft1` and `hd560`). The hard veto's target jumps between a fit without treble noise and one with it came from four edges: near-ties in the median (1.24 and 1.09 dB), a voter lost from the overlap check after a peak jumped across a two-dip lobe (2.23 dB), and the 1/12-octave limit (0.52 dB).

Interpolating the fit between the two models in 40 steps, with soft rejection at width 0.5 in the soft rows; largest values in dB, left and right:

| Veto | Largest target step | Target, one model to the other | Filters, one model to the other |
| --- | --- | --- | --- |
| Hard, no soft rejection | 2.23, 1.09 | 2.44, 1.29 | 2.35, 1.16 |
| $\gamma = 0.05$ | 0.02, 0.02 | 0.61, 0.73 | 0.65, 0.70 |
| $\gamma = 0.1$ | 0.02, 0.02 | 0.47, 0.71 | 0.37, 0.58 |
| $\gamma = 0.2$ | 0.02, 0.02 | 0.45, 0.68 | 0.45, 0.49 |

That's one path, so it doesn't show the target is continuous everywhere.

On known curves (40 simulations for each ear of all three profiles, averaged over `sundara`'s two ears, no error bars, one fit of 240 not converged), in dB RMS:

| Setup | Two runs from one curve: target | Filters | Target error against the truth | Target size |
| --- | --- | --- | --- | --- |
| Hard | 1.16 | 0.91 | 0.98 | 1.98 |
| Soft rejection only | 1.15 | 0.89 | 0.95 | 1.97 |
| $\gamma = 0.1$ only | 1.07 | 0.81 | 0.89 | 1.98 |
| Both, $\gamma = 0.1$ (the default) | 1.05 | 0.80 | 0.86 | 1.97 |
| Both, $\gamma = 0.2$ | 1.02 | 0.77 | 0.83 | 1.95 |
| Pointwise, soft rejection | 1.01 | 0.74 | 0.79 | 1.75 |

The truth is each setup's own correction of the true curves. So the soft veto helps modestly, and most of the difference between runs is measurement noise. Pointwise scores about as well but corrects about 12% less overall, and it still has a target step of 0.88 dB on the same path.

## Filter fit

The fit used to weight by 1/SD and stop within 2 SD, so it could drift up to 2.55 dB from the target in the treble, where the SD is largest. Along the path above, over six ears on default settings and `sundara`'s two on its real settings, with soft rejection at width 0.5 and $\gamma = 0.1$, 7 of 9 jumps over 0.5 dB in the narrow filters came with the filter count changing by one. Removing the 0.5 dB minimum, or lowering the compensation threshold, made no difference. Starting each step from the last one's filters cut the 9 to 2, so many nearly equal solutions contribute too.

Largest values in dB, in that same setup, just before a last small change to the soft veto (a voter with this feature's shape now leads to no correction):

| Rule | Largest total step, mean over ears, worst | Filters to target, RMS, worst | Narrow filters, mean, most | Opposed pairs per ear |
| --- | --- | --- | --- | --- |
| 1/SD, within 2 SD (before) | 1.06, 2.93 | 0.46, 2.55 | 5.7, 8 | 0.37 |
| Plain dB, within 1 dB | 0.25, 0.77 | 0.26, 0.98 | 7.3, 11 | 0.98 |
| **Plain dB, within 0.5 dB** (adopted) | 0.34, 0.77 | 0.14, 0.50 | 10.3, 14 | 0.98 |

The 1 dB row has the smaller steps on this path, but a feature's filter then appears only when its target passes 1 dB, about 1.4 dB at once on a single peak, against 0.7 dB at 0.5 dB. An opposed pair is opposite gains within 1/3 octave together reaching under 60% of the larger. They usually follow an asymmetric target, and one of 16 fits on real data had a truly cancelling one (±8 dB together reaching 0.8 dB). Export rounding moves any fit's response by at most 0.02 dB. Replacing a pair with one filter almost never kept the fit.

On known curves (40 simulations per ear, RMS dB; one fit of 240 not converged):

| Settings | Rule | Filters to truth | Two runs from one curve, total | Narrow filters, mean, most |
| --- | --- | --- | --- | --- |
| Default | Before | 1.54 | 0.98 | 5.3, 9 |
| Default | Within 0.5 dB | 1.48 | 1.00 | 9.8, 18 |
| `sundara`'s | Before | 0.89 | 0.87 | 5.5, 8 |
| `sundara`'s | Within 0.5 dB | 0.89 | 0.87 | 8.9, 12 |

So accuracy and false left/right difference don't change: the rule keeps the filters on the target, and the noise is in the target.

The stop once also counted the fade above $f_\text{hi}$. An error there can't close, so about 9% of runs at 0.75 dB used 14 to 20 narrow filters on a chain of Q 16 filters below 13 kHz, and with all 20 bands gone they lost compensation, moving the total by up to 5 dB. With the stop inside the range and bands held back, no run reaches the budget of 24.

With the budget tightened on the real fits, compensation is about as close to its goal at 6 bands as at 32 (mean RMS 0.12 dB), and only the narrow corrections suffer, with the largest error against the target rising to about 3 dB.

## Joint baseline

Validation used 300 simulated listeners per scenario (3 to 6 features, 3 to 9 dB, Q 2 to 10, three sessions of 200 trials), all against ISO 226. *Standard* listeners are on ISO 226. *Contour* listeners have two broad Gaussians off ISO 226, −6 to 0 dB near 2 to 3.5 kHz and +5 to +13 dB near 7 to 11 kHz: the real profiles' condition. "Real ears, SD × 0.8 to 1.2" scales the error bars of each real ear's fit in 2.5% steps, which is the path along which the robust baseline tips.

| Check | Robust | Joint |
| --- | --- | --- |
| Real ears, SD × 0.8 to 1.2 in 2.5% steps: largest baseline step | 0.10 to 2.14 dB | 0.03 to 0.25 dB |
| Contour listeners (120), same path: median, 90th percentile, largest; share over 1 dB | 0.12, 5.8, 13.1 dB; 23% | 0.04, 0.20, 1.2 dB; 1% |
| Real ears, curve jittered by 0.3 SD (10 draws): largest baseline movement | 0.7 to 6.0 dB | 0.6 to 3.3 dB |
| Real ears, two seeds | n/a | under 0.02 dB |
| Standard: target RMS error; misses, false corrections per ear; median share corrected (peaks, dips) | 1.674 dB; 0.25, 0.50; 0.68, 0.53 | −0.024 ± 0.013 dB against robust; 0.22, 0.49; 0.67, 0.53 |
| Contour: the same | 2.030 dB; 0.75, 1.39; 0.29, 0.46 | +0.004 ± 0.024 dB against robust; 0.76, 1.35; 0.30, 0.46 |

On the real profiles the targets move by 0.03 to 0.5 dB RMS, except `sundaratone` R (1.2 dB): its −3 dB dip at 5.3 kHz is dropped, because the residual's SD there doubles with the baseline's sensitivity. `sundaratone` L gains its 7.2 kHz peak (+10 dB) as a feature of its own, where the robust baseline merges it with the 11 kHz one.

The aims weren't all met: the largest step is still above 0.2 dB on some ears and listeners, and jittering the curve still moves the baseline by up to 3.3 dB (`sundara` R; `sundaratone` R moves 2.2 dB against 1.95 with the robust fit). Those come from the data, not the sampler: the posterior's weight on each reading moves with them. The floor trades steps against false corrections: at 1 dB, 90% of the contour listeners' steps were under 0.13 dB, but false corrections rose by 0.14 to 0.23 per ear.

**Residual covariance with fixed weights.** A version holding each point's posterior feature probability fixed, as the robust fit holds its weights, was tried with an earlier sampler and no floor: on known curves it made 0.07 to 0.22 more false corrections per ear, against 0.02 to 0.07 fewer misses.

**Tried first: a dictionary of bumps.** The design proposed a smooth tonality plus a regularised horseshoe over a dictionary of about 490 RBJ-shaped bumps no wider than $W$, sampled by Gibbs. Its prototype failed on two counts. The sampler didn't mix: chains started with every bump off and with every bump on disagreed by 2 to 6 dB on the real ears (9 dB with a sparser prior), against an aim of 0.1 dB. And bumps tile: two or three adjacent bumps no wider than $W$ sum to anything broad, so the baseline sank to the bottom of the curve (`sundaratone` L flat at about −3 dB, `hd560` L flat at −6), and moved by several dB with the priors and with whether $\Sigma_D$'s correlations were used. A single moment-matched importance sampler also collapsed (effective sample size 1 to 10) on a third of the contour listeners, which the tempering, the mode proposals and the multiple importance sampling fix.

## Shared baseline

Measured on the real profiles (each with its own settings) and on 80 simulated two-ear listeners in each of three scenarios. Every listener has its own tilt and a contour off ISO 226 in both ears (−6 to 0 dB near 2 to 3.5 kHz, +5 to +13 dB near 7 to 11 kHz), and 3 to 6 random features per ear, independent between the ears (3 to 9 dB, Q 2 to 10), with three sessions of 200 trials per ear. *Same*: the ears' broad shape is identical. *Shelf*: one ear has an extra ±2 to 4 dB, 1 to 1.5-octave bump at 8 to 12 kHz, about the real L−R differences above 8 kHz. *Tilt*: the ears' tilts differ by 0.3 to 0.6 dB per octave. The truth is each ear's features alone, since broad shape is kept.

| Scenario | Baseline | Target RMS error, separate | Shared − separate (± SE) | 5th, 95th percentile per ear | False corrections per ear |
| --- | --- | --- | --- | --- | --- |
| Same | robust | 2.380 dB | +0.018 ± 0.029 | −0.42, +0.66 | 0.56 → 0.49 |
| Same | joint | 2.431 dB | −0.041 ± 0.033 | −0.66, +0.53 | 0.49 → 0.47 |
| Shelf | robust | 2.355 dB | +0.009 ± 0.033 | −0.56, +0.77 | 0.53 → 0.51 |
| Shelf | joint | 2.417 dB | −0.028 ± 0.032 | −0.71, +0.52 | 0.48 → 0.44 |
| Tilt | robust | 2.412 dB | −0.030 ± 0.037 | −0.68, +0.60 | 0.61 → 0.56 |
| Tilt | joint | 2.409 dB | −0.030 ± 0.041 | −0.63, +0.81 | 0.54 → 0.45 |

On average it's neutral, with slightly fewer false corrections, and the broad differences between the ears cost nothing measurable: they're broad, so they're kept either way. Single ears move by up to about 2 dB either way.

On the real profiles the targets move 0.3 to 0.7 dB RMS per ear (`sundaratone` R 1.4). The L−R difference between the targets falls on Sundara (1.92 to 1.54 dB RMS), SundaraTone (2.11 to 0.75) and HD560 (1.26 to 1.14), and is unchanged on FT1 (1.53 to 1.55). On `sundara` the left ear's 11.6 kHz cut shrinks from 6.2 to 4.0 dB, and the right ear's excess there stays tonality. On `sundaratone` (robust, ISO 226) the left ear had tipped, correcting 6 to 13 kHz as one 12.5 dB broad feature; with the baseline shared, both ears have their 7 and 11 kHz peaks corrected as features of their own. On the SD-scale path (0.8 to 1.2 in 2.5% steps, ISO 226), the largest baseline step is about the same as for separate baselines on the joint baseline (at most 0.18 dB) and on robust `hd560` and `ft1`. On robust `sundara` it's up to 0.41 dB (from 0.15), and on robust `sundaratone` 1.2 dB in both ears (from 1.9 and 0.6).

**A per-ear tilt as well** was tried in the prototype: each ear gets a level and a slope. In simulation it had the narrowest spread (95th percentile at most +0.3 dB per ear), but on Sundara it gave back most of the difference (the left cut at 12 kHz −5.4 dB against −4.4 with level only): a slope between the ears can absorb a broad treble difference. Not adopted, since the point is one broad shape.

## Robust baseline alternatives

Two other ways to stop the robust baseline tipping were tried and rejected:

- **Scaling the robust weights by the residuals' own spread** (MAD, or a lower quantile), held from the Huber pass. That makes the baseline exactly immune to the error bars' scale. But on the real ears even the lowest tenth of the residuals sits 1.2 to 7 SD off the stiff baseline, so no noise-only scale exists. The cutoff in effect grows several-fold, the baseline bends into the features, and targets move by up to 4 dB on ears that never tipped (`ft1`). On known curves it was slightly worse throughout (target +0.03 dB RMS, +0.08 false corrections per ear).
- **A rolling-ball (morphological) baseline**: the average of opening $\circ$ closing and closing $\circ$ opening with a flat element of width 0.5 to 2 $W$ on the lightly smoothed curve, then smoothed. It can't tip: on the real ears it ignores the error bars' scale entirely, and jittering the curve by 0.3 SD moved it at most 0.9 dB (up to 5 dB for the spline). But a flat element only clips a feature where it's narrower than the element, so a feature's skirts stay in the baseline. On known curves, at every width tried, it corrected less of each feature: about 45% against 60%, and dips worst (median 0.33 against 0.69). Feature error rose 0.5 dB and target error 0.1 to 0.2 dB RMS, though with fewer false corrections. In the multi-headphone simulator's contour scenario, against ISO 226, it was about even with the spline at $W$ (narrow-correction error 2.3 against 2.4 dB) and worse when wider, while the consensus reference brought either to 1.0 to 1.1 dB. The reference matters far more there than the baseline does.

# Known limitations

- **The MAP isn't unique.** The lapse term makes the posterior non-concave, so two nearby solutions can exist. On one simulated data set, moving $\sigma_0$ from 1.12 to 1.15 dB moved the curve by up to 1 dB, with nothing else changed. Which solution a fit lands on can depend on small changes to the noise parameters, which is one way a feature near the threshold can flip. Over 45 seeds this averages out (0.66 dB shape error, 1.12 worst).
- **The baseline can tip.** The robust baseline's Tukey weights depend on residual/SD. When a block wider than $W$ sits far off the reference, the baseline either rises to meet it or rejects it as a feature, and the choice can turn on the error bars' scale. Two solutions coexist near the edge.
    - On `sundaratone` L against ISO 226, 6 to 13 kHz sits 8 to 15 dB above 4 kHz. Scaling the SDs by 0.875 instead of 0.9 moved the baseline at 13 kHz from 3.5 to 7.6 dB, and the target by up to 6 dB, merging the 7 and 11 kHz peaks. `sundara` L is as close to the edge (3.5 dB per 2.5% of SD) and `hd560` R is moderately sensitive (1 dB); `ft1` isn't.
    - Any change that shrinks the error bars can tip it, the channel-balance prior included, and so can a new session.
    - Against the consensus reference the block is mostly in the reference, and the baseline is stable (0.4 dB at most per 2.5%).
    - The simulated listeners hardly ever tip (0.5% of them by over 1 dB per 2.5% of SD): they follow ISO 226 closely. The multi-headphone simulator's "contour" scenario has the real condition.
    - `--baseline joint` (experimental) reduces the largest step to 0.25 dB on the real ears. Two other fixes were rejected (see "Robust baseline alternatives" under "Findings").
    - A shared baseline doesn't stop it tipping, but both ears now tip together. On `sundaratone` against ISO 226 the shared robust baseline still steps by up to 1.2 dB per 2.5% of SD, in both ears (1.9 and 0.6 dB apart).
- **The shared baseline takes the ears' errors as independent.** The balance prior correlates the two ears' curves, but the fits don't keep that covariance, so the other ear's share of a residual's error bars is approximate.
- **Broad corrections are only as good as the reference.** With `--broad-strength` above 0, individual ears can sit several dB off ISO 226 in the ear-gain region, which is why tonality is kept by default.
- **The treble is noisy.** Fit variability above about 8 kHz is high. Reseats and the seating model mitigate it; the correction range stops at 13 kHz by default and shouldn't go below 12 kHz.
- **The diffuse-field reference needs a user-supplied ISO 389-7 difference table.**
- **Equaliser's (macOS) handling of imported Q and filter mode is unverified.**
- **The veto depends on detection.** Which features exist, and how wide they are, decides what matches, and a feature's own part can stay merged with a neighbour's. The pointwise mode avoids this, but its separate baselines make it sensitive to broad differences between headphones.
- **Nothing says whether a veto's answer is right for a real ear.** The simulator can only check its own assumptions about what's shared.

# Open questions

- **Seating priors and shape.** The $\tau_\text{seat}$ prior medians and the three-value shape need tuning on more data. Criterion 11 is accepted as partly met for now; if a Gaussian $u_g$ keeps handling on-or-off resonances badly, even with the median rule, a heavy-tailed $u_g$ is the next thing to try.
- **Minimum widths.** 1/12 octave may miss the narrowest features at $k = 2$, so $k$ and the width should be chosen together. The narrow test has a related weakness: "two adjacent points" means 1/24 octave on most of a fit's grid and 1/48 where it's refined. Should it move to a width in octaves too? That would change existing corrections.
- **The $\tau_\text{hp}$ floor.** 0.5 dB is a judgment call, so that the most-measured headphone can't dominate where the profiles happen to agree. Tune it in the simulator.
- **IEMs in the consensus.** Excluded by default, because they lack the pinna and concha. They could be allowed as hearing-only evidence, separating hearing from ear acoustics. Not needed for corrections, so deferred.
- **Heavier tails for the channel balance.** See "Channel balance".

# Speaker profiles (NOT BUILT)

**NOT BUILT: nothing in this section exists in the code.** There's no `speaker` profile kind, block runner, position model, joint model or speaker simulation, and none of the commands, options or file fields below exist. The only trace is two placeholders, `speaker_trust` and `speaker_response`, always null, in the consensus cache key. This is the design to build from. In this section a group $g$ is a speaker block.

**In brief.** Speaker profiles answer the same question as the comparison and consensus: which narrow features belong to the listener, and which to the headphone. They'd add one thing the headphones can't show: a feature every headphone shares may still be an over-ear coupling artifact, because no speaker has it. A speaker is measured in many short blocks, each at a different position, with the other ear plugged; the hierarchical model averages the positions into one direction-averaged curve per ear. That curve then enters a joint model with the headphones, where it can only demote a shared headphone feature to a coupling artifact, never create a correction of its own. Speakers would be downstream of the cached perceptual fits, so like every other setting they could be switched on and off without new trials.

## Speaker sessions (NOT BUILT)

**Setup.**

- `flatiron profiles new NAME --kind speaker`, with the speaker's label and which output channel drives it (`--channel L` or `R`). Only one speaker plays: two speakers playing the same burst would comb-filter at the ear.
- Distance at least 0.7 m where the level allows (a phone at arm's length is acceptable, but logged as near field). The level screen runs as usual. It warns if the set level is unusually high, since speakers need more drive than headphones and the room is open.
- A quiet room. The level screen adds a one-off check: the listener confirms they can't hear the burst with the speaker muted, which catches hum or fan noise near the stimulus level.

**Blocks.**

- One block = one session file = one open ear and one position. The default is 24 answered trials, about a minute (`--block`, 16 to 60).
- Before a block, the screen names the open ear and asks for a foam earplug in the other ear, fully inserted. Ears alternate every block: a block on L, then on R at a new position, and so on.
- Position plan: the screen suggests the next position from a rotation meant to spread directions around the open ear: front, 30° and 60° toward the open side, side-on, rear quarter (turn the chair), plus one higher and one lower placement. The listener can accept it or type their own label. Optional numbers (azimuth, elevation, distance) are stored when given.
- Prefer turning the head or chair over moving the speaker. The speaker then always faces the listener, so its own on-axis colouration stays constant across positions instead of adding variance.
- Suggested minimum: at least 8 positions and about 400 to 500 trials per ear in total, such as 20 blocks of 24 or 8 of 60. About 200 trials per ear was too few in simulation (see "Prototype results for speakers"). The report shows how much more trials or positions would tighten the curve.
- Anchor pairs: about a quarter of each block goes to a small fixed set of frequency pairs repeated across blocks, so that differences between positions can be observed directly (see "Open questions for speakers").
- No seating step. Positions are supposed to differ, and that variation is modelled instead.

**Trials.**

- Same stimuli, level offsets, rove, catch trials, undo and reaction-time logging as headphone trials. Bursts go to the speaker's channel. The trial's ear is the open ear.
- Adaptive selection runs on the pooled speaker model (below), not per block, so a block's trials are spent where the average curve is least certain.
- The channel check is replaced by a plug prompt. A plugged ear can't be verified by listening, so it's a confirmation only.

## Positions as random effects (NOT BUILT)

This is the perceptual model with blocks as the groups. Per ear, the perceived level at block $g$ is $L(f) + u_g(f)$:

- $L$ is the quantity of interest: the average over the directions the position plan is meant to cover, with each position weighted equally. It has the same priors as a headphone $L$.
- $u_g$ is block $g$'s deviation, with amplitude $\tau_\text{pos}(f)$ and correlation width 1/12 octave.
- With 24 trials a block can't pin down its own $u_g$. The model doesn't need it to: $L$'s uncertainty includes $\tau_\text{pos}^2$ divided by the number of positions. That's what "averaged, not rejected" means in practice.
- $\tau_\text{pos}$ is fixed rather than estimated at this block size, by default at 1 dB below 5 kHz rising to 4 dB at 12 kHz. Estimating it alongside $L$ from 24-trial blocks doesn't work: in simulation it ran up to many times the true value, and a prior would only hide that. The fixed curve is an assumption, so the report labels it as one. Estimation (with a log-normal prior, log SD 0.3, around that curve) is enabled only if the simulator shows that anchor pairs make it reliable.
- The result is a perceptual fit like any other ($\hat L$ with covariance), so `report`, `compare` and the pipeline can treat it as a profile. Its kind marks it as speaker evidence: it's drawn dashed in comparisons and doesn't vote by default.

The SD covers the sampling of positions and the judgment noise. It doesn't cover bias from the plan itself: the rotation isn't a random draw of directions, so if it misses a region, nothing in the error bars shows that.

Averaging in dB fills in a moving notch less than a true diffuse-field (power) average would. "Open questions for speakers" covers whether that's worth correcting.

## Folding speakers into the reference (NOT BUILT)

Speakers and headphones go into one joint model. Speakers are evidence about one thing only: whether a feature every headphone shares is really the listener's. They can demote a shared feature to a coupling artifact. They never create a correction target by themselves.

Every residual (narrower than $W$) is a sum of parts. Hearing sensitivity and the ear canal are in all of them and fold into $T$. Speakers are the only sources without $\mathrm{cup}$, so that's the one place they change the outcome.

| Source | What its residual contains |
| --- | --- |
| Headphone $h$ | $T + \mathrm{cup} + \mathrm{hp}_h$ + noise |
| Speaker $j$ | $T + \mathrm{spk}_j$ + noise (noise includes position spread) |

- $T$: the listener's own structure. Direction-averaged pinna and concha response, canal and hearing. This is what a correct headphone should reproduce.
- $\mathrm{cup}$: over-ear coupling structure common to all the headphones (how a cup loads this pinna). Invisible to a headphone-only consensus.
- $\mathrm{hp}_h$: headphone $h$'s own aberrations. Sparse: at most frequencies none, a few real peaks and dips.
- $\mathrm{spk}_j$: speaker $j$'s colouration plus the room. Unknown, and for cheap speakers possibly large.

A headphone-only consensus estimates $T + \mathrm{cup}$. Speakers are the only data without $\mathrm{cup}$, but they carry their own $\mathrm{spk}_j$. So speaker data is useful exactly where it's trusted to say "this shared headphone feature isn't on the listener's side."

**The joint model.** Per ear, estimate $T$, $\mathrm{cup}$, every $\mathrm{hp}_h$ and every $\mathrm{spk}_j$ together by MAP:

- All these parts live on the common 1/48-octave grid. Each source is compared with them on its own grid through $A_p$, with its residual covariance as its noise. On a source's own grid that covariance is full rank except for the level and slope the baseline removes, which a pseudo-inverse excludes. On a finer grid it couldn't be used at all: most patterns would have no modelled noise, and so infinite weight.
- $\mathrm{hp}_h$ and $\mathrm{spk}_j$ get heavy-tailed priors (Student-t, 3 degrees of freedom, scales $\sigma_\mathrm{hp}$ and $\sigma_\mathrm{spk}$), so a few large features are cheap and broad noise isn't. With several headphones, this is the soft median in a different form.
- $\mathrm{cup}$ gets a Gaussian prior with a small scale $\sigma_\mathrm{cup}$ (default 1.5 dB). Without strong evidence, $\mathrm{cup}$ stays near zero and shared headphone features are attributed to $T$ and kept.
- $T$ gets only the smoothness prior.
- Prior terms are weighted by the octave width each grid point covers, relative to 1/24 octave as in the soft median (the unit the perceptual fit's curvature penalty is scaled to), so their strength doesn't depend on grid spacing.
- It's solved by IRLS for the Student-t terms, with covariances from the Laplace approximation at the solution.

This part was written before the consensus was split into a reference and a veto, and is to be revisited when speakers are built: the correction for headphone $h$ is the pointwise rule from `--veto-mode pointwise`, with $\hat T$ as the median. What $h$ shows beyond $\hat T$ is $\mathrm{cup} + \mathrm{hp}_h$, and that's what gets removed.

How the evidence plays out:

| Headphones | Speakers | Result |
| --- | --- | --- |
| One shows a peak | Any | Peak attributed to $\mathrm{hp}_h$: removed from that headphone |
| All show a peak | Both show it | $T$: kept everywhere |
| All show a peak | Both confidently lack it | $\mathrm{cup}$: removed from every headphone |
| All show a peak | One shows it, one lacks it | Mostly $T$ (the t-prior treats the odd speaker as $\mathrm{spk}_j$): kept |
| All show a peak | Too noisy to say | $\mathrm{cup}$ stays near zero: kept |
| None show a peak | Both show it | $T$ has a peak the headphones lack: not imported under remove-only |

**Trust, and what speakers never decide.**

- `--speaker-trust low`, `medium` or `high` sets $\sigma_\mathrm{spk}$ to 6, 3 or 1.5 dB. Low means speakers almost never overturn the headphone consensus.
- Speakers inform only structure narrower than $W$. The broad shape of a speaker in a room isn't trusted at all unless its measured response is supplied (`--speaker-response FILE.csv`, subtracted before detrending). Even then it stays behind `--broad-strength`.
- Two unrelated speakers count for much more than one. A colouration shared by a phone and a SoundSticks satellite is unlikely, so features they agree on are attributed to $T$. With a single speaker, the report says that shared-feature removals rest on one device.
- IEMs don't count toward $T$. They bypass the pinna and concha, so a peak they lack says nothing about whether it's the listener's. They're shown in comparisons as a diagnostic: a feature that persists at two insertion depths is in hearing, not in the ear's acoustics.

## What the speaker work adds (NOT BUILT)

| Command | New option | Does |
| --- | --- | --- |
| `profiles new`, `profiles set` | `--kind speaker`; `--channel L` or `R` on `new` | Sets the profile kind and the channel that drives the speaker |
| `run` | `--block N` (speaker only, default 24), `--position LABEL` | Runs one speaker block; refuses `--sweep` on speakers |
| `compare` | `--include-kind speaker` | Lets speakers count, as `iem` can now |
| `config` | `--speaker-trust low`, `medium` or `high`; `--speaker-response FILE` | Speaker handling |

On disk:

- `profile.json`: `kind: speaker`, plus `speaker` (label) and `channel`. Settings gain the speaker trust and response.
- Speaker session files: `open_ear`, `position` (`label`, optional `azimuth`, `elevation`, `distance_m`), `speaker_channel`, and the plug confirmation. Trials are unchanged.

Modules:

- `fit/consensus.py`: the joint $T$/$\mathrm{cup}$/$\mathrm{hp}$/$\mathrm{spk}$ model.
- `session/`: a speaker block runner (plug prompt, position plan, anchor pairs, ear alternation).
- `sim/`: the speaker position model and speaker ground truth (see "Seating and multi-headphone scenarios").

Build order, continuing from what's built:

1. Speaker profile kind and block runner: plug prompt, position plan, anchor pairs, ear alternation.
2. The speaker position model, simulator criterion 7.
3. The joint $T$/$\mathrm{cup}$/$\mathrm{hp}$/$\mathrm{spk}$ model with speaker trust, simulator criteria 3 to 6.

## Prototype results for speakers (NOT BUILT)

Scratch code only, with simulated speaker pairs that were random rather than adaptive, and $\tau_\text{pos}$ known:

| Positions × trials | RMS error, 1–13 kHz | Median SD | SD, 8–13 kHz | 68% coverage |
| --- | --- | --- | --- | --- |
| 8 × 24 | 2.14 dB | 1.93 dB | 2.80 dB | 0.70 |
| 16 × 24 | 1.47 dB | 1.24 dB | 1.88 dB | 0.66 |
| 8 × 60 | 1.22 dB | 1.15 dB | 1.70 dB | 0.71 |

- At 8 × 24, fixing $\tau_\text{pos}$ at half the true curve only lowers coverage to 0.64.
- Estimating $\tau_\text{pos}$ at 8 × 24 with $L$ also free: a true 4 dB gives a median at the top of the tested range (14 dB), and a true 1 dB gives a median of 5 dB, ranging from 1 to 14. With $L$ held at the truth, the Laplace approximation agrees with brute-force integration and centres near the true value, so the approximation isn't the cause. At 8 × 60 the median is 5 to 6 dB for a true 4 dB.

## Open questions for speakers (NOT BUILT)

- **dB vs power averaging over positions.** A diffuse field averages power, which fills in moving notches more than averaging perceived dB does. A power average of per-position posteriors is possible, but each position is barely measured, so it'd be noisy. Proposed: dB averaging, with the expected bias at notch frequencies estimated in the simulator before deciding.
- **The default $\sigma_\mathrm{cup}$, and whether $\mathrm{cup}$'s prior should be Gaussian.** 1.5 dB keeps shared features unless both speakers clearly lack them. Too small, and real coupling artifacts are never caught; too large, and noisy speakers start removing the listener's own features. A rough hand calculation (one frequency, no noise, ignoring $T$'s smoothness prior and $\mathrm{hp}_h$) suggests the bigger problem is the shape of the prior. With both speakers lacking a 4 dB shared peak at medium trust, the best split is about $T = 2.6$ and $\mathrm{cup} = 1.4$ dB, which would miss criterion 3. At 8 dB, $\mathrm{cup}$ only reaches about 1.7 dB, because the Gaussian penalty keeps growing while the speakers' Student-t penalty levels off. Check this in a simulation before building the joint model, and try a heavier-tailed prior on $\mathrm{cup}$. Then tune it, using the real data with `compare` as a sanity check.
- **Why $\tau_\text{pos}$ runs away, and whether anchor pairs fix it.** The likely cause is that 24-trial blocks rarely compare the same frequencies, so differences between blocks can't be seen, but this isn't proven. Anchor pairs are untested. If they don't work, $\tau_\text{pos}$ stays fixed and anchor trials go back to the adaptive scheduler.
- **Same-ear assumption.** Earplug attenuation and bone conduction leave the plugged ear about 30 dB down in the treble. That should be enough for loudness comparisons in the open ear, but it's worth a check: one block with the "plugged" ear actually open should look different.
- **Room.** The position spread absorbs room comb filtering along with HRTF variation. A small, reflective room may make $\tau_\text{pos}$ large enough that speakers rarely move a decision. With $\tau_\text{pos}$ fixed, the model can't see that, so the anchor-pair question matters here too. The report should show weak speaker evidence as weak, not hide it.

# Sources

- [PipeWire filter-chain documentation](https://docs.pipewire.org/devel/page_module_filter_chain.html)
- [LSP Parametric Equalizer x32 LeftRight (GStreamer LV2 listing)](https://gstreamer.freedesktop.org/documentation/lv2/lsp-plug-in-plugins-lv2-para-equalizer-x32-lr.html)
- [Equaliser for macOS (cvknage/equaliser)](https://github.com/cvknage/equaliser)
- [EasyEffects preset example with left/right bands](https://gist.github.com/ryuukk/1702e352b4de3217cad1e971cb46e181)
