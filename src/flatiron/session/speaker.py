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

# Degrees from straight ahead toward the open ear, and above ear level.
AZIMUTHS = (0, 30, 60, 90, 135)
HEIGHTS = (0, 30, -30)
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
        side, other = EAR_WORDS[self.ear], EAR_WORDS["R" if self.ear == "L" else "L"]
        vert = "at ear level" if self.elevation == 0 else \
            f"{abs(self.elevation)}° {'above' if self.elevation > 0 else 'below'}"
        horiz = "straight in front" if self.azimuth == 0 else f"{self.azimuth}° to the {side}"
        behind = " (behind you)" if self.azimuth > 90 else ""
        drop = "" if self.elevation == 0 else \
            f" At 1 m away that's about 0.5 m {'above' if self.elevation > 0 else 'below'} your ears."
        return (f"{side.upper()} ear open: put a foam earplug fully into your {other} ear.\n\n"
                f"The speaker should be {vert} and {horiz} of your head{behind}, about 1 m away "
                f"(at least 0.7 m), pointing at you.{drop}\n"
                "Angles are from your head facing straight ahead, upright and level: turn the chair "
                "to change direction, and move the speaker, not your head, to change height.")


def plan() -> list[Position]:
    """Every block of a full measurement, in order: each height, an ear's
    five directions, then the other ear's."""
    return [Position(ear, az, el) for el in HEIGHTS for ear in EARS for az in AZIMUTHS]


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
