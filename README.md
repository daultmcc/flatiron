# Flatiron

By-ear headphone resonance EQ from forced-choice loudness judgments. See
[DESIGN.md](DESIGN.md) for the method.

Status: milestones 1 (audio core) and 2 (model and simulator) are in place.

```sh
uv sync
uv run flatiron devices              # list outputs, flag Bluetooth/mono/rate issues
uv run flatiron level --device N     # loop a 4 kHz band while setting system volume
uv run flatiron probe --device N     # hunt by hand: play a band at a chosen freq/ear
uv run flatiron simulate --trials 600  # fit a simulated listener, report recovery
uv run pytest
```

Layout: `audio/` (biquads, stimuli, limiter, devices, playback), `model/`
(grid, probit fit), `sim/` (simulated listener, non-adaptive trial design),
`iso226.py` (equal-loudness contours).
