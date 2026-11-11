"""Listeners (DESIGN.md, "Listeners"): the one-off migration, LISTENER/PROFILE
names, and what stays within one listener."""

import json

import pytest

from flatiron.cli import main


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("FLATIRON_LISTENER", raising=False)
    return tmp_path / "flatiron"


def _sim(prof, n=60, seed=1):
    from flatiron.session.scheduler import SchedulerConfig
    from flatiron.sim.listener import design_example
    from flatiron.sim.profile import simulate_profile

    simulate_profile(prof, {e: design_example(e) for e in "LR"}, sessions=1, trials_per_ear=n, seed=seed,
                     config=SchedulerConfig(min_trials_noise_fit=10_000))


def test_the_old_layout_becomes_the_default_listener(data, capsys):
    (data / "profiles" / "hd560").mkdir(parents=True)
    (data / "profiles" / "hd560" / "profile.json").write_text(json.dumps({"label": "HD560", "slug": "hd560"}))
    for sub in ("noise", "consensus"):
        (data / "cache" / sub).mkdir(parents=True)
        (data / "cache" / sub / "x.json").write_text("{}")
    (data / "cache" / "comparison.html").write_text("page")
    assert main(["profiles"]) is None and "HD560" in capsys.readouterr().out
    home = data / "listeners" / "default"
    assert not (data / "profiles").exists() and (home / "profiles" / "hd560" / "profile.json").exists()
    assert (home / "listener.json").exists() and (home / "cache" / "noise" / "x.json").exists()
    assert (home / "cache" / "consensus" / "x.json").exists() and (data / "cache" / "comparison.html").exists()


def test_names_need_a_listener_once_there_are_several(data, capsys, monkeypatch):
    from flatiron.session.store import Profile, current_listener

    _sim(Profile.open("HD600"))
    assert main(["sessions", "HD600"]) is None  # one listener: plain names, as before
    assert main(["listeners", "new", "Bob"]) == 0
    assert main(["profiles", "new", "bob/HD600"]) == 0
    capsys.readouterr()
    assert main(["sessions", "HD600"]) == 1 and "LISTENER/PROFILE" in capsys.readouterr().out
    assert main(["sessions"]) == 1  # no name at all is just as ambiguous
    assert main(["sessions", "default/HD600"]) is None
    assert main(["sessions", "bob/"]) is None and "No sessions" in capsys.readouterr().out  # bob's only profile
    assert main(["sessions", "carol/HD600"]) == 1 and "No listener 'carol'" in capsys.readouterr().out
    monkeypatch.setenv("FLATIRON_LISTENER", "bob")
    assert current_listener().slug == "bob"
    assert main(["sessions", "HD600"]) is None and "No sessions" in capsys.readouterr().out
    monkeypatch.setenv("FLATIRON_LISTENER", "carol")
    assert main(["sessions", "HD600"]) == 1 and "isn't a listener" in capsys.readouterr().out
    monkeypatch.delenv("FLATIRON_LISTENER")
    capsys.readouterr()
    assert main(["profiles"]) is None
    out = capsys.readouterr().out
    assert "bob/" in out and "default/" in out


def test_listeners_list_and_rename(data, capsys):
    from flatiron.session.store import Listener, Profile

    _sim(Profile.open("HD600"))
    assert main(["listeners"]) == 0 and "default  (Default)" in capsys.readouterr().out
    assert main(["listeners", "rename", "default", "Dathan"]) == 0
    assert [x.slug for x in Listener.all()] == ["dathan"] and Listener.find("dathan").label == "Dathan"
    assert main(["sessions", "HD600"]) is None  # still the only listener
    assert main(["listeners", "new", "Dathan"]) == 1


def test_moving_a_profile_between_listeners(data, capsys):
    from flatiron.session.store import Listener, Profile

    _sim(Profile.open("A"))
    _sim(Profile.open("B"), seed=2)
    Listener.create("Bob")
    assert main(["config", "default/A", "--sessions", "A,B"]) == 0  # A pools B
    capsys.readouterr()
    assert main(["profiles", "move", "default/B", "--to", "bob"]) == 1 and "A use" in capsys.readouterr().out
    assert main(["profiles", "move", "default/A", "--to", "bob"]) == 1 and "it uses b" in capsys.readouterr().out
    assert main(["config", "default/A", "--reset", "sessions"]) == 0
    assert main(["profiles", "move", "default/B", "--to", "bob"]) == 0
    assert [p.slug for p in Listener.find("bob").profiles()] == ["b"]
    assert [p.slug for p in Listener.find("default").profiles()] == ["a"]


def test_comparisons_cross_listeners_and_consensus_does_not(data, capsys, tmp_path, monkeypatch):
    from flatiron.session.store import Listener, Profile

    _sim(Profile.open("A"))
    bob = Listener.create("Bob")
    _sim(Profile.open("A", bob.root), seed=2)
    _sim(Profile.open("C", bob.root), seed=3)
    capsys.readouterr()
    assert main(["compare", "default/A", "bob/A"]) == 0
    out = capsys.readouterr().out
    assert "default/A" in out and "bob/A" in out
    # A consensus or veto is about one listener's ears.
    assert main(["config", "bob/C", "-r", "consensus:default/A"]) == 1
    assert "same listener" in capsys.readouterr().out
    assert main(["config", "bob/C", "--veto", "A"]) == 0
    # Exports carry the listener's name when there are several.
    monkeypatch.chdir(tmp_path)
    assert main(["export", "bob/A", "-t", "apo"]) == 0
    assert any(p.name.startswith("bob-a-") for p in tmp_path.iterdir())
