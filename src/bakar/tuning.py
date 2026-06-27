"""Topology- and RAM-aware build-parallelism derivation.

When the user leaves ``[build] parallel_make`` / ``bb_number_threads`` unset,
bakar derives sensible figures from every performance input it can observe: the
local CPU count, host RAM, the active compiler launcher, and - for sccache-dist
- the remote cluster CPU count. :func:`derive_parallelism` is pure; the host
probes here read ``/proc/meminfo`` best-effort and never raise into a caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

# Estimated peak RAM per concurrent recipe. BB_NUMBER_THREADS is capped to
# floor(ram_gb / PER_TASK_GB) so a host does not OOM under heavy C++ links
# (webkit, nodejs, llvm), where a single recipe can hold several GB resident.
PER_TASK_GB = 4.0

# Indirected so a test can point the meminfo read at a fixture file.
_MEMINFO_PATH = Path("/proc/meminfo")

# Fallback when /proc/meminfo is unreadable: a conservative figure that keeps
# BB_NUMBER_THREADS modest rather than guessing high and risking an OOM.
_RAM_FALLBACK_GB = 16.0


@dataclass(frozen=True)
class ParallelismPlan:
    """Derived build-parallelism figures and a human rationale.

    ``parallel_make`` is the compile ``-j`` width (sized to the cluster under
    sccache-dist, else the local CPU count); ``bb_number_threads`` is the
    recipe-concurrency width (RAM-bound). ``rationale`` names the inputs that
    drove each number so the choice is auditable.
    """

    parallel_make: int
    bb_number_threads: int
    rationale: str


def derive_parallelism(
    *,
    nproc_local: int,
    ram_gb: float,
    launcher: str,
    cluster_cpus: int | None,
) -> ParallelismPlan:
    """Derive ``(PARALLEL_MAKE, BB_NUMBER_THREADS)`` from the perf inputs.

    ``launcher`` is one of ``"none"``, ``"ccache"``, ``"sccache-dist"``.

    PARALLEL_MAKE feeds all available compile slots: the remote+local cluster
    width under sccache-dist (when ``cluster_cpus`` is a positive int), else the
    local CPU count. BB_NUMBER_THREADS bounds concurrent recipes by RAM:
    ``min(nproc_local, floor(ram_gb / PER_TASK_GB))``, clamped to at least 1.
    """
    if launcher == "sccache-dist" and isinstance(cluster_cpus, int) and cluster_cpus > 0:
        parallel_make = cluster_cpus
        pm_reason = f"sccache-dist: cluster {cluster_cpus} cpus"
    else:
        parallel_make = nproc_local
        pm_reason = f"local nproc {nproc_local}"

    ram_threads = math.floor(ram_gb / PER_TASK_GB)
    bb_number_threads = max(1, min(nproc_local, ram_threads))

    rationale = (
        f"PARALLEL_MAKE={parallel_make} ({pm_reason}); "
        f"BB_NUMBER_THREADS={bb_number_threads} "
        f"(min(nproc={nproc_local}, ram {ram_gb:g}GB/{PER_TASK_GB:g}GB={ram_threads}))"
    )
    return ParallelismPlan(
        parallel_make=parallel_make,
        bb_number_threads=bb_number_threads,
        rationale=rationale,
    )


def host_ram_gb() -> float:
    """Return total host RAM in GB from ``/proc/meminfo`` ``MemTotal``.

    Mirrors :func:`bakar.diagnostics.check_memory`'s parsing. Returns
    :data:`_RAM_FALLBACK_GB` when the file is absent or the value is missing or
    non-integer, so a caller never has to guard the read.
    """
    try:
        text = _MEMINFO_PATH.read_text()
    except OSError:
        return _RAM_FALLBACK_GB
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            try:
                total_kb = int(line.split()[1])
            except IndexError, ValueError:
                return _RAM_FALLBACK_GB
            return total_kb / (1024 * 1024)
    return _RAM_FALLBACK_GB
