"""Reference contours E(f): the perceived-level shape a reference-correct
headphone would produce (DESIGN.md, pipeline step 1).

    iso226     negated ISO 226 (frontal free field) equal-loudness contour
    diffuse    iso226 plus a diffuse-minus-free-field threshold difference,
               read from a CSV (ISO 389-7 is paywalled and its table hasn't
               been sourced yet; see DIFFUSE_CSV)
    none       flat
    <path>     a user CSV of perceived-level shape: "freq_hz,db" rows

A consensus reference ("consensus:A,B") is built from other profiles' fits, so
it's made per profile in fit/run.py and doesn't come through here.

Every reference is returned with its mean removed over the frequencies asked for.
"""

from pathlib import Path

import numpy as np

from flatiron import iso226
from flatiron.paths import data_dir

BUILTIN = ("iso226", "diffuse", "none")
CONSENSUS_PREFIX = "consensus:"


def diffuse_csv() -> Path:
    return data_dir() / "reference" / "diffuse_minus_free_db.csv"


def read_curve_csv(path) -> tuple[np.ndarray, np.ndarray]:
    rows = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p for p in line.replace(";", ",").replace("\t", ",").split(",") if p.strip()]
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except (ValueError, IndexError):
            if rows:  # a header is fine, anything later isn't
                raise ValueError(f"{path}:{n}: expected 'freq_hz,db', got {line!r}")
    if len(rows) < 2:
        raise ValueError(f"{path}: need at least two 'freq_hz,db' rows")
    f, db = np.array(sorted(rows)).T
    return f, db


def interp_log(f_src, db_src, freqs) -> np.ndarray:
    """Linear in log2 f; held constant beyond the ends."""
    return np.interp(np.log2(freqs), np.log2(f_src), db_src)


def reference(name: str, freqs, phon: float = 60.0) -> np.ndarray:
    f = np.asarray(freqs, float)
    if name.startswith(CONSENSUS_PREFIX):
        raise ValueError("a consensus reference is only available in a profile's own fit, not here")
    if name == "none":
        e = np.zeros(f.size)
    elif name == "iso226":
        e = iso226.perceived_level(f, phon)
    elif name == "diffuse":
        path = diffuse_csv()
        if not path.exists():
            raise FileNotFoundError(
                f"The diffuse-field reference needs the ISO 389-7 diffuse-minus-free-field threshold "
                f"difference as 'freq_hz,db' rows in {path}. Use --reference iso226 until then.")
        # A higher diffuse-field threshold means quieter, so it lowers perceived level.
        e = iso226.perceived_level(f, phon) - interp_log(*read_curve_csv(path), f)
    else:
        e = interp_log(*read_curve_csv(name), f)
    return e - e.mean()
