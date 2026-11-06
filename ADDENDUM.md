# Flatiron addendum — profile comparison, speaker reference and seating spread

Nov 3, 2026 · @Dathan

## Summary

This addendum adds three features to Flatiron as described in DESIGN.md. The first two answer the same question: which narrow features belong to the listener, and which belong to one headphone. The third changes how every perceptual fit treats differences between seatings, and the speaker model is built on it.

- **Feature 1, profile comparison.** (a) `report --compare` plots several profiles' responses against each other, with a soft-median consensus and the regions where they disagree most. (b) `config -r consensus:…` uses a consensus of the listed profiles as the reference a profile is corrected against, so features every headphone shows on this listener's head stay.
- **Feature 2, speaker reference.** (a) A speaker profile kind with its own trial protocol: short one-ear blocks, earplug in the other ear, speaker or head moved between blocks, and positions averaged rather than rejected. (b) Speaker profiles join the consensus as a different kind of evidence. They can show that a feature shared by every headphone is a coupling artifact, but they are never trusted on their own.
- **Feature 3, seating spread.** Sessions become groups in a hierarchical model: each seating has its own curve, related to the others through a learned average and spread. This replaces pooling sessions as one curve and checking them for consistency afterwards.

The guiding idea: the listener's own ear and hearing appear in every source, headphone or speaker. A headphone's aberrations appear only in that headphone. Over-ear coupling artifacts appear in every headphone but no speaker. The design separates those three with as little trust in any single setup as possible.

Features 1 and 2b are downstream of the cached perceptual fits, so like every other setting they can be switched on and off without new trials. Feature 3 does change how existing trials are modelled. No new trials are needed, but existing profiles get refitted and their results can change (see Feature 3).

## Notation

| Name | Meaning |
| --- | --- |
| T, cup, hp\_h, spk\_s | The parts of a residual: the listener's own structure, the over-ear coupling shared by all headphones, headphone h's own aberrations, speaker s's colouration plus the room |
| w\_cup, w\_hp, w\_spk | Prior scales: how large each part is assumed to be, typically |
| τ\_hp | Between-headphone spread in the soft median: one value per ear and profile set |
| τ\_seat(f), τ\_pos(f) | How much a seating or a speaker position typically differs from the average at frequency f |
| L\_avg, u\_g, L\_g | The average curve, group g's deviation from it, and group g's curve (L\_avg + u\_g). A group is a headphone session or a speaker block |
| ℓ | Correlation width of u\_g across frequency |
| R\_h, m | Headphone h's residual, and the consensus |
| A\_p | The interpolation matrix from the common grid to profile p's own grid |
| κ | The soft median's curvature penalty (DESIGN.md's λ remains the lapse rate) |

## Feature 1a: comparison report

`flatiron report --compare A B C …` overlays two or more profiles' raw curves per ear with their consensus, and says what the veto would correct on each. It fits no filters.

### Inputs

- Each profile's cached perceptual fit, L̂ with its covariance. No new trials, and no profile's own settings are used except its sessions and pooling.
- Compare-time settings, shared by all profiles: max feature width W (`-w`, default 1 octave), reference (`-r`, default the profiles' common fixed one) and ear (`--ear L|R|both`). A consensus reference isn't used here: `iso226` stands in, with a note.
- Each fit stays on its own grid of about 100 to 110 points, and statistics are computed there. Interpolating a fit onto a finer grid adds no measurements: the new points are mixes of their neighbours, so a 399-point curve still holds only about 106 independent values. Treating those points as separate data would make the two-point rule meaningless and error bars too small.
- Shared curves such as the consensus live on a common 1/48-octave grid, the finest any fit uses, over the intersection of the measured ranges. They're compared with a profile on that profile's own grid through A\_p. The 1/96-octave grid is used only for plotting.

### What is reported (per ear)

1. **Aligned curves.** Each L̂ minus the reference, shifted by its robust offset (Feature 1b), with ±1 SD bands, and the raw consensus on top with its own. A mean-based alignment would be pulled by a treble difference in one headphone. Broad tonal differences stay visible here, and only here.
2. **Where the curves differ.** A strip shades frequencies where the aligned curves disagree as a whole (Cochran's Q, p < 0.01). It includes broad differences.
3. **Flags.** A feature is flagged when the veto (Feature 1b, in its feature mode) would correct it, so a flag always names something a veto would act on, and a broad difference can't produce one. A flag lists the profile, ear, centre, height, correction, and a reading: "peak only here", "peak elsewhere on the others", or "taller here than on the others", with the words for dips to match. They're sorted by the size of the correction.
4. **All features.** One row per place, for features that all overlap each other, with each profile's height there and what the veto would correct. A feature missing from one headphone isn't flagged on it; it shows up here as a gap, and in the others' rows.

Profiles of kind `speaker` and `iem` are drawn dashed and don't count toward the consensus or the veto by default (Feature 2 and Open questions say why). They're measured against the voters' median without counting toward it.

This replaced an earlier report of residuals and pointwise z-scores. Each profile's baseline makes its own decision about what to reject, so the residuals disagreed more than the raw curves: on HD560, FT1 and Sundara, RMS spread was 2.97 dB in the residuals against 2.66 dB raw in the left ear, and in the left-ear mids a 4 to 5 dB raw gap became 6 to 10 dB.

### Soft median

The consensus m(f) minimises a robust misfit across profiles plus a smoothness penalty across frequency:

```latex
m = \arg\min_m \sum_p \sum_{i} w_{p,i}\, \rho_\delta\!\left(\frac{L_{p,i} - o_p - (A_p m)_i}{\mathrm{scale}_{p,i}}\right) + \kappa \lVert D_2\, m \rVert^2
```

- o\_p is profile p's constant offset, fitted with m by the same loss, summing to zero.
- i runs over profile p's own grid points. w\_{p,i} is the octave width each point covers, so a refined region doesn't count for more just because it has more points.
- ρ\_δ is the pseudo-Huber loss with δ = 1: close to a precision-weighted mean when profiles agree within their error bars, and close to a median when one is far off. That is the "soft" part.
- scale\_{p,i}² = SD\_{p,i}² + τ\_hp². Measurement noise alone would overweight the most-measured profile.
- τ\_hp is one value per ear and profile set: the median across frequency of the per-frequency DerSimonian–Laird estimates, floored at 0.5 dB. A separate estimate at each frequency would let a headphone's own peak inflate the tolerance at that peak, and the soft median would then act like a mean exactly where it most needs to be robust.
- κ uses the same curvature penalty as the perceptual fit's prior, so m is no smoother than the data it comes from.
- Solved by iteratively reweighted least squares.
- m's covariance carries each profile's full covariance through the final weights. With W\_p = diag(w\_p / scale\_p²) and H the final system matrix:

```latex
\operatorname{Cov}(m) = H^{-1}\Big(\sum_p A_p^{\mathsf T} W_p \operatorname{Cov}(L_p)\, W_p A_p\Big) H^{-1}
```

  Per-frequency weights alone ignore the correlation between neighbouring points and make the SD too small (see Prototype results). This treats the final weights as fixed, which the simulator's coverage check tests.

With only two profiles, the consensus is their precision-weighted mean and the report says so. The flags then show differences, not outliers.

## Feature 1b: consensus reference and veto

These are two separate settings, usable alone or together, with narrow or broad detection. Both exist to avoid correcting what's shared across headphones, so they aren't independent in effect: an unsmoothed consensus reference leaves a veto little to do. Smoothed, the reference carries the broad shape and the veto decides the narrow features.

### `-r consensus:A,B,C`: a reference built from raw curves

The consensus is a drop-in for E (DESIGN.md pipeline step 1). The steps that follow are unchanged, including the baseline.

1. **Raw curves.** The sources are the listed profiles' L̂, with no reference and no baseline. So a consensus depends only on its members' fits, and every profile using the same list shares one cache entry.
2. **Members.** Exactly the profiles listed. The profile being corrected is included only if it's listed. Leaving it out makes the reference independent of it, but with two others the soft median falls back to their weighted mean, so one's peak reaches the reference at about half height. Listing it as well gives a real median of three.
3. **Alignment.** Each source gets a constant offset, fitted together with the soft median by the same robust loss and summing to zero, so a treble difference in one headphone doesn't shift its whole curve. A mean-based alignment would be pulled by exactly those differences.
4. **Smoothing.** `--consensus-smooth` (FWHM, default 1/2 octave, 0: none) smooths m with a local linear fit. Unsmoothed, the reference holds the members' narrow features, so a shared peak disappears before detection and another headphone's peak reads as a dip here. Smoothed, the reference carries the broad shape and narrow features go to detection, and to the veto if it's on. About half of a shared peak around 1/4 octave wide stays in the reference at this width, so a feature's height against it is lower than against a fixed reference.
5. **Covariance.** Cov(L − m) adds m's error, and for a member subtracts the correlation its own vote creates. The offsets are treated as fixed.

A single profile is allowed (the reference is then that headphone's curve), with a note. Profiles that share trials through pooling can't be used together: `--pool` means the same headphones measured again, and a consensus needs different ones. Results live in `cache/consensus/`, keyed by each member's perceptual-fit hash and pruned least-recently-used at 200 MB. The smoothing is applied after the cache.

### `--veto A,B,C`: correcting toward the median feature

Each feature is corrected toward the median feature at the same place across headphones, rather than toward zero. Detection is unchanged, on this profile and each voter, against this profile's reference. Requires `--method features`. It applies to whatever would be corrected: narrow features, those inside broad regions, and with `--broad` broad ones too.

1. **Voters.** The profiles listed plus this one, always.
2. **Matching.** Each voter contributes its tallest same-sign feature that overlaps this one, in the literal sense that their half-height widths intersect. A voter with no overlapping feature counts as having nothing there. A voter's narrow lobes of 1 dB or more count even if they aren't significant, since an undetected lobe isn't the same as nothing. There's no matching parameter. Peaks that are clear of each other, however close, are treated as separate features.
3. **The median feature.** The most central of the candidates: the actual feature (or nothing) with the smallest total shape difference to the rest, each drawn as a Gaussian of its height and half-height width. Ties go to this profile. With one other profile there's no middle, so it's their average.
4. **Correction.** With R the feature's residual and b the median feature, C = −(R − b), with any net boost in the feature's own direction limited. The limit is `--veto-import` (default 1.5 dB) if the median feature is more than 1/12 octave away, and 0 otherwise, so at the same place a feature can only shrink. Outside the feature only an import can add anything. A limit of 0 means nothing is ever imported.

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

`--veto-mode pointwise` is the alternative. At each frequency, what exceeds the soft median of the voters' residuals, taken with this profile's and each with its own baseline, is corrected, and never past zero: C = −sign(R) max(0, |R| − max(0, sign(R) m)). Features are found on R − m. It can't import or move anything, and it's sensitive to broad differences between headphones because of the separate baselines.

### `--method match`

No baseline: the whole of L − E, within the correction range and with only its level free (a precision-weighted median), is the target, tonality included. There's no feature test, the limits are the usual cut cap and the broad boost cap, and no tonality compensation is added. It's for experiments, mostly with a consensus reference. It doesn't combine with `--veto`, and with `--method smooth` neither does `--veto`; a reference still applies to both.

## Feature 2a: speaker sessions

A speaker profile is measured in short one-ear blocks, each at a different position. Feature 3's hierarchical model averages the positions into one direction-averaged curve per ear instead of rejecting what varies.

### Setup

- `flatiron profiles new NAME --kind speaker`, with the speaker's label and which output channel drives it (`--channel L|R`). Only one speaker plays. Two speakers playing the same burst would comb-filter at the ear.
- Distance at least 0.7 m where the level allows (a phone at arm's length is acceptable but logged as near field). The level screen runs as usual. It warns if the set level is unusually high, since speakers need more drive than headphones and the room is open.
- A quiet room. The level screen adds a one-off check: the listener confirms they can't hear the burst with the speaker muted, which catches hum or fan noise near the stimulus level.

### Blocks

- One block = one session file = one open ear and one position. The default is 24 answered trials, about a minute (`--block 16–60`).
- Before a block, the screen names the open ear and asks for a foam earplug in the other ear, fully inserted. Ears alternate every block: a block on L, then on R at a new position, and so on.
- Position plan: the screen suggests the next position from a rotation meant to spread directions around the open ear. Front, 30° and 60° toward the open side, side-on, rear quarter (turn the chair), plus one higher and one lower placement. The listener can accept it or type their own label. Optional numbers (azimuth, elevation, distance) are stored when given.
- Prefer turning the head or chair over moving the speaker. The speaker then always faces the listener, so its own on-axis colouration stays constant across positions instead of adding variance.
- Suggested minimum: at least 8 positions and about 400 to 500 trials per ear in total, such as 20 blocks of 24 or 8 of 60. About 200 trials per ear was too few in simulation (see Prototype results). The report shows how much more trials or positions would tighten the curve.
- Anchor pairs: about a quarter of each block goes to a small fixed set of frequency pairs repeated across blocks, so that differences between positions can be observed directly (see Open questions).
- No seating step (Feature 3). Positions are supposed to differ, and that variation is modelled instead.

### Trials

- Same stimuli, Δ, rove, catch trials, undo and reaction-time logging as headphone trials. Bursts go to the speaker's channel. The trial's ear is the open ear.
- Adaptive selection runs on the pooled speaker model (below), not per block, so a block's trials are spent where the average curve is least certain.
- The channel check is replaced by a plug prompt. A plugged ear can't be verified by listening, so it's a confirmation only.

### Model: positions as random effects

This is Feature 3's model with blocks as the groups. Per ear, the perceived level at block g is L\_avg(f) + u\_g(f):

- L\_avg is the quantity of interest: the average over the directions the position plan is meant to cover, with each position weighted equally. It has the same priors as a headphone L.
- u\_g is block g's deviation, with amplitude τ\_pos(f) and correlation width ℓ = 1/12 octave.
- With 24 trials a block can't pin down its own u\_g. The model doesn't need it to: L\_avg's uncertainty includes τ\_pos² divided by the number of positions. That's what "averaged, not rejected" means in practice.
- τ\_pos is fixed rather than estimated at this block size, by default at 1 dB below 5 kHz rising to 4 dB at 12 kHz. Estimating it alongside L\_avg from 24-trial blocks doesn't work: in simulation it ran up to many times the true value, and a prior would only hide that. The fixed curve is an assumption, so the report labels it as one. Estimation (with a log-normal prior, log SD 0.3, around that curve) is enabled only if the simulator shows that anchor pairs make it reliable.
- The result is a perceptual fit like any other (L̂ with covariance), so `report`, `report --compare` and the pipeline can treat it as a profile. Its kind marks it as speaker evidence.

The SD covers the sampling of positions and the judgment noise. It doesn't cover bias from the plan itself: the rotation isn't a random draw of directions, so if it misses a region, nothing in the error bars shows that.

Averaging in dB fills in a moving notch less than a true diffuse-field (power) average would. Open questions covers whether that's worth correcting.

## Feature 2b: folding speaker data into the baseline

Speakers and headphones go into one joint model. Speakers are evidence about one thing only: whether a feature every headphone shares is really the listener's. They can demote a shared feature to a coupling artifact. They never create a correction target by themselves.

&#91;embedded content: how headphone and speaker residuals combine · per ear\]

Every source's residual (narrower than W) carries the listener's structure T. Speakers are the only ones without cup, so that's the one place they change the outcome.

### Why speakers need different treatment

Every residual (narrower than W) is a sum of parts. Hearing sensitivity and the ear canal are in all of them and fold into T.

| Source | What its residual contains |
| --- | --- |
| Headphone h | T + cup + hp\_h + noise |
| Speaker s | T + spk\_s + noise (noise includes position spread) |

- **T**: the listener's own structure. Direction-averaged pinna and concha response, canal and hearing. This is what a correct headphone should reproduce.
- **cup**: over-ear coupling structure common to all the headphones (how a cup loads this pinna). Invisible to a headphone-only consensus.
- **hp\_h**: headphone h's own aberrations. Sparse: most frequencies none, a few real peaks and dips.
- **spk\_s**: speaker s's colouration plus the room. Unknown, and for cheap speakers possibly large.

A headphone-only consensus estimates T + cup. Speakers are the only data without cup, but they carry their own spk\_s. So speaker data is useful exactly where it's trusted to say "this shared headphone feature isn't on the listener's side."

### The joint model

Per ear, estimate T, cup, every hp\_h and every spk\_s together by MAP:

- All these parts live on the common 1/48-octave grid. Each source is compared with them on its own grid through A\_p, with its residual covariance as its noise. On a source's own grid that covariance is full rank except for the level and slope the baseline removes, which a pseudo-inverse excludes. On a finer grid it couldn't be used at all: most patterns would have no modelled noise, and so infinite weight.
- hp\_h and spk\_s get heavy-tailed priors (Student-t, 3 degrees of freedom, scales w\_hp and w\_spk), so a few large features are cheap and broad noise isn't. With several headphones, this is the soft median of 1a in a different form.
- cup gets a Gaussian prior with a small scale w\_cup (default 1.5 dB). Without strong evidence, cup stays near zero and shared headphone features are attributed to T and kept.
- T gets only the smoothness prior.
- Prior terms are weighted by the octave width each grid point covers, like the perceptual fit's curvature penalty, so their strength doesn't depend on grid spacing.
- Solved by IRLS for the Student-t terms; covariances from the Laplace approximation at the solution.

(Written before 1b was split into a reference and a veto, and to be revisited when speakers are built.) The correction for headphone h is the pointwise rule from `--veto-mode pointwise`, with T̂ as the median: what h shows beyond T̂ is cup + hp\_h, and that's what gets removed.

### How the evidence plays out

| Headphones | Speakers | Result |
| --- | --- | --- |
| One shows a peak | Any | Peak attributed to hp\_h: removed from that headphone |
| All show a peak | Both show it | T: kept everywhere |
| All show a peak | Both confidently lack it | cup: removed from every headphone |
| All show a peak | One shows it, one lacks it | Mostly T (the t-prior treats the odd speaker as spk\_s): kept |
| All show a peak | Too noisy to say | cup stays near zero: kept |
| None show a peak | Both show it | T has a peak the headphones lack: not imported under remove-only |

### Trust and what speakers never decide

- `--speaker-trust low|medium|high` scales w\_spk (6, 3 and 1.5 dB). Low means speakers almost never overturn the headphone consensus.
- Speakers inform only structure narrower than W. The broad shape of a speaker in a room isn't trusted at all unless its measured response is supplied (`--speaker-response FILE.csv`, subtracted before detrending). Even then it stays behind `--broad`.
- Two unrelated speakers count for much more than one. A colouration shared by a phone and a SoundSticks satellite is unlikely, so features they agree on are attributed to T. With a single speaker, the report says that shared-feature removals rest on one device.
- IEMs don't count toward T. They bypass the pinna and concha, so a peak they lack says nothing about whether it's the listener's. They're shown in comparisons as a diagnostic: a feature that persists at two insertion depths is in hearing, not in the ear's acoustics.

## Feature 3: seating spread

Today all of an ear's sessions are fitted as one curve, then each feature is checked for consistency afterwards (DESIGN.md, Perceptual model and pipeline step 5). That treats a reseat as if it were judgment noise, but the two behave differently. Judgment noise is new on every trial. Everything in a session shares one seating, so more trials in a session say more about that seating and little about seatings in general. The check also can't run with one session, and it switches abruptly at p < 0.01.

### The model

Per ear and for every profile kind, a trial in group g (a headphone session or a speaker block) depends on L\_g = L\_avg + u\_g:

- u\_g is a Gaussian deviation with amplitude τ(f), τ\_seat for headphones and τ\_pos for speakers. It's centred on zero across frequency, because a constant offset cancels within a group.
- ℓ is fixed at 1/12 octave. On the real data the evidence shows no clear preference for a value.
- The time-order bias and noise parameters (σ₀, d₀, λ) are shared across an ear's groups. σ₀ can fall compared with today's fit, where it also absorbs seating differences.
- The term also picks up anything else that changes between sessions: the noise token, the listener's state, or the stimulus when profiles are pooled.

The partial pooling follows from this. A group with few trials is pulled strongly toward the average, and a well-measured one can stand further from it. L\_avg's SD includes τ² divided by the number of groups, so it stops shrinking in proportion to trials once a seating is well known.

### τ\_seat

- τ\_seat has two values, one below 5 kHz and one above 8 kHz, log-interpolated between them. Each is per ear and per profile. On the real data, treble spread is clear for one ear and profile, suggestive for another, and absent for the other two.
- Log-normal priors keep the estimate away from zero, with medians 0.5 and 1.5 dB and log SD 0.7. These are judgment calls, to be tuned in the simulator.
- With one group, or fewer than two with at least 60 trials in the ear, τ\_seat stays at the prior. A single session's error bars therefore allow for seatings not yet measured, instead of tightening without limit as trials are added.
- About three sessions of 110 trials per ear can detect a spread of 2 dB or more, but can't resolve one under about 1 dB. The prior decides that range, and it matters little there: τ of 1 dB raised the median SD by 2 to 12% at these trial counts.

### What replaces the consistency check

For each kept feature, its level is taken in each group's curve (L\_avg + u\_g, posterior mean) over the half-height core. The feature is corrected to the median group's level, weighted by precision, as a share of its level in L\_avg, and dropped if that's under 20%. This is pipeline step 5's rule without the Cochran's Q gate, so there's no abrupt switch at a p-value. `--consistency` / `--no-consistency` turns it on and off. A Gaussian term may shrink an on-or-off resonance toward the average, which criterion 11 tests.

The report separates two numbers for each feature: the SD of its average level, which decides whether it's real, and the spread between seatings, which says how far off the correction will be on a typical seating.

### Scheduler

- After each answer, the full model is refitted with τ and the noise fixed. Sessions connect only through L\_avg, so the solve scales linearly with the number of sessions (a block-arrow matrix, eliminating each u\_g in turn). A prototype was faster than today's refit (see Prototype results).
- Δ aims at the current group's curve, L\_avg + u\_g, because that's what the listener is hearing.
- Pair scoring targets L\_avg: how much one answer would shrink the variance of the L\_avg difference, through its covariance with the difference in L\_g. The score falls off once the current seating is well known at a pair.
- At each 50-trial block, the noise is re-estimated with τ fixed. τ is re-estimated at the end of the session and cached, because fitting everything together would take an estimated 4 to 35 s, against 2 to 11 s for today's noise fit.
- Curvature for grid refinement comes from L\_avg.

### Effect on existing profiles

- The model version is bumped, so all perceptual fits are recomputed on next use from the stored trials.
- Results can change. Features near the threshold are the most affected. With sundara L and τ fixed at 0.5, 1 and 1.5 dB, the prototype keeps 5, 4 and 3 of the current fit's 6 features (with no seating step applied), losing the smallest ones first.
- `--sessions`, `--pool`, checkpoints and verify sessions keep working: a selected, pooled or verify session is simply one more group.

## CLI and data model changes

Features 1 and 2 are additive. Existing profiles default to kind `headphone`. Feature 3 changes the fit of every profile, as above.

### Commands and options

| Command | New option | Does |
| --- | --- | --- |
| `profiles new` | `--kind headphone\|iem\|speaker`, `--channel L\|R` (speaker) | Sets the profile kind; `profiles` lists it |
| `profiles set` | `--kind`, `--label` | Changes kind after the fact (no trials affected) |
| `run` | `--block N` (speaker only, default 24), `--position LABEL` | Runs one speaker block; refuses `--sweep` on speakers |
| `report` | `--compare P…`, `-w`, `-r`, `--ear`, `--include-kind` | Comparison report (1a) |
| `report` | `--csv FILE` | Dumps L̂, reference, baseline and residual with SDs per ear, for any profile |
| `config` | `-r consensus:P,P,…`, `--consensus-smooth OCT` | Consensus reference (1b) |
| `config` | `--veto P,P,…\|none`, `--veto-mode feature\|pointwise`, `--veto-import DB` | Correct toward the median feature, or by the pointwise rule (1b) |
| `config` | `--method match` | Correct the whole curve toward the reference (1b) |
| `config` | `--speaker-trust low\|medium\|high`, `--speaker-response FILE` | Speaker handling (2b) |
| `simulate` | `--scenario consensus`, `--scenario seating` | The multi-profile and seating simulations (Validation) |

### On disk

- `profile.json`: `kind`, and for speakers `speaker` (label) and `channel`. Settings gain `reference: {type: consensus, profiles: [...], mode, speaker_trust, speaker_response}`.
- Speaker session files: `open_ear`, `position` (`label`, optional `azimuth`, `elevation`, `distance_m`), `speaker_channel`, and the plug confirmation. Trials are unchanged.
- Cache: all perceptual fits get a model-version bump for the group random effects, and store τ, each group's u\_g and the groups used. Consensus results go in the shared `cache/consensus/` (Feature 1b), never under a profile.

### Modules

- `model/`: `hierarchical.py` (group random effects and the block-arrow solve, used for every fit), plus a shared helper to map a common-grid curve onto a fit's own grid.
- `fit/`: `consensus.py` (soft median, raw consensus, the joint T/cup/hp/spk model), `compare.py` (the comparison's flags and rows), `veto.py` (the median feature and the pointwise rule), a `consensus` reference type in `reference.py`, and the group-based seating step in place of Cochran's Q in `features.py`.
- `session/`: a speaker block runner (plug prompt, position plan, anchor pairs, ear alternation), and group-aware Δ and pair scoring in the scheduler.
- `report/`: the comparison figures and table, the consensus columns in the single-profile report, and the two per-feature seating numbers.
- `sim/`: multi-profile ground truth, the speaker position model, and seatings that change feature levels.

## Validation with the simulator

The simulator gains a multi-source scenario and a seating scenario. The rule for passing the multi-source scenario: the listener's own features are never corrected, and only headphone-specific ones are.

### Ground truth per ear

| Component | Contents |
| --- | --- |
| T (listener) | +6 dB at 7 kHz (Q 8), +5 dB at 11 kHz (Q 10), −4 dB at 8.4 kHz (Q 12) |
| cup (all three headphones) | +4 dB at 9.5 kHz (Q 6) |
| hp, headphone 1 | +5 dB at 5.8 kHz (Q 5) |
| hp, headphone 2 | −4 dB at 8.0 kHz (Q 10) |
| hp, headphone 3 | +3 dB at 10.5 kHz (Q 4), +3 dB at 3.2 kHz (Q 3) |
| spk, speaker 1 | Random narrow ripple, 3 dB RMS above 3 kHz, plus a +6 dB peak at 6.3 kHz (Q 6) |
| spk, speaker 2 | Independent random ripple, 3 dB RMS |
| Positions | u\_g with τ\_pos 1 dB below 5 kHz rising to 4 dB at 12 kHz; 20 positions per ear, 24 trials each |
| Seatings | Today `reseat()` only shifts feature frequencies. It also gains u\_g with a set τ\_seat, and resonances present in only some seatings |

Each headphone gets its own smooth tilt, and every source gets the ISO 226 contour. Headphones get 600 trials per ear, as in the existing validation.

Speaker 1's own peak at 6.3 kHz checks that one speaker's colouration never reaches a headphone correction. A variant moves that peak to 9.5 kHz, on top of cup, to check the split-speakers case: one speaker shows a shared headphone feature and the other doesn't.

### Criteria

1. **Comparison report.** Every hp feature is flagged against the right headphone within 1/24 octave. No T or cup feature is flagged as one headphone standing out. False flags stay at the chosen target rate.
2. **Headphones-only consensus.** Every hp feature is corrected within 1.5 dB. T and cup are left alone (cup is invisible without speakers, as designed). No false correction above 1.5 dB.
3. **Both speakers, medium trust.** As in 2, and cup is also removed from all three headphones within 2 dB. T and speaker 1's 6.3 kHz peak leave no correction.
4. **Split-speakers variant.** With speaker 1's peak moved onto cup, cup is kept, matching the "one shows it, one lacks it" row.
5. **One speaker.** With speaker 2 only, any removal of cup is marked in the report as resting on one device.
6. **Low trust.** Results match criterion 2 within 0.5 dB.
7. **Speaker model.** L\_avg recovers the mean over positions within 1.5 dB RMS from 1 to 13 kHz, at both 20 × 24 and 8 × 60 trials. Its reported SD covers the error about 68% of the time over 50 simulated listeners. The criterion also runs with the fixed τ\_pos at half the true curve, to show how much a wrong assumption costs.
8. **Rule and edge cases.** Two-profile consensus runs and warns, and every row of the veto table comes out as specified (unit tests on the rule alone). Three-headphone cases come out near the worked values in 1b.
9. **Seating spread.** With three sessions of about 110 trials per ear, a true τ\_seat of 2 or 3 dB is recovered within a factor of 1.5, and a true τ\_seat of zero isn't estimated above 1 dB.
10. **Coverage.** L\_avg's reported SD covers the error about 68% of the time at 1, 3 and 8 sessions, and m's SD does the same for the consensus.
11. **Moving resonances.** A 9 dB resonance in two seatings of three is corrected within 2 dB of its median level. A resonance in one seating of three is dropped. With one session, a seating-specific resonance of up to about twice the prior's spread isn't corrected as confident.
12. **Speed.** A per-answer refit takes under 100 ms at 8 sessions of 150 trials on a fully refined 199-point grid, and a block-end pause is no longer than today's.

## Prototype results

These come from scratch code run on the `sundara` and `sundaratone` profiles (three or four sessions per ear, 31 to 120 trials each) and on simulated listeners. They guide the design above and aren't a substitute for the criteria. The prototype held noise fixed in its refits and used a looser convergence tolerance than the current fit, and its simulated speaker pairs were random rather than adaptive.

**Refit after one new answer:**

| Case | Current | Hierarchical |
| --- | --- | --- |
| Real data: 3 sessions, 108 points | 25 ms | 6 ms |
| 8 sessions × 150 trials, 100 points | 28 ms | 14 ms |
| 8 sessions × 150 trials, 199 points | 151 ms | 33 ms |

**Seating spread on real data:**

- A constant spread of 2 dB or more across frequency is clearly rejected. Spreads under about 1 dB can't be told apart from none.
- Spreads of 0.5 to 1.5 dB change the median SD by −1% to +18%. Today's SDs are already 1.2 to 2.0 dB at these trial counts, partly because σ₀ absorbs some of the seating differences.
- Treble spread, tested against simulations with no spread: sundaratone R is clear (none of 30 matched it), sundara L suggestive (10% matched), and the other two show none.

**Speaker model, with τ\_pos known:**

| Positions × trials | RMS error, 1–13 kHz | Median SD | SD, 8–13 kHz | 68% coverage |
| --- | --- | --- | --- | --- |
| 8 × 24 | 2.14 dB | 1.93 dB | 2.80 dB | 0.70 |
| 16 × 24 | 1.47 dB | 1.24 dB | 1.88 dB | 0.66 |
| 8 × 60 | 1.22 dB | 1.15 dB | 1.70 dB | 0.71 |

- At 8 × 24, fixing τ\_pos at half the true curve only lowers coverage to 0.64.
- Estimating τ\_pos at 8 × 24 with L\_avg also free: a true 4 dB gives a median at the top of the tested range (14 dB), and a true 1 dB gives a median of 5 dB, ranging from 1 to 14. With L\_avg held at the truth, the Laplace approximation agrees with brute-force integration and centres near the true value, so the approximation isn't the cause. At 8 × 60 the median is 5 to 6 dB for a true 4 dB.

**Grid, on sundara L** (108 points, 399 on a 1/96-octave grid; pure noise, k = 2):

| Flagging rule | False flags per ear |
| --- | --- |
| Own grid, 2 adjacent points | 1.04 |
| Own grid, 1 point | 2.67 |
| 1/96 grid, 2 adjacent points | 2.61 |
| 1/96 grid, width ≥ 1/24 octave | 1.51 |
| 1/96 grid, width ≥ 1/12 octave | 0.57 |

- The covariance has rank 106 on both grids. Neighbour correlation rises from 0.72 to 0.98 on the finer one.
- Consensus SD, measurement noise only, compared with Monte Carlo: per-frequency weights give 0.19 dB against 0.43 dB with τ\_hp = 0, and 0.28 dB against 0.42 dB with τ\_hp = 1 dB. The covariance formula in 1a matches Monte Carlo in both. This test ran on 1/96-octave curves, without robust reweighting.

## Open questions and build order

Build the headphone-only comparison first. It's useful immediately with the three over-ears, and everything after it reuses its pieces. It works from any fit's L̂ and covariance, so it doesn't have to wait for Feature 3. Feature 3 comes before the speaker work, which is built on it.

### Build order

1. `report --csv`, and the shared helper that maps a common-grid curve onto a fit's own grid.
2. `report --compare`: aligned raw curves, raw consensus, veto flags and feature table, heterogeneity strip.
3. `config -r consensus:…` and `--veto` for headphones, report columns, simulator criteria 1, 2 and 8.
4. Feature 3: the hierarchical fit, scheduler changes, the group-based seating step, simulator seatings and criteria 9 to 12. DESIGN.md is updated once this is built.
5. Speaker profile kind and block runner: plug prompt, position plan, anchor pairs, ear alternation.
6. The speaker position model, simulator criterion 7.
7. The joint T/cup/hp/spk model with speaker trust, simulator criteria 3 to 6.

### Open questions

- **dB vs power averaging over positions.** A diffuse field averages power, which fills in moving notches more than averaging perceived dB does. A power average of per-position posteriors is possible, but each position is barely measured, so it'd be noisy. Proposed: dB averaging, with the expected bias at notch frequencies estimated in the simulator before deciding.
- **Default w\_cup, and whether cup's prior should be Gaussian.** 1.5 dB keeps shared features unless both speakers clearly lack them. Too small and real coupling artifacts are never caught; too large and noisy speakers start removing the listener's own features. A rough hand calculation (one frequency, no noise, ignoring T's smoothness prior and hp\_h) suggests the bigger problem is the shape of the prior. With both speakers lacking a 4 dB shared peak at medium trust, the best split is about T = 2.6 and cup = 1.4 dB, which would miss criterion 3. At 8 dB, cup only reaches about 1.7 dB, because the Gaussian penalty keeps growing while the speakers' Student-t penalty levels off. Check this in a simulation before building the joint model, and try a heavier-tailed prior on cup. Then tune it, using the real data with `report --compare` as a sanity check.
- **Why τ\_pos runs away, and whether anchor pairs fix it.** The likely cause is that 24-trial blocks rarely compare the same frequencies, so differences between blocks can't be seen, but this isn't proven. Anchor pairs are untested. If they don't work, τ\_pos stays fixed and anchor trials go back to the adaptive scheduler.
- **Seating priors and shape.** The τ\_seat prior medians and the two-value shape need tuning on more data. If a Gaussian u\_g handles on-or-off resonances badly, even with the median rule, a heavy-tailed u\_g is the next thing to try.
- **Minimum widths.** 1/12 octave may miss the narrowest features at k = 2, so k and the width should be chosen together. The standard narrow test has a related weakness: "two adjacent points" means 1/24 octave on most of a fit's grid and 1/48 where it's refined. Should it move to a width in octaves too? That would change existing corrections, so it isn't part of this addendum.
- **τ\_hp floor.** 0.5 dB is a judgment call, so the most-measured headphone can't dominate where the profiles happen to agree. Tune it in the simulator.
- **IEMs in the consensus.** Excluded by default (they lack the pinna and concha). Should they be allowed as hearing-only evidence, separating hearing from ear acoustics? Not needed for corrections, so deferred.
- **Same-ear assumption.** Earplug attenuation and bone conduction leave the plugged ear about 30 dB down in the treble. That should be enough for loudness comparisons in the open ear, but worth a check: one block with the "plugged" ear actually open should look different.
- **Room.** The position spread absorbs room comb filtering along with HRTF variation. A small, reflective room may make τ\_pos large enough that speakers rarely move a decision. With τ\_pos fixed, the model can't see that, so the anchor-pair question matters here too. The report should show weak speaker evidence as weak, not hide it.
