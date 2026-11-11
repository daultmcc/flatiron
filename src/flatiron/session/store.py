"""Listeners, profiles and session files on disk (DESIGN.md, "Data on disk").

listeners/<listener>/listener.json
listeners/<listener>/profiles/<slug>/profile.json
listeners/<listener>/profiles/<slug>/sessions/<timestamp>.json   every trial, written after each one
listeners/<listener>/profiles/<slug>/cache/                      perceptual fits, the last HTML report
listeners/<listener>/cache/                                      the listener's noise, consensus results
"""

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from flatiron import EARS
from flatiron.audio.limiter import DEFAULT_LEVEL_DB
from flatiron.audio.stimulus import POINT, StimulusSpec, stimulus_key
from flatiron.model.probit import Trials
from flatiron.paths import data_dir

FORMAT_VERSION = 1
fsync = os.fsync  # the tests swap in a no-op: their files are throwaway
RESERVED = ("none",)  # profile names the CLI uses as keywords
KINDS = ("headphone", "iem")


LISTENER_ENV = "FLATIRON_LISTENER"  # the listener an unprefixed profile name belongs to
DEFAULT_LISTENER = "default"


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
        fsync(f.fileno())
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
    level_dbfs: float | None = None  # presentation level for this trial (None: the session's)
    undone: bool = False  # withdrawn by the listener; kept on file, left out of fits
    retake: bool = False  # answer given after undoing the previous one for this trial

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
    level_dbfs: float = DEFAULT_LEVEL_DB
    gap_s: float = 0.3
    seed: int = 0
    scheduler: dict = field(default_factory=dict)
    grid: dict = field(default_factory=dict)  # ear -> list of Hz, at the end of the session
    channel_checks: list = field(default_factory=list)
    level_changes: list = field(default_factory=list)  # {after_trial, from, to, at}
    preview: dict | None = None  # verify sessions: the correction applied to every burst (PreviewEQ JSON)
    sweep_marks: dict = field(default_factory=dict)  # ear -> list of {freq, kind}
    sweep_eq: str | None = None  # fit hash of the correction applied during the sweep (it can be toggled)
    tone_marks: list = field(default_factory=list)  # tone-generator marks this session's grid was refined around
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
        ts = [t for t in self.trials if t.ear == ear and not t.undone]
        key = stimulus_key(StimulusSpec(**self.stimulus)) if self.stimulus else POINT
        return Trials([t.f_a for t in ts], [t.f_b for t in ts], [t.offset for t in ts],
                      [t.a_louder for t in ts], [1.0 if t.a_first else -1.0 for t in ts], [key] * len(ts))


# -- listeners (DESIGN.md, "Listeners") ----------------------------------------

class AmbiguousListener(ValueError):
    pass


class Listener:
    """One person: their profiles, caches and noise, in a folder of their own.
    `root` is what a profile's fits treat as their data folder."""

    def __init__(self, slug: str, data: Path | None = None):
        self.slug = slug
        self.data = data or data_dir()
        self.root = self.data / "listeners" / slug

    @property
    def path(self) -> Path:
        return self.root / "listener.json"

    @property
    def label(self) -> str:
        return json.loads(self.path.read_text()).get("label", self.slug) if self.path.exists() else self.slug

    def ensure(self) -> "Listener":
        if not self.path.exists():
            write_json(self.path, {"label": self.slug if self.slug != DEFAULT_LISTENER else "Default",
                                   "slug": self.slug, "created": now_iso(), "version": FORMAT_VERSION})
        return self

    @classmethod
    def all(cls, data: Path | None = None) -> list["Listener"]:
        migrate(data)
        base = (data or data_dir()) / "listeners"
        if not base.is_dir():
            return []
        return [cls(d.name, data) for d in sorted(base.iterdir()) if d.is_dir() and not d.name.startswith(".")]

    @classmethod
    def find(cls, name: str, data: Path | None = None) -> "Listener | None":
        for x in cls.all(data):
            if name in (x.slug, x.label) or slugify(name) == x.slug:
                return x
        return None

    @classmethod
    def create(cls, label: str, data: Path | None = None) -> "Listener":
        x = cls(slugify(label), data)
        if x.root.exists():
            raise ValueError(f"There's already a listener {x.slug!r}.")
        x.root.mkdir(parents=True)
        write_json(x.path, {"label": label, "slug": x.slug, "created": now_iso(), "version": FORMAT_VERSION})
        return x

    def rename(self, label: str) -> "Listener":
        new = Listener(slugify(label), self.data)
        if new.slug != self.slug:
            if new.root.exists():
                raise ValueError(f"There's already a listener {new.slug!r}.")
            self.ensure()
            self.root.rename(new.root)
        d = json.loads(new.path.read_text())
        d.update(label=label, slug=new.slug)
        write_json(new.path, d)
        return new

    def profiles(self) -> list["Profile"]:
        return Profile.all(self.root)


def migrate(data: Path | None = None) -> "Listener | None":
    """Once: the single-listener layout (profiles/ at the top) becomes the
    default listener's. The caches that belong to a listener move with it."""
    data = data or data_dir()
    old = data / "profiles"
    if not old.is_dir():
        return None
    target = Listener(DEFAULT_LISTENER, data)
    if (target.root / "profiles").exists():
        raise RuntimeError(f"Can't move {old} into {target.root}: it already has profiles. Move one of them by hand.")
    target.root.mkdir(parents=True, exist_ok=True)
    target.ensure()
    old.rename(target.root / "profiles")
    for sub in ("noise", "consensus"):
        src = data / "cache" / sub
        if src.is_dir():
            dst = target.root / "cache" / sub
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(src)  # a cache: it's recomputed
            else:
                src.rename(dst)
    return target


def current_listener(data: Path | None = None) -> Listener:
    """The listener an unprefixed profile name belongs to: the one named by
    FLATIRON_LISTENER, or the only one (the default, before there are any)."""
    env = os.environ.get(LISTENER_ENV)
    everyone = Listener.all(data)
    if env:
        x = Listener.find(env, data)
        if x is None:
            raise ValueError(f"{LISTENER_ENV}={env!r} isn't a listener. See: flatiron listeners")
        return x
    if len(everyone) > 1:
        raise AmbiguousListener("There are several listeners (" + ", ".join(x.slug for x in everyone)
                                + "): name the profile as LISTENER/PROFILE, or set " + LISTENER_ENV + ".")
    return everyone[0] if everyone else Listener(DEFAULT_LISTENER, data)


def split_name(name: str, data: Path | None = None) -> tuple[Listener, str]:
    """`listener/profile` or `profile` (the current listener's). The profile
    part may be empty: the listener's only profile."""
    if "/" in name:
        who, prof = name.split("/", 1)
        x = Listener.find(who, data)
        if x is None:
            raise ValueError(f"No listener {who!r}. See: flatiron listeners")
        return x, prof
    return current_listener(data), name


def listener_of(profile: "Profile") -> Listener:
    root = profile.dir.parent.parent
    return Listener(root.name, root.parent.parent)


def several_listeners(data: Path | None = None) -> bool:
    return len(Listener.all(data)) > 1


def qualified(profile: "Profile") -> str:
    """The profile's label, with its listener's name when there are several."""
    label = profile.read()["label"]
    x = listener_of(profile)
    return f"{x.slug}/{label}" if several_listeners(x.data) else label


class Profile:
    def __init__(self, slug: str, root: Path | None = None):
        """root: the listener's folder (default: the current listener's)."""
        self.slug = slug
        self.dir = (root or current_listener().root) / "profiles" / slug

    @classmethod
    def open(cls, label: str, root: Path | None = None) -> "Profile":
        """Open a profile by label or slug, creating it if needed."""
        p = cls(slugify(label), root)
        if p.slug in RESERVED and not p.path.exists():
            raise ValueError(f"{label!r} can't be a profile name: `config --veto none` uses it")
        if not p.path.exists():
            listener_of(p).ensure()
            write_json(p.path, {"label": label, "slug": p.slug, "created": now_iso(),
                                "version": FORMAT_VERSION})
        return p

    @classmethod
    def all(cls, root: Path | None = None) -> list["Profile"]:
        base = (root or current_listener().root) / "profiles"
        if not base.exists():
            return []
        return [cls(d.name, root) for d in sorted(base.iterdir()) if (d / "profile.json").exists()]

    @classmethod
    def existing(cls, label: str, root: Path | None = None) -> "Profile | None":
        p = cls(slugify(label), root)
        return p if p.path.exists() else None

    @property
    def path(self) -> Path:
        return self.dir / "profile.json"

    @property
    def sessions_dir(self) -> Path:
        return self.dir / "sessions"

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    @property
    def kind(self) -> str:
        return self.read().get("kind", "headphone")

    def update(self, **kw) -> None:
        d = self.read()
        d.update(kw)
        d.pop("fit", None)  # saved by the old `fit` command; fits are computed on demand now
        write_json(self.path, d)

    def marks(self) -> list[dict]:
        """Frequencies marked in the tone generator: {ear, freq, kind,
        stimulus, at, session}. `session` is the trial session whose grid
        was refined around the mark (None until one starts)."""
        return self.read().get("marks", [])

    def add_mark(self, ear: str, freq: float, kind: str, stimulus: str) -> None:
        self.update(marks=self.marks() + [{"ear": ear, "freq": float(freq), "kind": kind,
                                           "stimulus": stimulus, "at": now_iso(), "session": None}])

    def remove_last_mark(self) -> dict | None:
        m = self.marks()
        if not m:
            return None
        last = m.pop()
        self.update(marks=m)
        return last

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

    def last_level(self, sessions: list[Session] | None = None) -> float | None:
        """Level of the most recent trial on record (or the latest session's)."""
        sessions = self.sessions() if sessions is None else sessions
        for s in reversed(sessions):
            for t in reversed(s.trials):
                if not t.undone:
                    return s.level_dbfs if t.level_dbfs is None else t.level_dbfs
        return sessions[-1].level_dbfs if sessions else None

    def history(self, sessions: list[Session] | None = None) -> tuple[dict[str, Trials], dict[str, np.ndarray]]:
        """All trials per ear, one group per session with trials in that ear
        (in the order given), and the most recent grid per ear."""
        sessions = self.sessions() if sessions is None else sessions
        trials = {}
        for e in EARS:
            parts = [t for t in (s.trials_for(e) for s in sessions) if len(t)]
            trials[e] = Trials.grouped(parts) if parts else Trials([], [], [], [])
        grids = {}
        for s in sessions:
            for e, g in s.grid.items():
                grids[e] = np.asarray(g, float)
        return trials, grids
