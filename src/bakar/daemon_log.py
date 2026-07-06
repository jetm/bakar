"""Shared stderr-log plumbing for detached daemon spawns.

Both :mod:`bakar.central_service` (hashserv/prserv) and
:mod:`bakar.sccache_server` spawn a long-lived, detached daemon and want its
stderr captured to a state-dir file instead of discarded, so a daemon that
starts but crashes or never comes up leaves diagnostic output on disk. This
module holds that shared logic; each caller keeps its own module-level
``_STATE_DIR`` binding (tests monkeypatch it per-module) and passes it in.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# Default state dir for callers that don't override it with a workspace-scoped path.
STATE_DIR = Path.home() / ".local" / "state" / "bakar"


def stderr_log_path(binary: str, state_dir: Path) -> Path:
    """State-dir stderr log path for a central ``binary`` daemon."""
    return state_dir / f"{Path(binary).name}-central.stderr"


@contextmanager
def stderr_target(binary: str, state_dir: Path) -> Iterator[IO[bytes] | None]:
    """Best-effort open a state-dir stderr log file for a spawned ``binary``.

    Yields the open file handle, or ``None`` when the state dir can't be
    created or the log file can't be opened - a state-dir write failure must
    not block spawning the daemon itself. Always closes the handle on exit,
    including when the caller's ``Popen()`` raises, so a fork failure or a
    vanished binary never leaks the fd.
    """
    stderr_fh: IO[bytes] | None = None
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        stderr_fh = stderr_log_path(binary, state_dir).open("wb")
    except OSError:
        stderr_fh = None
    try:
        yield stderr_fh
    finally:
        if stderr_fh is not None:
            stderr_fh.close()
