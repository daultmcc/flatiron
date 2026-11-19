"""`measure`: one command for every kind of session (DESIGN.md, "Session flow")."""

import pytest

from flatiron.cli import main
from flatiron.session.centring import CentringRunner, CentringSettings
from flatiron.session.store import Profile, current_listener
from flatiron.session.threshold import ThresholdRunner, ThresholdSettings


@pytest.fixture
def hp(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return Profile.open("hp", root=current_listener().root)


def test_options_for_other_kinds_of_session_are_refused(hp, monkeypatch, capsys):
    launched = []
    monkeypatch.setattr("flatiron.cli._launch_detection", lambda args, prof, mode: launched.append(
        (mode, args.minutes, args.stimulus.bandwidth_oct, args.match)) or 0)
    monkeypatch.setattr("flatiron.cli._launch", lambda args, **kw: launched.append(("loudness", args.minutes)) or 0)
    for extra, flag in ((["--threshold", "--cross-ear", "5"], "--cross-ear"), (["--centre", "-s"], "-s/--sweep"),
                        (["--threshold", "--no-match"], "--no-match"), (["--centre", "--active-eq", "x.txt"],
                                                                       "--active-eq")):
        assert main(["measure", "hp", "-n", *extra]) == 1
        assert flag in capsys.readouterr().out
    assert main(["measure", "hp", "-n", "--centre", "--no-match"]) == 0
    assert main(["measure", "hp", "-n", "--threshold"]) == 0
    assert main(["measure", "hp", "-n"]) == 0
    # Each kind's own defaults.
    assert launched == [("centre", 5.0, 1.0, False), ("threshold", 8.0, 1 / 12, None), ("loudness", 12.0)]
    with pytest.raises(SystemExit):
        main(["measure", "hp", "--threshold", "--centre"])
    for old in ("run", "threshold", "centre"):
        with pytest.raises(SystemExit):
            main([old, "hp"])


def test_the_listening_level_is_the_profiles_whatever_the_kind_of_session(hp):
    hp.set_level(-37.0)
    assert ThresholdRunner(hp, ThresholdSettings(), seed=0, volume=False).level_dbfs == -37.0
    c = CentringRunner(hp, CentringSettings(), seed=0, curves={"L": None, "R": None})
    assert c.level_dbfs == -37.0 and c.session.level_dbfs == -37.0
    c.set_level(-30.0)
    assert c.session.level_dbfs == -30.0
