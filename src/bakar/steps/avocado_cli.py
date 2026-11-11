"""The one place bakar starts the avocado executable and writes overlay stanza text.

Keeping both here makes the public repository's coupling to avocado-cli a single
checkable module: every other module receives names and paths as parameters. The
wrapper never reads or writes the project's avocado configuration - the stanza is
printed for the user to paste, and the commands run with the project directory as
their working directory and nothing else.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

AVOCADO_COMMANDS: tuple[str, ...] = ("unlock", "install", "build")

_DEFAULT_TIMEOUT_S = 3600.0
_DEFAULT_GRACE_S = 10.0
_EXEC_FAILED_RC = 127


@dataclass(frozen=True)
class OverlayRepo:
    """One delta sub-repository as the project should list it."""

    name: str
    path: Path
    stages: tuple[str, ...]
    package_count: int


@dataclass(frozen=True)
class AvocadoRunResult:
    ok: bool
    failed_command: str | None = None
    returncode: int = 0
    timed_out: bool = False


def render_overlay_stanza(project_dir: Path, repos: Sequence[OverlayRepo]) -> str:
    """Return the ``repos:`` map (keyed by feed name) for the non-empty ``repos``, or ``""`` when none.

    Paths are relative to ``project_dir`` so the block stays valid when the
    project and the delta move together.
    """
    entries = [r for r in repos if r.package_count > 0]
    if not entries:
        return ""
    lines = ["repos:"]
    for repo in entries:
        rel = os.path.relpath(repo.path, project_dir)
        lines.append(f"  {repo.name}:")
        lines.append(f"    path: {rel}")
        lines.append(f"    stages: [{', '.join(repo.stages)}]")
    return "\n".join(lines) + "\n"


def command_lines() -> list[str]:
    """The commands as the user would type them, for the print-only path."""
    return [f"avocado {sub}" for sub in AVOCADO_COMMANDS]


def _terminate_group(proc: subprocess.Popen[bytes], grace: float) -> None:
    # The child leads its own session, so its group holds only itself and its
    # descendants; refuse to signal anything else.
    try:
        if os.getpgid(proc.pid) != proc.pid:
            return
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _run_one(sub: str, project_dir: Path, timeout: float, grace: float) -> tuple[int, bool]:
    try:
        proc = subprocess.Popen(
            ["avocado", sub],
            cwd=project_dir,
            start_new_session=True,
        )
    except OSError:
        return _EXEC_FAILED_RC, False
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        _terminate_group(proc, grace)
        return proc.returncode if proc.returncode is not None else -1, True


def run_avocado_commands(
    project_dir: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_S,
    grace: float = _DEFAULT_GRACE_S,
) -> AvocadoRunResult:
    """Run unlock, install and build in ``project_dir``, stopping at the first failure.

    ``timeout`` bounds each command, not the sequence. Output goes straight to
    the caller's terminal so long installs stay visible.
    """
    for sub in AVOCADO_COMMANDS:
        rc, timed_out = _run_one(sub, project_dir, timeout, grace)
        if timed_out or rc != 0:
            return AvocadoRunResult(ok=False, failed_command=sub, returncode=rc, timed_out=timed_out)
    return AvocadoRunResult(ok=True)
