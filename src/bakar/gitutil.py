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
