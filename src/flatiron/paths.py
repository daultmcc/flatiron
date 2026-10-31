"""Platform data directory (see DESIGN.md, "Data on disk")."""

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "flatiron"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "flatiron"
