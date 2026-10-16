"""Classify a shared workspace's pinned repositories for reuse by kas.

A release workspace is shared by every build that routes into it, so kas must
never move one of its clones. Each pinned repository is classified as
``frozen`` (already at its pin and clean: kas is told to leave it alone with
``url: null``), ``missing`` (kas clones it at its pin as usual) or ``side``
(on another commit or carrying local changes: kas is pointed at a separate
checkout under ``<workspace>/.bakar/pins/<name>/<commit12>`` instead).

Classification only ever runs ``git rev-parse HEAD`` and ``git status
--porcelain``, both with ``--no-optional-locks`` so not even the index stat
cache is refreshed.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

RepoStateKind = Literal["frozen", "missing", "side"]

_KAS_HEADER_VERSION = 16

# The holder runs a kas dump that may clone missing repositories, which on a
# slow link or a stalled fetch takes long; past this a waiter gives up loudly.
LOCK_WAIT_LIMIT_S = 20 * 60
_LOCK_POLL_S = 0.25
_LOCK_NOTICE_AFTER_S = 3.0


class RepoReuseError(Exception):
    """A shared repository cannot be reused without kas moving something."""


@dataclass(frozen=True)
class RepoState:
    name: str
    path: Path
    pin: str
    state: RepoStateKind
    side_path: Path | None = None
    side_exists: bool = False


@contextmanager
def workspace_lock(workspace: Path, *, wait_limit_s: float = LOCK_WAIT_LIMIT_S) -> Iterator[None]:
    """Hold the workspace's exclusive repository lock for the ``with`` body.

    Two builds that both classify a repository as ``missing`` would each let
    kas clone it, and the second kas would then check the first build's fresh
    clone out to its own pin. Serialising classify-through-dump closes that
    window: the second build classifies only after the first clone exists.

    The lock is polled rather than awaited so a waiter says why it is stuck
    and stops after ``wait_limit_s`` instead of hanging behind a holder whose
    clone is waiting on a credential prompt.

    Raises RuntimeError naming the lock file when the wait runs out.
    """
    lock_dir = workspace / ".bakar"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "repos.lock"
    with lock_path.open("a") as fh:
        start = time.monotonic()
        noticed = False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.monotonic() - start
                if waited >= wait_limit_s:
                    raise RuntimeError(
                        f"gave up after waiting {waited:.0f}s for {lock_path}: another bakar build on this "
                        "workspace still holds it (its kas dump may be stuck cloning a repository)"
                    ) from None
                if not noticed and waited >= _LOCK_NOTICE_AFTER_S:
                    print(
                        f"waiting for {lock_path}: another bakar build on this workspace holds it",
                        file=sys.stderr,
                    )
                    noticed = True
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _git_read(path: Path, *args: str) -> str | None:
    """Run a read-only git query in *path*; None when git fails."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _at_pin_and_clean(path: Path, pin: str) -> bool:
    head = _git_read(path, "rev-parse", "HEAD")
    if head is None or not head.strip().lower().startswith(pin.lower()):
        return False
    status = _git_read(path, "status", "--porcelain")
    return status is not None and status.strip() == ""


def _resolve_path(name: str, entry: dict, workspace: Path) -> Path:
    raw = entry.get("path") or name
    path = Path(raw)
    return path if path.is_absolute() else workspace / path


def classify(repos: dict, workspace: Path) -> list[RepoState]:
    """Classify every pinned repository in a kas ``repos`` mapping.

    Entries without a ``url`` are local path-only repositories kas never
    touches, so they are skipped. A ``url`` without a ``commit`` raises,
    because kas would move that clone to a branch tip.
    """
    states: list[RepoState] = []
    for name, raw_entry in (repos or {}).items():
        entry = raw_entry or {}
        if not entry.get("url"):
            continue
        pin = entry.get("commit")
        if not pin:
            raise RepoReuseError(
                f"repository {name!r} has a url but no commit pin; kas would move the shared clone to a branch tip"
            )
        pin = str(pin)
        path = _resolve_path(name, entry, workspace)
        if not path.is_dir():
            states.append(RepoState(name, path, pin, "missing"))
            continue
        if _at_pin_and_clean(path, pin):
            states.append(RepoState(name, path, pin, "frozen"))
            continue
        side_path = workspace / ".bakar" / "pins" / name / pin[:12]
        side_exists = False
        if side_path.exists():
            if not _at_pin_and_clean(side_path, pin):
                raise RepoReuseError(
                    f"side checkout {side_path} for {name!r} is not clean at "
                    f"pin {pin}; inspect it by hand, bakar never modifies it"
                )
            side_exists = True
        states.append(RepoState(name, path, pin, "side", side_path, side_exists))
    return states


def _kas_path(path: Path, workspace: Path) -> str:
    """Return ``path`` as the build's kas should name it.

    kas resolves a relative repo path against ``KAS_WORK_DIR``, which is the
    workspace on the host and ``/work`` inside kas-container, so a relative
    path names the same clone in both modes while a host-absolute one does
    not exist in the container. A repository outside the workspace has no
    such form and stays absolute.
    """
    # normpath first so a ``..`` component cannot pass the lexical check while
    # pointing outside; resolve second so a symlinked workspace still matches.
    lexical = (Path(os.path.normpath(workspace.absolute())), Path(os.path.normpath(path.absolute())))
    for base, target in (lexical, (workspace.resolve(), path.resolve())):
        if target.is_relative_to(base):
            return target.relative_to(base).as_posix()
    return str(path.absolute())


def reuse_overlay_text(states: list[RepoState], workspace: Path) -> str:
    """Render the kas overlay that keeps kas off every shared clone."""
    repos: dict[str, dict] = {}
    for st in states:
        if st.state == "frozen":
            # Without a path kas places a url-less repo at the dump file's
            # root, so name the clone classify actually checked.
            repos[st.name] = {"path": _kas_path(st.path, workspace), "url": None}
        elif st.state == "side" and st.side_path is not None:
            override: dict = {"path": _kas_path(st.side_path, workspace)}
            if st.side_exists:
                override["url"] = None
            repos[st.name] = override
    doc = {"header": {"version": _KAS_HEADER_VERSION}, "repos": repos}
    return yaml.safe_dump(doc, sort_keys=False)
