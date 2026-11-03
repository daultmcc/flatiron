import os

# Small matrices: BLAS threading only adds overhead.
for v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(v, "1")


# Session files are saved after every answer with an fsync; in throwaway test
# folders that's all cost (about 13 ms a save).
import flatiron.session.store  # noqa: E402

flatiron.session.store.fsync = lambda fd: None
