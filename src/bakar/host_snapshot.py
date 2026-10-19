"""Host measurement for ``bakar status --detail``.

Free space on the build and sstate filesystems, available memory and the
ten-second pressure averages, measured at query time on the host running the
caller. Every value degrades to None; :func:`measure` never raises.
"""

from __future__ import annotations

from pathlib import Path

from bakar import mounts, psi

MEMINFO_PATH = Path("/proc/meminfo")

_GIB = 1024**3


def _free_gb(build_path: Path | None, sstate_path: Path | None, deadline_s: float) -> dict[str, float | None]:
    out: dict[str, float | None] = {"build": None, "sstate": None}
    wanted = {k: p for k, p in (("build", build_path), ("sstate", sstate_path)) if p is not None}
    if not wanted:
        return out
    try:
        free = mounts.probe_free_bytes(list(wanted.values()), deadline_s=deadline_s)
    except Exception:  # noqa: BLE001 - measure() must never raise
        return out
    for key, path in wanted.items():
        try:
            value = free.get(path)
            out[key] = None if value is None else round(value / _GIB, 1)
        except Exception:  # noqa: BLE001 - measure() must never raise
            out[key] = None
    return out


def _mem_available_mb() -> int | None:
    try:
        text = MEMINFO_PATH.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            try:
                return int(parts[1]) // 1024
            except IndexError, ValueError:
                return None
    return None


def _pressure(resource: str) -> float | None:
    try:
        return psi.read_psi_avg10(resource)
    except Exception:  # noqa: BLE001 - measure() must never raise
        return None


def measure(build_path: Path | None, sstate_path: Path | None, *, deadline_s: float = 2.0) -> dict:
    """Return disk, memory and pressure readings; None for anything unavailable."""
    return {
        "disk_free_gb": _free_gb(build_path, sstate_path, deadline_s),
        "mem_available_mb": _mem_available_mb(),
        "pressure": {r: _pressure(r) for r in ("cpu", "io", "memory")},
    }
