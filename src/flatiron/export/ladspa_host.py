"""A minimal LADSPA host (ctypes), enough to run a plugin offline over a
buffer. Used to check exports against the real LSP filters."""

import ctypes as C

import numpy as np

INPUT, OUTPUT, CONTROL, AUDIO = 1, 2, 4, 8


class _Hint(C.Structure):
    _fields_ = [("hint", C.c_int), ("lo", C.c_float), ("hi", C.c_float)]


class _Desc(C.Structure):
    pass


_Handle = C.c_void_p
_Desc._fields_ = [
    ("UniqueID", C.c_ulong), ("Label", C.c_char_p), ("Properties", C.c_int), ("Name", C.c_char_p),
    ("Maker", C.c_char_p), ("Copyright", C.c_char_p), ("PortCount", C.c_ulong),
    ("PortDescriptors", C.POINTER(C.c_int)), ("PortNames", C.POINTER(C.c_char_p)),
    ("PortRangeHints", C.POINTER(_Hint)), ("ImplementationData", C.c_void_p),
    ("instantiate", C.CFUNCTYPE(_Handle, C.POINTER(_Desc), C.c_ulong)),
    ("connect_port", C.CFUNCTYPE(None, _Handle, C.c_ulong, C.POINTER(C.c_float))),
    ("activate", C.CFUNCTYPE(None, _Handle)),
    ("run", C.CFUNCTYPE(None, _Handle, C.c_ulong)),
    ("run_adding", C.c_void_p), ("set_run_adding_gain", C.c_void_p),
    ("deactivate", C.CFUNCTYPE(None, _Handle)),
    ("cleanup", C.CFUNCTYPE(None, _Handle)),
]


class Plugin:
    def __init__(self, lib_path: str, label_part: str):
        self._lib = C.CDLL(lib_path)
        self._lib.ladspa_descriptor.restype = C.POINTER(_Desc)
        self._lib.ladspa_descriptor.argtypes = [C.c_ulong]
        i = 0
        while True:
            d = self._lib.ladspa_descriptor(i)
            if not d:
                raise KeyError(f"no LADSPA plugin matching {label_part!r} in {lib_path}")
            if label_part in d.contents.Label.decode():
                self._d = d
                break
            i += 1
        desc = self._d.contents
        self.ports = [desc.PortNames[i].decode() for i in range(desc.PortCount)]
        self.kinds = [desc.PortDescriptors[i] for i in range(desc.PortCount)]

    def run(self, controls: dict[str, float], audio: np.ndarray, fs: int = 48_000, block: int = 1024) -> np.ndarray:
        """controls: port name → value (unnamed control inputs get 0).
        audio: (n, inputs). Returns (n, outputs)."""
        unknown = set(controls) - set(self.ports)
        if unknown:
            raise KeyError(f"unknown ports: {sorted(unknown)[:5]}")
        desc = self._d.contents
        h = desc.instantiate(self._d, fs)
        keep = []
        ins = [i for i, k in enumerate(self.kinds) if k & AUDIO and k & INPUT]
        outs = [i for i, k in enumerate(self.kinds) if k & AUDIO and k & OUTPUT]
        for i, (name, k) in enumerate(zip(self.ports, self.kinds)):
            if k & CONTROL:
                v = C.c_float(float(controls.get(name, 0.0)))
                keep.append(v)
                desc.connect_port(h, i, C.pointer(v))
        b_in = [np.zeros(block, np.float32) for _ in ins]
        b_out = [np.zeros(block, np.float32) for _ in outs]
        for b, i in zip(b_in + b_out, ins + outs):
            desc.connect_port(h, i, b.ctypes.data_as(C.POINTER(C.c_float)))
        if desc.activate:
            desc.activate(h)
        n = audio.shape[0]
        out = np.zeros((n, len(outs)), np.float32)
        for s in range(0, n, block):
            m = min(block, n - s)
            for c, b in enumerate(b_in):
                b[:m] = audio[s:s + m, c]
            desc.run(h, m)
            for c, b in enumerate(b_out):
                out[s:s + m, c] = b[:m]
        if desc.deactivate:
            desc.deactivate(h)
        desc.cleanup(h)
        return out
