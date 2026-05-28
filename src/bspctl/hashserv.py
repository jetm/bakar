"""Workspace-scoped bitbake-hashserv daemon helpers.

This module owns the lifecycle of a per-workspace ``bitbake-hashserv``
daemon. A persistent daemon lets cross-build sstate hash equivalence
accumulate instead of being rebuilt from scratch on every ``bspctl
build`` (which is what ``BB_HASHSERVE = "auto"`` does).

The daemon binary is sourced exclusively from the synced workspace at
``<bsp_root>/sources/poky/bitbake/bin/bitbake-hashserv``. We deliberately
do NOT fall back to a host PATH lookup: the daemon's wire protocol must
match the bitbake the build will run against, and the workspace bitbake
is the only version we can guarantee that for.
"""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_PID_FILENAME = "hashserv.pid"
_PORT_FILENAME = "hashserv.port"
_DB_FILENAME = "hashserv.db"
_STDERR_FILENAME = "hashserv.stderr"
_STATE_SUBDIR = ".bspctl"
_PORT_FLOOR = 49152
_PORT_SPAN = 16383
_TERM_GRACE_SECONDS = 5
_STARTUP_PROBE_DEADLINE_SECONDS = 2.0


def _workspace_port(bsp_root: Path) -> int:
    """Derive a stable ephemeral port from the workspace path.

    Two workspaces on the same machine must not collide; a random pick
    would need a port-file lookup to be authoritative. Hashing
    ``realpath(bsp_root)`` into the 49152-65534 range gives a stable URL
    for the lifetime of the workspace without making any state file
    load-bearing for routing.
    """
    digest = sha256(str(bsp_root.resolve()).encode()).hexdigest()
    return _PORT_FLOOR + int(digest[:8], 16) % _PORT_SPAN


def _find_binary(bsp_root: Path) -> Path | None:
    """Return the workspace ``bitbake-hashserv`` path, or None if absent.

    Only the workspace path is consulted. A host PATH fallback would
    pull in a binary whose hashserv wire protocol may not match the
    workspace bitbake the build will use, which silently corrupts the
    equivalence cache. When the workspace binary is missing, callers
    fall through to the overlay's ``auto`` fallback.
    """
    candidate = bsp_root / "sources" / "poky" / "bitbake" / "bin" / "bitbake-hashserv"
    if candidate.is_file():
        return candidate
    return None


def _state_dir(bsp_root: Path) -> Path:
    """Return ``<bsp_root>/.bspctl`` (the workspace state directory)."""
    return bsp_root / _STATE_SUBDIR
