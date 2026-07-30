"""ctypes bridge to the Mojo tokenizer and selector."""

from __future__ import annotations

import ctypes
import os
import subprocess

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.environ.get("MOJO_LXML_LIB") or os.path.join(
    ROOT, "dist", "libmojo-lxml.so"
)

I = ctypes.c_int64

_SIGNATURES = {
    "mlx_tokenize": ([I] * 12, I),
    "mlx_select": ([I] * 21, I),
}


class BuildError(RuntimeError):
    pass


def build(force: bool = False) -> str:
    source = os.path.join(ROOT, "src", "capi.mojo")
    if (
        not force
        and os.path.exists(LIB)
        and (not os.path.exists(source) or os.path.getmtime(LIB) >= os.path.getmtime(source))
    ):
        return LIB
    script = os.path.join(ROOT, "build", "build.sh")
    if not os.path.exists(script):
        raise BuildError(f"compiled library not found at {LIB}")
    proc = subprocess.run(
        ["bash", script], cwd=ROOT, capture_output=True, text=True, timeout=1800
    )
    if proc.returncode or not os.path.exists(LIB):
        raise BuildError((proc.stderr or proc.stdout).strip()[:4000])
    return LIB


_loaded = None


def lib() -> ctypes.CDLL:
    global _loaded
    if _loaded is None:
        _loaded = ctypes.CDLL(build())
        for name, (argtypes, restype) in _SIGNATURES.items():
            fn = getattr(_loaded, name)
            fn.argtypes = argtypes
            fn.restype = restype
    return _loaded


def addr(array: np.ndarray) -> int:
    if not isinstance(array, np.ndarray):
        raise TypeError("native buffers must be NumPy arrays")
    if array.dtype not in (np.dtype(np.uint8), np.dtype(np.int64), np.dtype(np.uint64)):
        raise TypeError(f"unsupported native buffer dtype: {array.dtype}")
    if not array.flags.c_contiguous or not array.flags.aligned:
        raise ValueError("native buffers must be aligned and C-contiguous")
    address = int(array.ctypes.data)
    if not address:
        raise ValueError("native buffers must have a non-null data pointer")
    return address


def i64(size: int, fill: int = 0) -> np.ndarray:
    return np.full(max(1, size), fill, dtype=np.int64)


def u64(values) -> np.ndarray:
    array = np.asarray(values, dtype=np.uint64)
    return array if array.size else np.zeros(1, dtype=np.uint64)
