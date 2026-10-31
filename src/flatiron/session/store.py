"""Profiles and session files on disk (DESIGN.md, "Data on disk").

profiles/<slug>/profile.json
profiles/<slug>/sessions/<timestamp>.json   every trial, written after each one
profiles/<slug>/exports/
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from flatiron import EARS
from flatiron.model.probit import Trials
from flatiron.paths import data_dir

FORMAT_VERSION = 1


def slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    if not s:
        raise ValueError(f"can't make a profile name from {label!r}")
    return s


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_json(path: Path, obj) -> None:
    """Atomic write: a crash mid-save never leaves a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@dataclass
class TrialRecord:
    ear: str
    f_a: float
    f_b: float
    delta_a: float
    delta_b: float
    a_first: bool
    rove_db: float
    catch: bool
    response: str  # "first" or "second": which interval the listener chose
    a_louder: bool
    rt_ms: float  # from the end of B in the first presentation; negative = answered during it
    replays: int = 0
    fast: bool = False
    eq_gain_a: float = 0.0  # known EQ gain in effect at f_a / f_b (active + preview)
    eq_gain_b: float = 0.0
    t: float = 0.0  # seconds since session start
    rt_last_ms: float | None = None  # from the end of B in the latest replay

    @property
    def offset(self) -> float:
        return self.delta_a - self.delta_b + self.eq_gain_a - self.eq_gain_b


@dataclass
class Session:
    profile: str
    id: str
    started: str
    device: dict = field(default_factory=dict)
    sample_rate: int = 48_000
    eq_context: dict = field(default_factory=lambda: {"note": "none", "apo": None})
    stimulus: dict = field(default_factory=dict)
    level_dbfs: float = -40.0
    gap_s: float = 0.3
    seed: int = 0
    scheduler: dict = field(default_factory=dict)
    grid: dict = field(default_factory=dict)  # ear -> list of Hz, at the end of the session
    channel_checks: list = field(default_factory=list)
    sweep_marks: dict = field(default_factory=dict)  # ear -> list of {freq, kind}
    trials: list[TrialRecord] = field(default_factory=list)
    ended: str | None = None
    version: int = FORMAT_VERSION

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Session":
        d = dict(d)
        if d.get("version") != FORMAT_VERSION:
            raise ValueError(f"unsupported session format {d.get('version')}")
        d["trials"] = [TrialRecord(**t) for t in d.get("trials", [])]
        return cls(**d)

    def trials_for(self, ear: str) -> Trials:
        ts = [t for t in self.trials if t.ear == ear]
        return Trials([t.f_a for t in ts], [t.f_b for t in ts], [t.offset for t in ts],
                      [t.a_louder for t in ts])


class Profile:
    def __init__(self, slug: str, root: Path | None = None):
        self.slug = slug
        self.dir = (root or data_dir()) / "profiles" / slug

    @classmethod
    def open(cls, label: str, root: Path | None = None) -> "Profile":
        """Open a profile by label or slug, creating it if needed."""
        p = cls(slugify(label), root)
        if not p.path.exists():
            write_json(p.path, {"label": label, "slug": p.slug, "created": now_iso(),
                                "level": None, "fit": None, "version": FORMAT_VERSION})
        return p

    @property
    def path(self) -> Path:
        return self.dir / "profile.json"

    @property
    def sessions_dir(self) -> Path:
        return self.dir / "sessions"

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    def update(self, **kw) -> None:
        d = self.read()
        d.update(kw)
        write_json(self.path, d)

    def new_session(self, **kw) -> Session:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        sid, k = stamp, 1
        while (self.sessions_dir / f"{sid}.json").exists():
            k += 1
            sid = f"{stamp}-{k}"
        return Session(profile=self.slug, id=sid, started=now_iso(), **kw)

    def save(self, s: Session) -> None:
        write_json(self.sessions_dir / f"{s.id}.json", s.to_json())

    def delete(self, s: Session) -> None:
        (self.sessions_dir / f"{s.id}.json").unlink(missing_ok=True)

    def sessions(self) -> list[Session]:
        if not self.sessions_dir.exists():
            return []
        return [Session.from_json(json.loads(p.read_text()))
                for p in sorted(self.sessions_dir.glob("*.json"))]

    def history(self, sessions: list[Session] | None = None) -> tuple[dict[str, Trials], dict[str, np.ndarray]]:
        """All trials pooled per ear, and the most recent grid per ear."""
        sessions = self.sessions() if sessions is None else sessions
        trials = {e: Trials.concat([s.trials_for(e) for s in sessions]) if sessions
                  else Trials([], [], [], []) for e in EARS}
        grids = {}
        for s in sessions:
            for e, g in s.grid.items():
                grids[e] = np.asarray(g, float)
        return trials, grids
