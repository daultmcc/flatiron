# Flatiron

Flatiron is software designed to estimate the upper-mid and treble frequency response
of headphones and generate corrective EQ profiles, with a specific aim of smoothing
out narrow treble resonances. This is a subset of what other auto-EQ software claims to
do; however, most such existing software is based on measurements of headphones on
standardized test rigs, whereas both the real frequency response you experience from
any given headphone and the ideal target curve will depend on your specific anatomy,
meaning the results are often lackluster, especially in the treble. In contrast, Flatiron
estimates the behavior of your exact headphones on your exact head through a series of
perceived loudness trials.

Of course, this approach has its own drawbacks. See the [Theory](#theory) section or
[the full design doc](DESIGN.md) for more information.

## Installation

Flatiron can be installed system-wide by cloning this repository and running one of
the following commands from the directory root.
```sh
uv tool install -e .
# or
pip install .
```
Alternatively, it can be invoked locally without installation as `uv run flatiron`.

## Usage

Some common commands and options are described below.
For full usage instructions, use the `--help/-h` flag or see [the design doc](DESIGN.md).

### Quick start

```sh
# create a new profile for a pair of headphones
flatiron profiles new HD600

# perform the listening trials
flatiron run HD600

# view a report of the measured response
flatiron report HD600

# generate a corrective EQ preset
flatiron export HD600 --target easyeffects -o HD600.json
```

### Profiles and listeners

Each listening device (headphone or speaker) has its own _profile_, which is associated
to a single _listener_. Profiles are specified with the syntax `<LISTENER>/<PROFILE>`, but
if only a single listener exists then the listener can be
omitted, as in the examples above. By default there is a single listener called `default`.
A listener should correspond to a single individual who is performing the tests; certain
fitted parameters of the perceptual model are shared across profiles for the same listener.

Profiles are managed with `flatiron profiles` and listeners with `flatiron listeners`.
Running either command bare prints a list, and each has `new`, `rename`, and `delete` subcommands.

### Measurement

`flatiron run <PROFILE>` begins a measurement run for the specified profile. This consists
of a number of _sessions_, which last 12 minutes by default, each of which in turn contains a
number of _trials_. During each trial, two tones will be played at different frequencies, and
you will be asked to indicate the louder of the two with arrow keys (or by clicking A or B). Spacebar will replay the tones.
Between sessions you'll be instructed to reseat your headphones to identify placement-dependent
features of the frequency response.

One profile can have any number of sessions and you can quit an active session at any time.
Previous sessions can be listed (and deleted) using the `flatiron sessions` command.

Three stimulus types are available: white noise (`noise`), a warble tone (`warble`), and a pure sine
wave (`tone`), which can be selected with the `--stimulus` flag of `run`. The default is 12th-octave white noise
(`noise:12`).

If the `--sweep` flag is present, before the sessions a tone sweep will be played. You can mark
regions where you hear a rise or fall in the volume and this will be used to narrow in on
frequency regions of interest during the trials.

As it learns how loud each frequency sounds to you, Flatiron adjusts the volume of each pair of
tones so that all trials sound about equally loud, with the level you choose at the start of each
session as the average. Quiet regions are raised by at most 12 dB (`--max-match-boost`), and
`--no-match` turns this off. If the highest (or lowest) frequencies are hard for you to hear,
narrow the range trials are drawn from for all your profiles with
`flatiron config <LISTENER> --default-max-measured 12k` (or `--default-min-measured`), or for one
profile with `flatiron config <PROFILE> --max-measured 12k`. The range that gets corrected follows it.

300-400 trials is enough for a very basic measurement and 1000 should give reasonably accurate
results. That may sound like a lot, but even 1000 trials should take less than an hour. If you find
it taking longer than that you may be overthinking your answers. The fit accounts for a degree of
error so it's better just to answer quickly.

### Reports

`flatiron report <PROFILE>` opens an HTML page showing the measured frequency response, planned
corrections, and more. `flatiron compare <PROFILE1> <PROFILE2> ...` shows the responses for multiple
profiles overlaid. The `--no-html/-H` flag on either command suppresses the detailed report
and prints only the simplified summary to the command line.

### Configuration

There are many options that can be configured to control the statistical model and correction
scheme used. These can be set using `flatiron config <PROFILE> --<OPTION1> <VALUE1> --<OPTION2> <VALUE2> ...`.
`--reset <OPTION1>,<OPTION2>,...` resets the specified options to their default values, and
`--reset all` resets all options. `flatiron config -h` prints a full list of options.

### Exporting presets

`flatiron export <PROFILE> -t <PRESET FORMAT> -o <FILE>` generates a corrective EQ preset for
the specified profile. The currently supported formats are `apo` (for Equalizer APO and AutoEQ),
`easyeffects` (for EasyEffects and Equaliser), `lsp` (for the LSP Parametric Equalizer plugin),
`pipewire` (a configuration file for a filter chain that can be dropped into `pipewire.conf.d`),
and `tincan` (for [TinCan](https://github.com/daultmcc/tincan)).

### Validation

There are a few commands that let you verify the accuracy of a current fitted response.
`flatiron tone` is a tone generator that lets you manually sweep the frequency (in either
or both ears, with any stimulus type) to look for resonances. This can be useful in its own
right for manual EQ, but if you provide a profile you can also toggle the corrections on and
off to see the effect they have. `flatiron verify` runs trials similar to `run` but with the
correction applied, and `flatiron audition` lets you toggle the corrections while playing a
provided audio clip.

### Speakers

The same loudness judgement trial methodology can be used with speakers, to further refine a
personalized target curve. A speaker preset can be created with `flatiron profiles new --kind
speaker --channel <L|R>`. The behavior of `run` for speaker profiles is slightly different,
with shorter sessions ("blocks") that ask you to plug a specific ear with an ear plug and change the position
of your head between each.

## Theory

What is a "neutral" sound signature? For speakers the answer is (relatively) simple: a flat
frequency response. Headphones are more complicated, however, because sound arriving at your
ear from headphone drivers less than an inch away is not the same as sound arriving from an object
in your environment. To sound truly natural and "neutral", a headphone's frequency response
needs to take these differences into account.

This idea can be stated in terms of head-related transfer functions (HRTFs) and headphone-to-ear
transfer functions (HpTFs). Your HRTF is the frequency-dependent volume modulation experienced
by sound arriving at your eardrum from a distant source (due to the geometry of your pinna and ear canal), and an
HpTF is the same thing for sound arriving from the driver of a specific headphone (due to the
geometry of your ear _and_ the geometry of the headphone, which can interact to produce
unnatural resonances). A headphone will then sound neutral _to you_ when the intrinsic frequency
response of its driver _plus_ its HpTF on your head (the combination of which we have been referring to
as simply "the frequency response of the headphone") matches your HRTF. Since, as repeatedly stressed
thus far, both HRTFs and HpTFs vary from person to person, this makes for a hard problem for
audio engineers! It's impossible to design a headphone which achieves this goal for everyone, so
some individualized EQ will generally be required. Generating such individualized presets is the
goal of Flatiron.

The correct way to do this would be as follows. Put very accurate microphones in both of your ear canals
and sit in an anechoic chamber. Have a very accurate speaker play a tone sweep from a bunch of different
angles at a fixed distance from your head and record the result. That gives you your HRTF. Then put
on your desired headphones and play another tone sweep. That gives you the frequency response of
those headphones on your head. Your desired EQ preset is the difference. Simple!

Alas, as the equipment necessary for this procedure is not available to most people, this author
included, we are forced to resort to other means. The idea is that, instead of measuring the transfer
functions directly with a microphone and tone sweep, we can approximate them on the basis of perceived
loudness. Flatiron plays you hundreds of pairs of tones at different frequencies and volumes and asks you
to choose the louder one. It then performs an empirical Bayesian fit, allowing for both a degree of random
noise in your answers and certain confounding factors like a time-order bias, to determine the underlying perceived
loudness as a function of frequency, which is a stand-in for the physical frequency response of the listening
device. In principle, doing this with both a pair of headphones and a speaker will approximate the result
of the procedure described above.

Unfortunately, in practice it's not quite that simple. For one, since the trials only give information
about the relative loudness at nearby frequencies, small errors can accumulate, making the large-scale
tilt of the response untrustworthy. Yes, in principle Flatiron can measure your HRTF using a speaker,
but that's only if you have nice, flat studio monitors and a very acoustically well-treated space.
Generally, speaker colorations and room reflections will make that far too noisy to use naively as a target.

Flatiron deals with these limitations in a few ways. For one, by default it only attempts to smooth out narrow
features in the frequency response (treble notches, basically), not correct broadband tonal qualities.
Specifically, after subtracting the HRTF reference, it fits a smooth baseline to the remaining headphone
response, then only features that deviate from _that_ are narrower than a half octave, and clear a
certain threshold for statistical significance get smoothed.

But even with this conservative approach, a
raw speaker response curve generally won't make for a suitable reference. Instead, Flatiron offers a few
reference options. The default is a generic diffuse field HRTF, taken from a combination of the ISO 226 and
ISO 389-7 standards. You can also select the raw ISO 226 free field reference with `config -r iso226`.
(Diffuse field and free field refer to slightly different ways of handling the fact that HRTFs depend
on the direction from which sound enters your ear. Free field considers only sound from directly in front
of your head, while diffuse field averages over all directions.)
A better option, if you're lucky enough to own multiple headphones (all ideally close-ish
to a reference tuning) is the "consensus" baseline. This combines the measured responses of all the headphones,
keeping features that they agree on and smoothing out regions where they differ. It can be selected with
`config -r consensus:<PROFILE1>,<PROFILE2>,...`. This still isn't perfect though, because a feature being
present in the HpTF of every headphone doesn't necessarily mean it's really there in your HRTF. It could be
random chance, or more likely, a common artifact arising from some generic interaction of any headphone
cup with your ear geometry. Adding a speaker profile to the consensus, even a very noisy one, helps
Flatiron identify and ignore such features.

Most of Flatiron's configuration options have to do with these issues: controlling either the reference
or how features are identified and corrected. Getting good results for your specific devices and anatomy
may require a degree of trial and error, backed by subjective listening tests.
