"""Flatiron: by-ear headphone resonance EQ."""

import os

# Flatiron's matrices are small (a few hundred rows): BLAS threading only adds
# overhead, about 8× on a 16-core machine. Set before numpy loads its BLAS.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

__version__ = "0.1.0"

FS = 48_000
EARS = ("L", "R")
