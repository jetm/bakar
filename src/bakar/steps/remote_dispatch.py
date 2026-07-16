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

import os
import re
import shlex
import subprocess
from collections import deque
from pathlib import Path

import typer
from rich.console import Console

# stderr so `bakar build --on <host> > log` keeps chrome (preview, prompts,
# run-id) out of the piped build log, matching the project convention in
# commands/_app.py.
console = Console(stderr=True)

# Build artifacts and caches, never source. ``.git`` is deliberately absent:
# kas/bitbake read git state for SRCREV/AUTOREV. The NFS caches (sstate,
# downloads, ccache) live outside the workspace. Workspace-root outputs are
# anchored with a leading ``/`` so an unanchored basename cannot also drop a
# real source dir (e.g. oe-core's ``meta/recipes-devtools/ccache/``). The
# ``**/`` patterns intentionally match at any depth.
RSYNC_EXCLUDES: tuple[str, ...] = (
    "/build/",
    "/build-*/",
    "/*/build/",
    "/ccache/",
    "**/tmp/",
    "**/sstate-cache/",
    "**/downloads/",
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

# A short-option cluster such as `-nky` (click splits it into `-n -k -y`).
_SHORT_CLUSTER_RE = re.compile(r"-[a-zA-Z]+")


def strip_dispatch_options(local_args: list[str]) -> list[str]:
    """Return ``local_args`` with the dispatch-only options removed.

    Strips ``--on <host>`` / ``--on=<host>`` (else the remote re-enters dispatch)
    and the confirm-gate bypass ``--yes`` / ``-y``; every other token is left
    intact so the remote build sees the same flag surface as the local one.

    Short-option clusters are handled too: ``-nky`` becomes ``-nk`` (the
    clustered ``y`` is dropped) so the bypass never rides to the remote inside a
    cluster. The stripper is position-blind by design: a literal ``--yes``/``-y``
    or a ``y``-bearing cluster appearing as another option's value is out of
    scope (no build option takes such a value today).
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
        if _SHORT_CLUSTER_RE.fullmatch(arg):
            stripped = "-" + arg[1:].replace("y", "")
            if stripped != "-":
                result.append(stripped)
            continue
        result.append(arg)
    return result


def build_remote_script(remote_argv: list[str], cwd: Path, env_vars: dict[str, str], *, sccache_off: bool) -> str:
    """Generate the bash script fed to ``ssh <host> bash -s`` over stdin.

    The script changes into the invoking cwd (replicated on the identical-path
    remote), echoes a machine-clock dispatch-start marker, and ``exec``s
    ``env <forwarded> bakar <argv>``. ``env_vars`` are the local ``BAKAR_*`` /
    ``KAS_*`` vars forwarded so the remote resolves the same build as the local
    one would; each is emitted sorted and shlex-quoted. When ``sccache_off`` is
    True the ``BAKAR_SCCACHE_DIST=0`` assignment is appended **last** so it wins
    over any forwarded ``BAKAR_SCCACHE_DIST`` (env(1) applies ``NAME=value``
    tokens left-to-right, last assignment wins); when False the token is omitted
    and a forwarded ``--sccache-dist`` wins by CLI-over-env precedence.
    """
    env_tokens = ["env"]
    env_tokens += [shlex.quote(f"{name}={env_vars[name]}") for name in sorted(env_vars)]
    if sccache_off:
        env_tokens.append("BAKAR_SCCACHE_DIST=0")
    exec_line = "exec " + " ".join([*env_tokens, "bakar", shlex.join(remote_argv)])
    # BAKAR_DISPATCH_START fences run-id discovery: a discovered run dir older
    # than this remote-clock timestamp predates the dispatch and is discarded.
    # `|| exit 1`: if the replicated cwd is missing on the remote, fail loudly
    # instead of silently running the build in $HOME (the wrong directory).
    return f'cd {shlex.quote(str(cwd))} || exit 1\necho "BAKAR_DISPATCH_START=$(date -u +%Y%m%d-%H%M%S)"\n{exec_line}'


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
_DISPATCH_START_RE = re.compile(r"BAKAR_DISPATCH_START=(\d{8}-\d{6})")


def preflight_remote(host: str) -> tuple[bool, str | None]:
    """Probe ``host`` over the same non-login bash the build itself uses.

    Runs ``command -v bakar && bakar --version`` via
    ``ssh -o BatchMode=yes <host> bash -s``. ``BatchMode=yes`` disables any
    interactive password/passphrase prompt, so an unreachable host or a missing
    key fails fast instead of blocking on input. Delivering the probe over the
    non-login bash (not the login fish, which sources config.fish) catches the
    case where ``bakar`` is on the interactive PATH but not on sshd's compiled
    default PATH - the PATH the build's ``bash -s`` actually sees.

    Returns ``(True, remote_version)`` when bakar is found, else
    ``(False, detail)`` where ``detail`` is a not-found hint or the captured ssh
    stderr for the caller to surface.
    """
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "bash", "-s"],
        input="command -v bakar >/dev/null 2>&1 || exit 127\nbakar --version\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 127:
        return False, (
            "bakar not found on the remote non-login PATH (uv-tool ~/.local/bin may be absent from ssh's PATH)"
        )
    if result.returncode != 0:
        return False, (result.stderr.strip() or None)
    return True, (result.stdout.strip() or None)


def _local_bakar_version() -> str | None:
    """Return the local ``bakar --version`` string, or None when it cannot run."""
    result = subprocess.run(["bakar", "--version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def confirm_destructive_sync(ws_root: Path, host: str, *, assume_yes: bool) -> bool:
    """Preview the ``rsync --delete`` and gate the real transfer behind a prompt.

    Runs the dry-run rsync (``build_rsync_argv(..., dry_run=True)``), then shows
    only the safety-relevant signal: the ``*deleting`` lines (bounded head with a
    ``(+N more)`` overflow count) plus a one-line create/update count. This human
    gate layers on top of :func:`assert_safe_workspace`: the deletions catch a
    wrong exclude set that would still pass the path guard before ``--delete``
    destroys remote data. Returns True immediately under ``assume_yes``, else the
    caller's prompt answer. A failed dry-run refuses the sync even under
    ``assume_yes`` (never run ``--delete`` blind).
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
        if preview.stderr.strip():
            console.print(preview.stderr.strip(), markup=False)
        return False
    lines = preview.stdout.splitlines()
    deletions = [ln for ln in lines if ln.startswith("*deleting")]
    other = [ln for ln in lines if ln and not ln.startswith("*deleting")]
    head_limit = 40
    if deletions:
        # markup=False: rsync -i itemized paths may contain '[' which Rich would
        # otherwise parse as markup and raise MarkupError on.
        for ln in deletions[:head_limit]:
            console.print(ln, markup=False)
        if len(deletions) > head_limit:
            console.print(f"(+{len(deletions) - head_limit} more deletions)")
    else:
        console.print("no deletions")
    console.print(f"{len(other)} files to create/update")
    if assume_yes:
        return True
    return typer.confirm(f"Mirror the workspace to {host} (rsync --delete)?")


def _stream_remote_build(host: str, script: str) -> tuple[int, list[str]]:
    """Feed ``script`` to ``ssh <host> bash -s`` over stdin, streaming stdout.

    Non-PTY ``Popen``: the script is written to stdin and closed, then stdout is
    read line-by-line and echoed live. The remote bakar sees a non-TTY and
    renders plain output. Returns the remote exit code and the captured lines
    (for run-id parsing). ``errors="replace"`` matches kas_build's decode
    convention so a non-UTF-8 byte in Yocto output cannot crash the stream.
    """
    proc = subprocess.Popen(
        ["ssh", host, "bash", "-s"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdin is not None and proc.stdout is not None  # PIPE is set above
    try:
        proc.stdin.write(script)
        proc.stdin.close()
    except BrokenPipeError:
        # ssh exited between preflight and the write (host rebooted, agent
        # expired): report cleanly instead of a raw traceback.
        console.print(f"[red]connection to {host} lost[/] before the build script was delivered.")
        return 255, []
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
        "-type d -path '*/build/runs/20*' -prune -printf '%T@ %p\\n' | sort -rn | head -1"
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
    :func:`_discover_newest_run_id`. A discovered id older than the streamed
    ``BAKAR_DISPATCH_START`` marker predates this dispatch (the build failed
    before creating its own run dir), so it is discarded rather than surfaced as
    a misleading stale id.
    """
    dispatch_start: str | None = None
    for line in captured:
        m = _DISPATCH_START_RE.search(line)
        if m:
            dispatch_start = m.group(1)
            break

    run_id: str | None = None
    if rc != 0:
        for line in captured:
            match = _RUN_ID_RE.search(line)
            if match:
                run_id = match.group(1).strip("`")
                break
    # Discovery is the fallback: on success the stream carries no run-id, and on
    # failure the triage-hint line can be lost to Rich's 80-col wrap on a
    # non-TTY. Fence the discovered id by the dispatch-start marker so a build
    # that failed before creating a run dir does not surface a previous run.
    if run_id is None:
        discovered = _discover_newest_run_id(host, ws_root)
        if discovered is not None and dispatch_start is not None and discovered < dispatch_start:
            discovered = None
        run_id = discovered

    if run_id:
        console.print(f"remote run-id: {run_id}")
        console.print(f"inspect the remote run: ssh {host} bakar triage {run_id}")
    else:
        console.print("no remote run dir was created - the build failed before starting")


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

    Each step gates the next: (1) reject a hyphen-prefixed host and guard the
    rsync destination; (2) preflight the host (reachable + bakar on the non-login
    PATH), aborting with NO rsync/build when it fails; (3) confirm the
    destructive sync, aborting before any real transfer when declined; (4) run
    the real ``rsync -a --delete``; (5) stream the remote build over
    ``ssh <host> bash -s`` stdin; (6) surface the run-id + triage command;
    (7) return the remote build's exit code.
    """
    if host.startswith("-"):
        console.print(
            f"[red]invalid host {host!r}[/]: must not begin with '-' (it would parse as an ssh/rsync option)."
        )
        return 1

    try:
        assert_safe_workspace(ws_root)
    except ValueError as exc:
        console.print(f"[red]unsafe workspace for remote sync:[/] {exc}")
        return 1

    ok, detail = preflight_remote(host)
    if not ok:
        console.print(
            f"[red]remote preflight failed for {host}[/] - check connectivity, ssh key auth, "
            f"and that a matching bakar is installed on the remote."
        )
        if detail:
            console.print(detail, markup=False)
        return 1
    local_ver = _local_bakar_version()
    if local_ver and detail and detail != local_ver:
        console.print(
            f"[yellow]bakar version mismatch[/]: local {local_ver!r} vs remote {detail!r} - "
            "forwarded flags may not be understood by the remote."
        )

    if not confirm_destructive_sync(ws_root, host, assume_yes=assume_yes):
        console.print("[yellow]remote sync declined; nothing transferred.[/]")
        return 1

    rsync_rc = subprocess.run(build_rsync_argv(ws_root, host), check=False).returncode
    if rsync_rc != 0:
        console.print(f"[red]rsync failed (exit {rsync_rc})[/]; remote build not started.")
        return rsync_rc

    env_vars = {k: v for k, v in os.environ.items() if k.startswith(("BAKAR_", "KAS_"))}
    script = build_remote_script(strip_dispatch_options(local_args), cwd, env_vars, sccache_off=not sccache_dist)
    try:
        rc, captured = _stream_remote_build(host, script)
    except KeyboardInterrupt:
        console.print("[yellow]Ctrl-C does not stop the remote build[/] - it keeps running on the host.")
        console.print(f"stop it:    ssh {host} bakar stop")
        console.print(f"triage it:  ssh {host} bakar triage <run-id>")
        return 130
    _surface_run_id(host, ws_root, captured, rc)
    return rc
