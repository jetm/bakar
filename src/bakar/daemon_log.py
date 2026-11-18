"""Shared stderr-log plumbing for detached daemon spawns.

Both :mod:`bakar.central_service` (hashserv/prserv) and
:mod:`bakar.sccache_server` spawn a long-lived, detached daemon and want its
stderr captured to a state-dir file instead of discarded, so a daemon that
starts but crashes or never comes up leaves diagnostic output on disk. This
module holds that shared logic; each caller keeps its own module-level
``_STATE_DIR`` binding (tests monkeypatch it per-module) and passes it in.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

# Default state dir for callers that don't override it with a workspace-scoped path.
STATE_DIR = Path.home() / ".local" / "state" / "bakar"


def stderr_log_path(binary: str, state_dir: Path) -> Path:
    """State-dir stderr log path for a central ``binary`` daemon."""
    return state_dir / f"{Path(binary).name}-central.stderr"


@contextmanager
def stderr_target(binary: str, state_dir: Path, log_path: Path | None = None) -> Generator[IO[bytes] | None]:
    """Best-effort open a state-dir stderr log file for a spawned ``binary``.

    Yields the open file handle, or ``None`` when the state dir can't be
    created or the log file can't be opened - a state-dir write failure must
    not block spawning the daemon itself. Always closes the handle on exit,
    including when the caller's ``Popen()`` raises, so a fork failure or a
    vanished binary never leaks the fd.

    ``log_path`` overrides the derived ``<binary>-central.stderr`` name so the
    per-workspace spawns keep their ``hashserv.stderr`` / ``prserv.stderr``
    files; its parent directory is created when missing.
    """
    stderr_fh: IO[bytes] | None = None
    try:
        target = log_path if log_path is not None else stderr_log_path(binary, state_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        stderr_fh = target.open("wb")
    except OSError:
        stderr_fh = None
    try:
        yield stderr_fh
    finally:
        if stderr_fh is not None:
            stderr_fh.close()


def record_refusal(log_path: Path, state_dir: Path, text: str, *, append: bool = False) -> None:
    """Best-effort replace ``log_path`` with the single line ``bakar: <text>``.

    ``append=True`` adds the line after the existing content instead, for a log
    a live daemon is still writing to.

    The reason is a diagnostic: a failing write or close (full disk, quota,
    unwritable state dir) must not turn a documented no-URL fallback into an
    exception in the caller, so every ``OSError`` is swallowed.
    """
    line = f"bakar: {text}\n".encode()
    if append:
        with suppress(OSError):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as fh:
                fh.write(line)
        return
    with suppress(OSError), stderr_target("", state_dir, log_path=log_path) as fh:
        if fh is not None:
            fh.write(line)
