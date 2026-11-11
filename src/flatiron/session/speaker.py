"""Speaker profiles: the position plan and the block's setup (DESIGN.md,
"Speaker sessions").

A speaker is measured in short blocks, one open ear and one position each,
with the other ear plugged. The plan covers five directions around the open
ear at three heights, 15 positions per ear. Ears switch every CHUNK blocks,
each chunk one height, so the speaker's height changes once a chunk and only
the chair turns between blocks.
"""

from dataclasses import dataclass

from flatiron import EARS

DEFAULT_BLOCK = 32  # trials: 15 positions x 32 = 480 per ear
MIN_BLOCK, MAX_BLOCK = 16, 60
CHUNK = 5  # blocks on one ear before switching

# Degrees from straight ahead toward the open ear, and how to get there.
AZIMUTHS = (
    (0, "straight in front of you"),
    (30, "30° toward your open ear"),
    (60, "60° toward your open ear"),
    (90, "level with your open ear (side-on)"),
    (135, "behind your open ear (a rear quarter: turn the chair away)"),
)
HEIGHTS = ((0, "at ear level"), (30, "raised, about 30° above ear level"), (-30, "lowered, about 30° below ear level"))
EAR_WORDS = {"L": "left", "R": "right"}


@dataclass(frozen=True)
class Position:
    ear: str  # the open ear
    azimuth: int
    elevation: int

    @property
    def label(self) -> str:
        height = {0: "ear level", 30: "high", -30: "low"}[self.elevation]
        return f"{self.azimuth}° {height}"

    def to_json(self) -> dict:
        return {"label": self.label, "azimuth": self.azimuth, "elevation": self.elevation}

    def instructions(self) -> str:
        az = dict(AZIMUTHS)[self.azimuth]
        el = dict(HEIGHTS)[self.elevation]
        other = EAR_WORDS["R" if self.ear == "L" else "L"]
        return (f"{EAR_WORDS[self.ear].upper()} ear open: put a foam earplug fully into your {other} ear.\n\n"
                f"Speaker {el}, about 1 m away (at least 0.7 m), facing you.\n"
                f"Turn your chair or head so the speaker is {az}.")


def plan() -> list[Position]:
    """Every block of a full measurement, in order: each height, an ear's
    five directions, then the other ear's."""
    return [Position(ear, az, el) for el, _ in HEIGHTS for ear in EARS for az, _ in AZIMUTHS]


def done_positions(sessions) -> set[Position]:
    """The plan's positions that have a block with answers in it."""
    out = set()
    for s in sessions:
        info = getattr(s, "speaker", None)
        if not info or not any(not t.undone for t in s.trials):
            continue
        pos = info.get("position") or {}
        out.add(Position(info["open_ear"], int(pos.get("azimuth", -1)), int(pos.get("elevation", 0))))
    return out


def next_position(sessions) -> Position | None:
    """The first position of the plan without a block yet (None: the plan is done)."""
    done = done_positions(sessions)
    return next((p for p in plan() if p not in done), None)


def progress(sessions) -> tuple[int, int]:
    done = done_positions(sessions)
    return sum(p in done for p in plan()), len(plan())
