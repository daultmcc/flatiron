import os

# Small matrices: BLAS threading only adds overhead.
for v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(v, "1")


def pytest_xdist_auto_num_workers(config):
    """-n auto: one worker per core but one, so the machine stays usable and
    the workers don't contend with everything else (all cores: about 50% slower)."""
    return max(1, (os.cpu_count() or 2) - 1)


# Session files are saved after every answer with an fsync; in throwaway test
# folders that's all cost (about 13 ms a save).
import flatiron.session.store  # noqa: E402

flatiron.session.store.fsync = lambda fd: None


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """report and compare open their HTML unless -H: never in tests."""
    monkeypatch.setattr("webbrowser.open", lambda *_: None)


@pytest.fixture
def tmp_path(tmp_path):
    """Tests often use tmp_path as a listener's folder (Profile.open(name,
    root=tmp_path)), and listener_of() takes the listener's data folder to be
    two levels up: with pytest's own layout that's the run's base folder,
    shared by every xdist worker, so two tests whose folders got the same name
    raced on one listener.json. One level more makes it the test's own."""
    path = tmp_path / "data" / "listeners" / "default"
    path.mkdir(parents=True)
    return path
