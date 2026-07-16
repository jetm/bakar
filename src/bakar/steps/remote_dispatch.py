"""Builders and orchestration for ``bakar build --on <host>`` remote dispatch.

The pure section holds the host-free primitives that construct the rsync
invocation, strip the ``--on`` dispatch option from the forwarded argv,
generate the fish-safe remote bash script, and guard the ``rsync --delete``
destination. The orchestration section (host preflight, confirm gate, rsync
transfer, live remote-build streaming, and run-id surfacing) drives ssh/rsync
subprocesses; it is exercised with a mocked ``subprocess`` and no live remote.

The remote command is delivered over ``ssh <host> bash -s`` stdin rather than
``ssh <host> '<cmd>'`` (the remote login shell is fish, where a bare
``NAME=value`` prefix silently fails) or ``ssh <host> bash -lc '<cmd>'`` (which
mangles argv via quote-loss). fish parses only the two tokens ``bash -s``; the
script body reaches bash unmodified.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections import deque
from pathlib import Path

import typer
from rich.console import Console

console = Console()

# Build artifacts and caches, never source. ``.git`` is deliberately absent:
# kas/bitbake read git state for SRCREV/AUTOREV. The NFS caches (sstate,
# downloads, ccache) live outside the workspace, but the workspace-local
# ``ccache/`` build dir and any stray tmp/downloads under a layer are excluded
# from both transfer and ``--delete``.
RSYNC_EXCLUDES: tuple[str, ...] = (
    "build-*/",
    "build/",
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


# Confirm-gate bypass flags that exist only to drive `--on` dispatch and must
# never reach the remote build (an older remote bakar rejects `--yes`, and it is
# a no-op there in any case).
_DISPATCH_ONLY_FLAGS = frozenset({"--yes", "-y"})


def strip_dispatch_options(local_args: list[str]) -> list[str]:
    """Return ``local_args`` with the dispatch-only options removed.

    Strips ``--on <host>`` / ``--on=<host>`` (else the remote re-enters dispatch)
    and the confirm-gate bypass ``--yes`` / ``-y``; every other token is left
    intact so the remote build sees the same flag surface as the local one.
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
        if arg in _DISPATCH_ONLY_FLAGS:
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
    # `|| exit 1`: if the replicated cwd is missing on the remote, fail loudly
    # instead of silently running the build in $HOME (the wrong directory).
    return f"cd {shlex.quote(str(cwd))} || exit 1\n{exec_line}"


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
    # Resolve before the equality checks so a symlinked home or root
    # (e.g. /var/home/user -> /home/user) cannot slip a --delete past the guard.
    resolved = ws_root.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"workspace root is the filesystem root: {ws_root}")
    if resolved == Path.home().resolve():
        raise ValueError(f"workspace root is the home directory: {ws_root}")


_RUN_ID_RE = re.compile(r"bakar triage (\S+)")


def check_host_reachable(host: str) -> bool:
    """Return True when ``ssh -o BatchMode=yes <host> true`` exits 0.

    ``BatchMode=yes`` disables any interactive password/passphrase prompt, so an
    unreachable host or a missing key fails fast instead of blocking on input.
    """
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "true"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def confirm_destructive_sync(ws_root: Path, host: str, *, assume_yes: bool) -> bool:
    """Preview the ``rsync --delete`` and gate the real transfer behind a prompt.

    Runs the dry-run rsync (``build_rsync_argv(..., dry_run=True)``), prints the
    itemized preview, then returns True immediately when ``assume_yes`` or the
    caller's answer otherwise. This human gate layers on top of
    :func:`assert_safe_workspace`: the preview catches a wrong exclude set that
    would still pass the path guard before ``--delete`` destroys remote data.
    """
    preview = subprocess.run(
        build_rsync_argv(ws_root, host, dry_run=True),
        capture_output=True,
        text=True,
        check=False,
    )
    console.print(f"[bold]rsync --delete preview[/] -> {host}:{ws_root}")
    if preview.returncode != 0:
        # A failed dry-run cannot show what --delete would remove; never run the
        # destructive mirror blind, even under --yes.
        console.print(f"[red]preview failed (rsync exit {preview.returncode})[/]; refusing the destructive sync.")
        return False
    if preview.stdout:
        # markup=False: rsync -i itemized paths may contain '[' which Rich would
        # otherwise parse as markup and raise MarkupError on.
        console.print(preview.stdout, end="", markup=False)
    if assume_yes:
        return True
    return typer.confirm(f"Mirror the workspace to {host} (rsync --delete)?")


def _stream_remote_build(host: str, script: str) -> tuple[int, list[str]]:
    """Feed ``script`` to ``ssh <host> bash -s`` over stdin, streaming stdout.

    Non-PTY ``Popen``: the script is written to stdin and closed, then stdout is
    read line-by-line and echoed live. The remote bakar sees a non-TTY and
    renders plain output. Returns the remote exit code and the captured lines
    (for run-id parsing).
    """
    proc = subprocess.Popen(
        ["ssh", host, "bash", "-s"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None  # PIPE is set above
    proc.stdin.write(script)
    proc.stdin.close()
    # Bounded: only the tail is needed (the `bakar triage <id>` hint rides near
    # the end on failure), so cap memory on a long/verbose Yocto build stream.
    captured: deque[str] = deque(maxlen=200)
    for line in proc.stdout:
        print(line, end="")
        captured.append(line)
    return proc.wait(), list(captured)


def _discover_newest_run_id(host: str, ws_root: Path) -> str | None:
    """Find the newest ``build/runs/<run-id>/`` dir under ``ws_root`` on ``host``.

    The success stream does not carry the run-id (RunLogger writes ``run_start``
    to events.jsonl only, observability.py:126-141), so discover it by mtime via
    a second ssh and return the basename.
    """
    find_cmd = (
        f"find {shlex.quote(str(ws_root))} "
        "-type d -name tmp -prune -o "
        "-type d -name sstate-cache -prune -o "
        "-type d -name downloads -prune -o "
        "-type d -name .git -prune -o "
        "-type d -path '*/build/runs/20*' -printf '%T@ %p\\n' | sort -rn | head -1"
    )
    result = subprocess.run(["ssh", host, find_cmd], capture_output=True, text=True, check=False)
    line = result.stdout.strip()
    if not line:
        return None
    return Path(line.split(maxsplit=1)[-1]).name


def _surface_run_id(host: str, ws_root: Path, captured: list[str], rc: int) -> None:
    """Print the remote run-id and a copy-pasteable ``bakar triage`` command.

    On failure the run-id rides in the stream (build.py:101 emits
    ``Run `bakar triage <id>` for details.``); on success it is discovered via
    :func:`_discover_newest_run_id`.
    """
    run_id: str | None = None
    if rc != 0:
        for line in captured:
            match = _RUN_ID_RE.search(line)
            if match:
                run_id = match.group(1).strip("`")
                break
    # Discovery is the universal fallback: on success the stream carries no
    # run-id, and on failure the triage-hint line can be lost to Rich's 80-col
    # wrap on a non-TTY. Either way the finished build wrote the newest run dir,
    # so recover the id from disk when the stream did not yield it.
    if run_id is None:
        run_id = _discover_newest_run_id(host, ws_root)
    if run_id:
        console.print(f"remote run-id: {run_id}")
        console.print(f"inspect the remote run: ssh {host} bakar triage {run_id}")


def dispatch_remote_build(  # noqa: PLR0913 - fixed dispatch signature consumed by the build command
    host: str,
    ws_root: Path,
    cwd: Path,
    local_args: list[str],
    *,
    sccache_dist: bool,
    assume_yes: bool,
) -> int:
    """Mirror the workspace to ``host`` and run the build there, in strict order.

    Each step gates the next: (1) guard the rsync destination; (2) preflight the
    host, aborting with NO rsync/build when unreachable; (3) confirm the
    destructive sync, aborting before any real transfer when declined; (4) run
    the real ``rsync -a --delete``; (5) stream the remote build over
    ``ssh <host> bash -s`` stdin; (6) surface the run-id + triage command;
    (7) return the remote build's exit code.
    """
    try:
        assert_safe_workspace(ws_root)
    except ValueError as exc:
        console.print(f"[red]unsafe workspace for remote sync:[/] {exc}")
        return 1

    if not check_host_reachable(host):
        console.print(
            f"[red]host {host} unreachable[/] - check connectivity and ssh key auth "
            f"(`ssh -o BatchMode=yes {host} true` must succeed)."
        )
        return 1

    if not confirm_destructive_sync(ws_root, host, assume_yes=assume_yes):
        console.print("[yellow]remote sync declined; nothing transferred.[/]")
        return 1

    rsync_rc = subprocess.run(build_rsync_argv(ws_root, host), check=False).returncode
    if rsync_rc != 0:
        console.print(f"[red]rsync failed (exit {rsync_rc})[/]; remote build not started.")
        return rsync_rc

    script = build_remote_script(strip_dispatch_options(local_args), cwd, sccache_off=not sccache_dist)
    rc, captured = _stream_remote_build(host, script)
    _surface_run_id(host, ws_root, captured, rc)
    return rc
