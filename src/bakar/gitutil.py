"""Shared bounded git invocation helper for best-effort read-only probes.

Several analysis probes (:mod:`bakar.layers`, :mod:`bakar.pin_state`,
:mod:`bakar.manifest_diff`) shell out to ``git`` for a single fact and treat
any failure as "unavailable". Left unbounded, a wedged or NFS-stalled checkout
turns one of those probes into an indefinite hang. :func:`run_git` collapses the
triplicated invocation shape into one place and applies the same ``timeout=5``
precedent already used by :func:`bakar.workspace._head_sha`.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def run_git(argv: list[str], *, timeout: float = 5) -> subprocess.CompletedProcess[str] | None:
    """Run a git command best-effort, returning None on failure or timeout.

    Wraps :func:`subprocess.run` with ``capture_output=True``, ``text=True`` and
    ``check=False``. Returns the completed process on success, or ``None`` when
    the binary is missing (:class:`OSError`) or the command exceeds ``timeout``
    seconds (:class:`subprocess.TimeoutExpired`).
    """
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except OSError, subprocess.TimeoutExpired:
        return None


def git_stdout(repo: Path, *args: str, timeout: float = 5, new_session: bool = False) -> str | None:
    """Run ``git -C <repo> <args>``; return stdout, or None on any failure.

    Failure covers a missing binary, a timeout and a non-zero exit, so callers
    can treat "git could not answer" as one outcome. ``new_session`` puts git
    in its own session so a timeout cannot leave a credential prompt attached
    to the caller's terminal.

    Output is decoded with ``errors="replace"``: git can emit raw filename
    bytes (``status --porcelain`` prints a non-UTF-8 name unquoted when
    ``core.quotePath`` is false, which a user's config may set; the default
    octal-quotes it into ASCII), and a strict decode would raise
    ``UnicodeDecodeError`` and turn a dirty tree into a
    crash. The callers only test emptiness or read a SHA, so a replacement
    character costs nothing and the "is it dirty" signal survives, where
    returning None would have hidden it.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=timeout,
            start_new_session=new_session,
        )
    except subprocess.SubprocessError, OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout
