"""Shared logic for the ``dl_mirror_paths`` directories.

Parses the configured directory list and classifies each directory through
:func:`bakar.mounts.probe_statfs`. That bounded child-process probe is the
only filesystem contact in this module: an in-process ``stat`` against a hung
NFS share cannot be interrupted, so nothing here touches the filesystem
directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import mounts

if TYPE_CHECKING:
    from collections.abc import Sequence

# Per-process memo: each directory is probed at most once, so repeated callers
# (doctor, overlay build, prune) never pay the probe deadline twice for a share.
_PROBE_MEMO: dict[Path, DirStatus] = {}


@dataclass(frozen=True)
class DirStatus:
    """Readiness of one mirror directory.

    ``state`` is one of ``ready``, ``missing``, ``unresponsive``, ``error``;
    ``detail`` carries the probe's stderr text for ``missing``/``error``.
    """

    path: Path
    state: str
    detail: str


def parse_dirs(value: str | None) -> list[Path]:
    """Split ``value`` on whitespace into paths, keeping order and dropping duplicates."""
    if not value:
        return []
    return [Path(token) for token in dict.fromkeys(value.split())]


def probe_dirs(dirs: Sequence[Path]) -> list[DirStatus]:
    """Classify each directory in ``dirs``, probing each at most once per process.

    Directories not yet in the memo go into a single batch
    :func:`bakar.mounts.probe_statfs` call; a directory absent from its result
    counts as ``error``.
    """
    new = [d for d in dict.fromkeys(dirs) if d not in _PROBE_MEMO]
    if new:
        results = mounts.probe_statfs(new, deadline_s=mounts.CACHE_PROBE_DEADLINE_S)
        for d in new:
            state, detail = results.get(d, ("error", "no probe result"))
            _PROBE_MEMO[d] = DirStatus(path=d, state=state, detail=detail)
    return [_PROBE_MEMO[d] for d in dirs]


def _statuses(value: str | None, *, host_mode: bool) -> list[DirStatus]:
    if not host_mode:
        return []
    dirs = parse_dirs(value)
    if not dirs:
        return []
    return probe_dirs(dirs)


def ready_dirs(value: str | None, *, host_mode: bool) -> list[Path]:
    """Ready directories in configured order; ``[]`` without probing outside host mode."""
    return [s.path for s in _statuses(value, host_mode=host_mode) if s.state == "ready"]


def dropped_dirs(value: str | None, *, host_mode: bool) -> list[DirStatus]:
    """Not-ready directories in configured order; ``[]`` without probing outside host mode."""
    return [s for s in _statuses(value, host_mode=host_mode) if s.state != "ready"]
