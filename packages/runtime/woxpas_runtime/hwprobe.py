"""Hardware probe — picks the best accelerator for inference once at startup.

ONNX path: detection is driven by which providers `onnxruntime` was built
with on this machine — no torch dependency.

Auto-detection precedence: CUDA → ROCm → CoreML (Apple Silicon) → CPU.
Honors WOXPAS env override (ACCEL_BACKEND=auto|cuda|metal|rocm|cpu).
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from enum import StrEnum

import structlog

log = structlog.get_logger(__name__)


class Backend(StrEnum):
    CUDA  = "cuda"
    ROCM  = "rocm"
    METAL = "metal"
    CPU   = "cpu"


@dataclass(slots=True, frozen=True)
class ConcurrencyDefaults:
    embed:     int
    chunk:     int
    summarize: int
    av:        int
    detect:    int


# CPU intra-op already saturates threads inside one ONNX call; concurrent embed
# calls would just thrash. GPUs have parallelism budget concurrent batches consume well.
_CONCURRENCY_BY_BACKEND: dict[Backend, ConcurrencyDefaults] = {}


@dataclass(slots=True, frozen=True)
class HardwareInfo:
    backend: Backend
    device_str: str
    onnx_providers: list[str]
    threads: int

    @property
    def concurrency_defaults(self) -> ConcurrencyDefaults:
        return _CONCURRENCY_BY_BACKEND.get(self.backend, _CONCURRENCY_BY_BACKEND[Backend.CPU])


_CONCURRENCY_BY_BACKEND.update({
    Backend.CPU:   ConcurrencyDefaults(embed=1, chunk=2, summarize=1, av=4, detect=8),
    Backend.METAL: ConcurrencyDefaults(embed=2, chunk=4, summarize=1, av=4, detect=8),
    Backend.CUDA:  ConcurrencyDefaults(embed=4, chunk=8, summarize=2, av=4, detect=8),
    Backend.ROCM:  ConcurrencyDefaults(embed=4, chunk=8, summarize=2, av=4, detect=8),
})


def _ort_providers() -> set[str]:
    try:
        import onnxruntime as ort
        return set(ort.get_available_providers())
    except Exception:
        return {"CPUExecutionProvider"}


def _has_cuda() -> bool:
    return "CUDAExecutionProvider" in _ort_providers()


def _has_rocm() -> bool:
    return "ROCMExecutionProvider" in _ort_providers()


def _has_coreml() -> bool:
    """Apple Silicon only. Intel Macs technically have CoreML but no usable GPU
    for ML; restrict to arm64 to avoid silent perf cliffs."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    return "CoreMLExecutionProvider" in _ort_providers()


def _cpu_threads() -> int:
    return os.cpu_count() or 4


def probe() -> HardwareInfo:
    requested = (os.environ.get("ACCEL_BACKEND") or "auto").lower()

    if requested in {b.value for b in Backend} and requested != "auto":
        backend = Backend(requested)
    else:
        if _has_cuda():
            backend = Backend.CUDA
        elif _has_rocm():
            backend = Backend.ROCM
        elif _has_coreml():
            backend = Backend.METAL
        else:
            backend = Backend.CPU

    device_str = {
        Backend.CUDA:  "cuda:0",
        Backend.ROCM:  "cuda:0",       # llama.cpp + onnxruntime both alias rocm under cuda label
        Backend.METAL: "mps",
        Backend.CPU:   "cpu",
    }[backend]

    onnx_providers = {
        Backend.CUDA:  ["CUDAExecutionProvider", "CPUExecutionProvider"],
        Backend.ROCM:  ["ROCMExecutionProvider", "CPUExecutionProvider"],
        Backend.METAL: ["CoreMLExecutionProvider", "CPUExecutionProvider"],
        Backend.CPU:   ["CPUExecutionProvider"],
    }[backend]

    info = HardwareInfo(
        backend=backend,
        device_str=device_str,
        onnx_providers=onnx_providers,
        threads=_cpu_threads(),
    )
    log.info("runtime.hwprobe", backend=info.backend.value,
             device=info.device_str, threads=info.threads,
             ort_providers=sorted(_ort_providers()))
    return info
