import numpy as np
import pytest

from flatiron.audio.player import Mixer

FS = 48000


def run(m, blocks=40, size=256):
    return np.concatenate([m.render(size) for _ in range(blocks)])


def max_step(x):
    return np.max(np.abs(np.diff(x[:, 0])))


def test_plays_buffer_then_silence():
    m = Mixer(fs=FS)
    buf = np.linspace(0, 0.1, 1000, dtype=np.float32)
    m.start(buf)
    out = run(m, 8)
    assert np.allclose(out[:1000, 0], buf) and np.allclose(out[:1000, 1], buf)
    assert np.all(out[1000:] == 0) and not m.playing


def test_stop_mid_sound_fades_without_a_jump():
    m = Mixer(fs=FS)
    m.start(np.full((FS, 2), 0.5, np.float32))  # worst case: full-scale DC step
    a = run(m, 10)
    m.stop()
    b = run(m, 10)
    x = np.concatenate([a, b])
    assert not m.playing
    assert np.all(b[m.n_fade:] == 0)
    # A 10 ms raised cosine from 0.5: steepest step is 0.5 * pi / (2 * 480).
    assert max_step(x[a.shape[0] - 1:]) < 0.5 * np.pi / (2 * m.n_fade) * 1.01


def test_start_while_playing_fades_then_switches():
    m = Mixer(fs=FS)
    m.start(np.full((FS, 2), 0.5, np.float32))
    run(m, 4)
    new = np.zeros((2000, 2), np.float32)
    new[1000:] = 0.25  # marker: starts silent, so any jump would be the fade's fault
    m.start(new)
    out = run(m, 20)
    fade_end = m.n_fade
    # Through the fade and the switch-over (the step at the marker is the new sound's own).
    assert max_step(out[:fade_end + 1000]) < 0.5 * np.pi / (2 * m.n_fade) * 1.01
    assert np.all(out[fade_end:fade_end + 1000] == 0)
    assert np.allclose(out[fade_end + 1000:fade_end + 2000], 0.25)


def test_loop_wraps():
    m = Mixer(fs=FS)
    buf = np.arange(100, dtype=np.float32)[:, None].repeat(2, 1) / 1000
    m.start(buf, loop=True)
    out = m.render(250)
    assert np.allclose(out[:, 0], np.tile(buf[:, 0], 3)[:250])
    assert m.playing


def test_stop_when_idle_is_harmless():
    m = Mixer(fs=FS)
    m.stop()
    assert np.all(m.render(64) == 0)


def test_switch_keeps_position_and_crossfades():
    m = Mixer(fs=FS)
    a = np.full((4000, 2), 0.5, np.float32)
    b = np.full((4000, 2), -0.5, np.float32)
    b[:, 1] = 0.25
    m.start(a, loop=True)
    first = m.render(1000)
    m.switch(b)
    out = np.concatenate([first, m.render(3000)])
    # Same position: the switch happens over the next n_fade samples, then it's b.
    assert np.allclose(out[1000 + m.n_fade:, 0], -0.5) and np.allclose(out[1000 + m.n_fade:, 1], 0.25)
    assert max_step(out) < 1.0 * np.pi / (2 * m.n_fade) * 1.01  # a 1.0 swing, raised-cosine
    assert m.playing and m.pos == 0  # a full loop: back to the start
