"""Pure builders for ``bakar build --on <host>`` remote dispatch.

This module holds the host-free primitives that construct the rsync
invocation, strip the ``--on`` dispatch option from the forwarded argv,
generate the fish-safe remote bash script, and guard the ``rsync --delete``
destination. Host orchestration (ssh/rsync subprocess, streaming, run-id
surfacing) lives in a later addition to this module; everything here is pure
and unit-testable without a live remote.

The remote command is delivered over ``ssh <host> bash -s`` stdin rather than
``ssh <host> '<cmd>'`` (the remote login shell is fish, where a bare
``NAME=value`` prefix silently fails) or ``ssh <host> bash -lc '<cmd>'`` (which
mangles argv via quote-loss). fish parses only the two tokens ``bash -s``; the
script body reaches bash unmodified.
"""

from __future__ import annotations

import shlex
from pathlib import Path

# Build artifacts and caches, never source. ``.git`` is deliberately absent:
# kas/bitbake read git state for SRCREV/AUTOREV. The NFS caches (sstate,
# downloads, ccache) live outside the workspace, but the workspace-local
# ``ccache/`` build dir and any stray tmp/downloads under a layer are excluded
# from both transfer and ``--delete``.
RSYNC_EXCLUDES: tuple[str, ...] = (
    "build-*/",
    "**/tmp/",
    "**/sstate-cache/",
    "**/downloads/",
    ".bakar/runs/",
    "ccache/",
    "**/.venv/",
    "**/__pycache__/",
    "**/*.pyc",
)


def build_rsync_argv(ws_root: Path, host: str, *, dry_run: bool = False) -> list[str]:
    """Construct the ``rsync`` argv mirroring ``ws_root`` to ``host``.

    Returns ``rsync -a --delete`` (plus ``-n -i`` when ``dry_run``) followed by
    one ``--exclude=<pat>`` per :data:`RSYNC_EXCLUDES` entry, then the source
    ``<ws_root>/`` and destination ``<host>:<ws_root>/`` (same absolute path,
    trailing slashes so directory contents map 1:1).
    """
    argv = ["rsync", "-a", "--delete"]
    if dry_run:
        argv += ["-n", "-i"]
    argv += [f"--exclude={pat}" for pat in RSYNC_EXCLUDES]
    argv += [f"{ws_root}/", f"{host}:{ws_root}/"]
    return argv


def strip_on_option(local_args: list[str]) -> list[str]:
    """Return ``local_args`` with the ``--on`` dispatch option removed.

    Handles both the two-token ``--on <host>`` form and the single-token
    ``--on=<host>`` form; every other token is left intact.
    """
    result: list[str] = []
    skip_next = False
    for arg in local_args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--on":
            skip_next = True
            continue
        if arg.startswith("--on="):
            continue
        result.append(arg)
    return result


def build_remote_script(remote_argv: list[str], cwd: Path, *, sccache_off: bool) -> str:
    """Generate the bash script fed to ``ssh <host> bash -s`` over stdin.

    The script changes into the invoking cwd (replicated on the identical-path
    remote) and ``exec``s ``env bakar <argv>``. When ``sccache_off`` is True the
    ``BAKAR_SCCACHE_DIST=0`` assignment is passed to ``env(1)`` so the remote
    build runs as an independent worker; when False the token is omitted and a
    forwarded ``--sccache-dist`` wins by CLI-over-env precedence.
    """
    env_tokens = ["env"]
    if sccache_off:
        env_tokens.append("BAKAR_SCCACHE_DIST=0")
    exec_line = "exec " + " ".join([*env_tokens, "bakar", shlex.join(remote_argv)])
    return f"cd {shlex.quote(str(cwd))}\n{exec_line}"


def assert_safe_workspace(ws_root: Path) -> None:
    """Guard the ``rsync --delete`` destination.

    Raises :class:`ValueError` when ``ws_root`` is empty, not absolute, or
    equals the home directory or the filesystem root, so a destructive mirror
    can never target ``~`` or ``/``.
    """
    if not str(ws_root).strip():
        raise ValueError("workspace root is empty")
    if not ws_root.is_absolute():
        raise ValueError(f"workspace root is not absolute: {ws_root}")
    if ws_root == Path(ws_root.anchor):
        raise ValueError(f"workspace root is the filesystem root: {ws_root}")
    if ws_root == Path.home():
        raise ValueError(f"workspace root is the home directory: {ws_root}")
