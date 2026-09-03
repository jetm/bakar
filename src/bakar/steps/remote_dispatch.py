"""Builders and orchestration for ``bakar build --on <host>`` remote dispatch.

The pure section holds the host-free primitives that construct the rsync
invocation, strip the ``--on`` dispatch option from the forwarded argv,
generate the fish-safe remote bash script, and guard the ``rsync --delete``
destination. The orchestration section (host preflight, confirm gate, rsync
transfer, live remote-build streaming, and run-id surfacing) drives ssh/rsync
subprocesses; it is exercised with a mocked ``subprocess`` and no live remote.

EVERY generated script - the launch, the log follower and the stop ladder - is
delivered over ``ssh <host> bash -s`` stdin rather than ``ssh <host> '<cmd>'``
(the remote login shell is fish, where a bare ``NAME=value`` prefix silently
fails) or ``ssh <host> bash -lc '<cmd>'`` (which mangles argv via quote-loss).
fish parses only the two tokens ``bash -s``; the script body reaches bash
unmodified.

The argument form is not merely lossy, it is fatal, and worse than it looks:
fish validates the WHOLE buffer before running any of it, so a script carrying
one ``waited=0`` runs nothing at all - not even the lines above the offender.
The one exception here is :func:`_running_dispatch_units`, whose payload is a
single ``systemctl`` command that fish parses identically to bash; anything with
an assignment, a loop or a redirect must use the stdin form.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import subprocess
from collections import deque
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from bakar.config import WORKSPACE_FEED_DIRNAME, WORKSPACE_FEED_STAGE_DIRNAME

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
    # The package feed and its staging sibling. Unlike everything above these
    # are not caches: a feed accumulates across syncs, holds every retained
    # snapshot plus the content pool they reference, and cannot be rebuilt from
    # the local tree - so `--delete` destroying it costs the snapshots
    # themselves, not just the time to regenerate them. A remote feed is
    # reachable whenever someone runs `bakar feed sync` on a node that is also
    # an `--on` target, which is the ordinary way a second builder gets one.
    #
    # Anchored at the workspace root, matching the outputs above rather than the
    # `**/` patterns, because only the workspace-relative default lands inside
    # the mirror: a configured `feed_dir`, and the `feed_shared` XDG location,
    # both sit outside it and rsync never sees them. Anchoring also keeps a
    # source directory that merely shares the name deeper in a layer mirroring
    # normally.
    #
    # NO TRAILING SLASH, unlike every cache pattern above. A trailing slash makes
    # rsync match directories only, and a feed is routinely a SYMLINK onto a
    # storage volume rather than a real directory - which is how it is set up on
    # the two-node cluster. Measured: with `/_feed/` the symlink is deleted and
    # with `/_feed` it survives, while a real directory survives either way. The
    # slashed form would therefore have protected the case that does not occur
    # and missed the one that does.
    f"/{WORKSPACE_FEED_DIRNAME}",
    f"/{WORKSPACE_FEED_STAGE_DIRNAME}",
)


def build_rsync_argv(
    ws_root: Path, host: str, *, dry_run: bool = False, extra_excludes: tuple[str, ...] = ()
) -> list[str]:
    """Construct the ``rsync`` argv mirroring ``ws_root`` to ``host``.

    Returns ``rsync -a --delete`` (plus ``-n -i`` when ``dry_run``) followed by
    one ``--exclude=<pat>`` per :data:`RSYNC_EXCLUDES` entry, then one anchored
    ``--exclude=/<name>/`` per :paramref:`extra_excludes` entry (remote-only
    top-level dirs the local side does not carry, kept out of ``--delete`` so a
    remote checkout is preserved), and finally the source ``<ws_root>/`` and
    destination ``<host>:<ws_root>/`` (same absolute path, trailing slashes so
    directory contents map 1:1).
    """
    argv = ["rsync", "-a", "--delete"]
    if dry_run:
        argv += ["-n", "-i"]
    argv += [f"--exclude={pat}" for pat in RSYNC_EXCLUDES]
    argv += [f"--exclude=/{name}/" for name in extra_excludes]
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


# Where a detached dispatch parks its log and exit-code sentinel. NOT the
# workspace: the next `--on` dispatch mirrors it with `rsync --delete`, so a log
# living there would be destroyed by the very next build - and `bakar stop --on`
# has to find the sentinel of a build whose workspace has already moved on.
#
# NOT /tmp either, which is what this used to be. /tmp is drwxrwxrwt, the log
# path is disclosed in the transient unit's argv (world-readable via /proc), and
# the `.rc` sentinel does not exist until the build ENDS - so any other local
# user had the whole build duration to `printf '0\n' > /tmp/<name>.log.rc` and
# make a failed build report success, or to pre-place a symlink there and get a
# truncate primitive pointed anywhere the build user can write.
# $XDG_RUNTIME_DIR is 0700 and per-user, so neither is reachable.
#
# Its value is a property of the remote session, so this is a shell EXPRESSION
# the remote bash expands, not a path the dispatcher can compute. That costs
# nothing here: the detached branch is already gated on XDG_RUNTIME_DIR being
# non-empty (its systemd-run probe needs it), so it is guaranteed set wherever
# this is used, and the launch script reports the EXPANDED path back in its
# BAKAR_DISPATCH_LOG marker so the follower and `stop` resolve the same file.
_DISPATCH_LOG_DIR_EXPR = '"$XDG_RUNTIME_DIR"'


def remote_log_expr(unit: str) -> str:
    """Return the remote shell expression for ``unit``'s detached dispatch log.

    Derived from the unit name rather than echoed back and parsed, so the script
    that writes the log and the marker that reports it agree without a round
    trip. ``unit`` is quoted for the same reason its siblings in the generated
    script are: it is public API, and an unquoted interpolation into a `>`
    redirect is a shell-injection hole waiting for the first caller that passes
    something other than :func:`dispatch_unit_name`'s output.
    """
    return f"{_DISPATCH_LOG_DIR_EXPR}/{shlex.quote(f'{unit}.log')}"


# Session environment forwarded into the transient unit, beyond PATH. A
# transient unit inherits the USER MANAGER's environment, not this ssh session's,
# so the old coupled `exec` form carried these silently and the detached form
# dropped them just as silently: a `SRC_URI = "git://...;protocol=ssh"` fetch
# with an agent-forwarded key, or any fetch through a corporate proxy, breaks on
# the detached path ONLY - i.e. exactly on the hosts this feature targets.
#
# An allowlist rather than a wholesale copy of the ssh environment: the unit
# outlives the session, and dragging that session's whole environment into a
# long-lived unit is how a stale DISPLAY or SSH_CONNECTION ends up confusing a
# build hours after the session that set them is gone.
_FORWARDED_SESSION_VARS: tuple[str, ...] = (
    "SSH_AUTH_SOCK",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "LANG",
    "LC_ALL",
)


def dispatch_unit_name(now: datetime | None = None) -> str:
    """Return a fresh transient-unit name for one remote dispatch.

    The timestamp is the DISPATCHER's clock, unlike the ``BAKAR_DISPATCH_START``
    marker below: this string is never compared against a remote run-id, it only
    has to be unique per dispatch. The random suffix covers two dispatchers
    hitting the same host within the same second, which the timestamp alone
    would collide on.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"bakar-dispatch-{stamp}-{secrets.token_hex(3)}"


def build_remote_script(
    remote_argv: list[str],
    cwd: Path,
    env_vars: dict[str, str],
    *,
    sccache_off: bool,
    unit: str | None = None,
) -> str:
    """Generate the bash script fed to ``ssh <host> bash -s`` over stdin.

    The script changes into the invoking cwd (replicated on the identical-path
    remote), echoes a machine-clock dispatch-start marker, and runs
    ``env <forwarded> bakar <argv>``. ``env_vars`` are the local ``BAKAR_*`` /
    ``KAS_*`` vars forwarded so the remote resolves the same build as the local
    one would; each is emitted sorted and shlex-quoted. When ``sccache_off`` is
    True the ``BAKAR_SCCACHE_DIST=0`` assignment is appended **last** so it wins
    over any forwarded ``BAKAR_SCCACHE_DIST`` (env(1) applies ``NAME=value``
    tokens left-to-right, last assignment wins); when False the token is omitted
    and a forwarded ``--sccache-dist`` wins by CLI-over-env precedence.

    The build runs under a transient ``systemd-run --user`` unit, NOT as a child
    of the ssh session. As an ssh child it died with the session: when the local
    dispatcher was killed, sshd SIGHUP'd the session and took a 50-minute build
    down with it ("Keyboard Interrupt, closing down" then bitbake exit -15). A
    transient unit is owned by the remote user manager, so a dropped link, a
    local Ctrl-C or a reaped dispatcher costs the log stream and nothing else.

    Both forms are emitted and the REMOTE picks between them, because whether
    ``systemd-run --user`` works is a property of the remote host that the local
    side cannot answer. The availability probe mirrors
    :func:`bakar.build_scope.systemd_run_available` (binary, ``XDG_RUNTIME_DIR``,
    then a throwaway scope, since on WSL and in minimal containers the first two
    pass while ``--user`` cannot reach the manager bus); when it fails the script
    falls through to the original ``exec`` form rather than failing the dispatch.
    """
    unit = unit or dispatch_unit_name()
    log = remote_log_expr(unit)
    env_tokens = ["env"]
    env_tokens += [shlex.quote(f"{name}={env_vars[name]}") for name in sorted(env_vars)]
    if sccache_off:
        env_tokens.append("BAKAR_SCCACHE_DIST=0")
    build_cmd = " ".join([*env_tokens, "bakar", shlex.join(remote_argv)])
    exec_line = "exec " + build_cmd
    # The unit is `--collect`ed the moment it exits, so `systemctl show` cannot
    # be relied on to still carry ExecMainStatus - the wrapper writes the exit
    # code to a sentinel beside the log instead. The sentinel is also what tells
    # the local follower the build is over, so it must be written LAST, after
    # the log is complete.
    wrapped = f"{build_cmd} >{log} 2>&1; rc=$?; printf '%s\\n' \"$rc\" >{log}.rc; exit $rc"
    # The allowlist is materialised on the REMOTE, into an array, because the
    # values live there: `${!v}` reads the session's value and the `[ -n ]` guard
    # keeps an unset var from becoming an empty-string OVERRIDE, which is not the
    # same as leaving it alone (an empty https_proxy disables a proxy the manager
    # environment might otherwise have supplied). An array rather than a flat
    # string so a value containing a space stays one argv element.
    setenv_lines = [
        "  setenv=()",
        f"  for v in {' '.join(shlex.quote(name) for name in _FORWARDED_SESSION_VARS)}; do",
        '    if [ -n "${!v:-}" ]; then setenv+=("--setenv=$v=${!v}"); fi',
        "  done",
    ]
    detached = " ".join(
        [
            "exec systemd-run --user",
            f"--unit={shlex.quote(unit)}",
            "--collect",
            "--same-dir",
            "--quiet",
            # A transient unit inherits the USER MANAGER's environment, not this
            # ssh session's, and bakar is a uv tool on ~/.local/bin - absent from
            # the manager's PATH on a normal box. Hand over the session PATH,
            # which preflight_remote has already proven carries bakar.
            '--setenv=PATH="$PATH"',
            # The wrapper writes the log and the rc sentinel under
            # $XDG_RUNTIME_DIR, and it runs as the unit's own bash. Forward the
            # value explicitly rather than betting on the user manager carrying
            # it: the follower resolves the same path from the marker below, and
            # the two must not be able to disagree.
            '--setenv=XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"',
            '"${setenv[@]}"',
            "--",
            "bash",
            "-c",
            shlex.quote(wrapped),
        ]
    )
    # BAKAR_DISPATCH_START fences run-id discovery: a discovered run dir older
    # than this remote-clock timestamp predates the dispatch and is discarded.
    #
    # LOCAL time, deliberately - `date`, not `date -u`. The marker is only ever
    # string-compared against a run DIRECTORY NAME, and those come from
    # RunLog.run_id, which formats `datetime.now()` - the remote's local clock.
    # Reading UTC here compared two different clocks: at UTC-6 every run dir the
    # remote had just created sorted below the marker, so a running build was
    # discarded as stale and reported as "no remote run dir was created - the
    # build failed before starting". East of UTC it fails the other way and more
    # quietly, surfacing a genuinely stale run as this build's.
    #
    # `|| exit 1`: if the replicated cwd is missing on the remote, fail loudly
    # instead of silently running the build in $HOME (the wrong directory).
    #
    # The unit/log markers are echoed BEFORE the launch so the local side holds
    # them even when systemd-run itself then fails to start the unit.
    return "\n".join(
        [
            f"cd {shlex.quote(str(cwd))} || exit 1",
            'echo "BAKAR_DISPATCH_START=$(date +%Y%m%d-%H%M%S)"',
            'if command -v systemd-run >/dev/null 2>&1 && [ -n "${XDG_RUNTIME_DIR:-}" ] &&'
            " systemd-run --user --scope --quiet -- true >/dev/null 2>&1; then",
            # printf with a literal format, not `echo "...={unit}"`: inside double
            # quotes an interpolated unit carrying `$(...)` would be COMMAND
            # SUBSTITUTED on the remote. Only tests pass `unit` today, but it is
            # public API and every one of its siblings here is already quoted.
            f"  printf 'BAKAR_DISPATCH_UNIT=%s\\n' {shlex.quote(unit)}",
            # The log marker reports the EXPANDED path, so the follower and
            # `bakar stop --on` never have to re-derive $XDG_RUNTIME_DIR
            # themselves and cannot resolve a different file than the wrapper
            # writes.
            f"  printf 'BAKAR_DISPATCH_LOG=%s\\n' {log}",
            # Detaching only buys anything while the user manager lives. Without
            # `loginctl enable-linger` that manager is stopped when the user's
            # last session ends, taking its transient units - and the build -
            # with it, which is the failure this whole path exists to remove.
            # Report the condition rather than enabling linger unasked: that is a
            # persistent change to the remote host and nobody here can consent to
            # it. Reported, not fixed, so the caller can turn it into a hint.
            '  loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -qx yes ||'
            ' echo "BAKAR_DISPATCH_WARN=linger-disabled"',
            *setenv_lines,
            f"  {detached}",
            "fi",
            exec_line,
        ]
    )


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
# Anchored at line start, and deliberately not re.MULTILINE - the scanned stream
# IS the build's own output, so an unanchored `.search` let any build that
# happens to print `BAKAR_DISPATCH_RC=0` (a bitbake environment dump, a recipe
# that greps bakar's sources) forge a dispatch result. No attacker required.
# Anchoring is necessary but not sufficient: see the phase gate in
# :func:`_stream_remote_build` and the delay buffer in :func:`_follow_remote_log`.
_DISPATCH_START_RE = re.compile(r"^BAKAR_DISPATCH_START=(\d{8}-\d{6})")
_DISPATCH_UNIT_RE = re.compile(r"^BAKAR_DISPATCH_UNIT=(\S+)")
_DISPATCH_LOG_RE = re.compile(r"^BAKAR_DISPATCH_LOG=(\S+)")
_DISPATCH_RC_RE = re.compile(r"^BAKAR_DISPATCH_RC=(\d+)")
_DISPATCH_WARN_RE = re.compile(r"^BAKAR_DISPATCH_WARN=(\S+)")
# The follower could not read a usable exit status: no sentinel, an empty one, or
# one holding something that is not an integer. All three mean the same thing -
# the build's exit status is unknown - and none of them means the build failed.
_DISPATCH_LOST_RE = re.compile(r"^BAKAR_DISPATCH_LOST=")

# Follower loop tuning. The poll is coarse on purpose: it only decides how soon
# the follower notices the build is over, never how fast output arrives (that is
# tail -F's job), and a tight poll would spend an ssh round trip per second for
# the length of a Yocto build.
_FOLLOW_POLL_SECONDS = 5
_FOLLOW_SENTINEL_GRACE_SECONDS = 3
_FOLLOW_DRAIN_SECONDS = 1

# Consecutive failed liveness probes before the follower concludes the unit is
# gone. More than one because a single probe is not a verdict: the bus can hiccup,
# and the window between the unit exiting and the wrapper flushing the sentinel
# reads as "gone" exactly once on a perfectly healthy build.
_FOLLOW_LIVENESS_MISSES = 3

# Placeholder rc paired with ``finished=False`` out of :func:`_follow_remote_log`.
# It is never surfaced as an exit code - callers branch on ``finished`` - because
# a lost stream is not a build result and must not be indistinguishable from a
# build that genuinely exited 255.
_FOLLOW_LOST_RC = 255

# bakar's own exit code when the log stream was lost. Distinct from every code a
# remote build can produce (255 included), so a script wrapping
# `bakar build --on <host>` can tell "I do not know how the build ended, and it
# is still running" apart from "the build failed". 75 is sysexits' EX_TEMPFAIL:
# a temporary failure of the transport, retryable by re-attaching.
_DISPATCH_LOST_EXIT = 75


def _first_match(pattern: re.Pattern[str], lines: list[str]) -> str | None:
    """Return the first capture of ``pattern`` across ``lines``, or None."""
    for line in lines:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None


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


def _remote_child_dirs(host: str, ws_root: Path) -> list[str] | None:
    """List the immediate child directory names of ``ws_root`` on ``host``.

    Uses ``ssh -o BatchMode=yes <host> ls -1p <ws_root>`` - ``-p`` marks
    directories with a trailing ``/`` so files and symlinks are filtered out.
    ``BatchMode=yes`` keeps a missing key or unreachable host from blocking on a
    prompt. Returns the directory basenames, or None when the ssh listing fails
    so the caller can fall back to no extra excludes.
    """
    cmd = f"ls -1p {shlex.quote(str(ws_root))}"
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return [ln[:-1] for ln in result.stdout.splitlines() if ln.endswith("/")]


def _remote_only_dirs(ws_root: Path, host: str) -> list[str]:
    """Return top-level dir names present on the remote ``ws_root`` but absent locally.

    These are the remote node's own repo checkouts the local side does not carry;
    excluding them from ``rsync --delete`` keeps the mirror from wiping them. On
    any ssh listing failure returns an empty list (preserve the current no-extra-
    excludes behavior) after a warning - a failed listing must never crash the
    dispatch.
    """
    remote = _remote_child_dirs(host, ws_root)
    if remote is None:
        console.print(f"[yellow]could not list remote dirs on {host}[/]; proceeding without remote-only excludes.")
        return []
    try:
        local = {p.name for p in ws_root.iterdir() if p.is_dir()}
    except OSError:
        # A missing or unreadable local workspace must not crash the dispatch any
        # more than a failed remote listing does; fall back to no extra excludes
        # and let the rsync step surface the real workspace problem.
        console.print(f"[yellow]could not list local workspace {ws_root}[/]; proceeding without remote-only excludes.")
        return []
    return sorted(set(remote) - local)


def confirm_destructive_sync(
    ws_root: Path, host: str, *, assume_yes: bool, extra_excludes: tuple[str, ...] = ()
) -> bool:
    """Preview the ``rsync --delete`` and gate the real transfer behind a prompt.

    Runs the dry-run rsync (``build_rsync_argv(..., dry_run=True)``) with the same
    ``extra_excludes`` as the real transfer so the preview reflects what
    ``--delete`` will actually remove, then shows only the safety-relevant signal:
    the ``*deleting`` lines (bounded head with a ``(+N more)`` overflow count) plus
    a one-line create/update count. This human gate layers on top of
    :func:`assert_safe_workspace`: the deletions catch a wrong exclude set that
    would still pass the path guard before ``--delete`` destroys remote data.
    Returns True immediately under ``assume_yes``, else the caller's prompt answer.
    A failed dry-run refuses the sync even under ``assume_yes`` (never run
    ``--delete`` blind).
    """
    preview = subprocess.run(
        build_rsync_argv(ws_root, host, dry_run=True, extra_excludes=extra_excludes),
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


def _ssh_bash_popen(host: str) -> subprocess.Popen:
    """Open a non-PTY ``ssh <host> bash -s`` with stdin/stdout piped.

    The remote bakar sees a non-TTY and renders plain output. ``errors="replace"``
    matches kas_build's decode convention so a non-UTF-8 byte in Yocto output
    cannot crash the stream.
    """
    return subprocess.Popen(
        ["ssh", host, "bash", "-s"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def build_follow_script(unit: str, log: str) -> str:
    """Generate the bash script that tails a detached dispatch's log.

    Tailing is deliberately all this does: the follower owns no part of the
    build's lifecycle, so killing it (Ctrl-C, a dropped link, a reaped agent)
    costs the stream and leaves the transient unit running.

    ``tail -F`` rather than ``-f`` because the log may not exist yet when the
    follower attaches - systemd-run returns as soon as the unit is queued, a
    beat before the wrapper's first redirect creates the file.

    The loop ends on the rc sentinel, with the unit's own liveness as the
    backstop: a unit killed hard enough to skip the sentinel would otherwise
    leave the follower waiting forever on a file nobody will write. The grace
    sleep after that covers the window where the build has exited but the
    wrapper has not yet flushed the sentinel.

    That backstop carries the launch script's own guards, because it is the same
    probe: no ``XDG_RUNTIME_DIR`` and no reachable manager bus means the liveness
    signal cannot be READ, which is not the same as the unit being gone, and a
    follower that confuses the two declares a healthy build lost.
    """
    q_log = shlex.quote(log)
    q_unit = shlex.quote(f"{unit}.service")
    return "\n".join(
        [
            f"tail -n +1 -F {q_log} 2>/dev/null &",
            "tail_pid=$!",
            # When the bus is unreachable there is no liveness signal to read, so
            # trust the rc sentinel alone rather than a probe that fails for a
            # reason that has nothing to do with the build.
            "probe=1",
            '[ -n "${XDG_RUNTIME_DIR:-}" ] || probe=0',
            f"systemctl --user show -p ActiveState --value {q_unit} >/dev/null 2>&1 || probe=0",
            "misses=0",
            f"while [ ! -e {q_log}.rc ]; do",
            '  if [ "$probe" = 1 ]; then',
            f"    state=$(systemctl --user show -p ActiveState --value {q_unit} 2>/dev/null)",
            '    case "$state" in',
            # `activating` counts as live, exactly as _running_dispatch_units
            # already treats it: systemd-run returns when the job is ENQUEUED, so
            # the first poll routinely lands on a unit that has not reached
            # `active` yet. `is-active --quiet` calls that non-zero, which used to
            # declare a healthy build lost a few seconds after dispatch.
            "      active|activating|reloading|deactivating) misses=0 ;;",
            "      *) misses=$((misses+1)) ;;",
            "    esac",
            # One failed probe is not a verdict; the gap between the unit exiting
            # and the wrapper flushing the sentinel reads as "gone" exactly once.
            f'    if [ "$misses" -ge {_FOLLOW_LIVENESS_MISSES} ]; then',
            f"      sleep {_FOLLOW_SENTINEL_GRACE_SECONDS}",
            "      break",
            "    fi",
            "  fi",
            f"  sleep {_FOLLOW_POLL_SECONDS}",
            "done",
            # Let tail drain what the build wrote between the last poll and now.
            f"sleep {_FOLLOW_DRAIN_SECONDS}",
            "kill $tail_pid 2>/dev/null",
            "wait $tail_pid 2>/dev/null",
            f"rc=$(cat {q_log}.rc 2>/dev/null)",
            # A missing, empty or non-numeric sentinel all mean the same thing:
            # the build's exit status is unknown. Defaulting to a number here
            # reported "the build exited 255" for a sentinel that was merely
            # truncated, which is a build failure the user then went hunting for.
            'case "$rc" in',
            "  ''|*[!0-9]*) echo \"BAKAR_DISPATCH_LOST=1\" ;;",
            '  *) echo "BAKAR_DISPATCH_RC=$rc" ;;',
            "esac",
        ]
    )


def _follow_remote_log(host: str, unit: str, log: str) -> tuple[int, list[str], bool]:
    """Stream a detached dispatch's log back and return ``(rc, tail, finished)``.

    ``finished`` is False when the follower ended without the rc sentinel - a
    dropped link or a killed follower. That is not a build failure and must not
    be reported as one: the build is still running under its transient unit.
    """
    proc = _ssh_bash_popen(host)
    assert proc.stdin is not None and proc.stdout is not None  # PIPE is set above
    try:
        proc.stdin.write(build_follow_script(unit, log))
        proc.stdin.close()
    except BrokenPipeError:
        console.print(f"[red]connection to {host} lost[/] before the log follower was delivered.")
        return _FOLLOW_LOST_RC, [], False
    captured: deque[str] = deque(maxlen=200)
    rc: int | None = None
    # One-line delay buffer: only the stream's FINAL line is eligible to be the
    # rc sentinel, because the follow script echoes it last, after tail is dead.
    # The stream being scanned IS the build's log, so anchoring the pattern is
    # not enough on its own - a bitbake environment dump prints
    # `BAKAR_DISPATCH_RC=0` at column 0 and would otherwise forge a successful
    # result for a build that failed.
    pending: str | None = None
    for line in proc.stdout:
        if pending is not None:
            print(pending, end="")
            captured.append(pending)
        pending = line
    if pending is not None:
        match = _DISPATCH_RC_RE.search(pending)
        if match:
            # Transport, not build output: consume it rather than echoing it.
            rc = int(match.group(1))
        elif not _DISPATCH_LOST_RE.search(pending):
            print(pending, end="")
            captured.append(pending)
    proc.wait()
    if rc is None:
        return _FOLLOW_LOST_RC, list(captured), False
    return rc, list(captured), True


def _stream_remote_build(host: str, script: str) -> tuple[int, list[str], bool]:
    """Run the dispatch script on ``host`` and stream the build's output back.

    Which of the script's two forms the remote took decides what happens here,
    and the script says so: a ``BAKAR_DISPATCH_UNIT`` marker on the launch stream
    means the build detached into a transient unit, so its output arrives over a
    second ssh tailing the log (:func:`_follow_remote_log`) and its exit code
    over the rc sentinel. Without the marker the remote fell back to the coupled
    ``exec`` form and this stream IS the build's, read to completion as before.

    Returns ``(rc, captured, finished)``. ``finished`` is False only when the log
    stream was lost, i.e. ``rc`` is NOT the build's exit status; it is plumbed out
    rather than encoded in ``rc`` so a lost stream can never be mistaken for a
    build that genuinely exited with the same number.

    The launch lines are kept whole and prepended to the follower's bounded tail:
    the ``BAKAR_DISPATCH_START`` fence rides on the launch stream, and a shared
    bounded tail would evict it on any build long enough to matter. Only the
    launch PHASE feeds that list, so the fallback path - where this stream is the
    build's own, unbounded output - cannot grow it without limit.
    """
    proc = _ssh_bash_popen(host)
    assert proc.stdin is not None and proc.stdout is not None  # PIPE is set above
    try:
        proc.stdin.write(script)
        proc.stdin.close()
    except BrokenPipeError:
        # ssh exited between preflight and the write (host rebooted, agent
        # expired): report cleanly instead of a raw traceback. finished=True -
        # the build never started, so 255 IS the answer here, not a placeholder.
        console.print(f"[red]connection to {host} lost[/] before the build script was delivered.")
        return 255, [], True
    # Bounded: only the tail is needed (the `bakar triage <id>` hint rides near
    # the end on failure), so cap memory on a long/verbose Yocto build stream.
    captured: deque[str] = deque(maxlen=200)
    launch: list[str] = []
    # The launch markers are echoed consecutively at the head of the stream,
    # before anything else runs. Past the first line that is not one of them the
    # remote took the fallback `exec` form and THIS STREAM IS THE BUILD'S - where
    # a `BAKAR_DISPATCH_UNIT=x` line is build output, and honouring it would make
    # the code discard the real `proc.wait()` rc, announce a detach that never
    # happened, and follow a unit that does not exist.
    in_launch_phase = True
    for line in proc.stdout:
        if in_launch_phase:
            if _DISPATCH_UNIT_RE.search(line) or _DISPATCH_LOG_RE.search(line) or _DISPATCH_WARN_RE.search(line):
                # Transport, not build output.
                launch.append(line)
                continue
            if _DISPATCH_START_RE.search(line):
                launch.append(line)
                print(line, end="")
                continue
            in_launch_phase = False
        print(line, end="")
        captured.append(line)
    rc = proc.wait()

    unit = _first_match(_DISPATCH_UNIT_RE, launch)
    log = _first_match(_DISPATCH_LOG_RE, launch)
    if unit is None or log is None:
        return rc, [*launch, *captured], True
    if rc != 0:
        # The markers are echoed before systemd-run runs, so they arrive even
        # when the unit fails to start. Following a log that will never be
        # written would hang until the user gives up.
        console.print(f"[red]the remote build did not start on {host}[/] (launch exit {rc}).")
        return rc, [*launch, *captured], True

    console.print(f"remote build detached as [bold]{unit}[/] on {host}; following {log}")
    if _first_match(_DISPATCH_WARN_RE, launch) == "linger-disabled":
        console.print(
            f"[yellow]{host} has no linger enabled[/] - its user manager, and this build with it, "
            f"may be stopped when your last ssh session there closes.\n"
            f"fix it once:  ssh {host} loginctl enable-linger"
        )
    follow_rc, follow_lines, finished = _follow_remote_log(host, unit, log)
    if not finished:
        console.print(
            f"[yellow]lost the log stream from {host}[/] - the build keeps running under {unit}.\n"
            f"re-attach:  ssh {host} tail -F {log}\n"
            f"stop it:    bakar stop --on {host}"
        )
    return follow_rc, [*launch, *follow_lines], finished


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
    dispatch_start = _first_match(_DISPATCH_START_RE, captured)

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
    PATH), aborting with NO rsync/build when it fails; (2b) abort when the remote
    bakar id/version differs from local (a build with different code/overlays),
    unless ``assume_yes`` overrides; (3) confirm the destructive sync, aborting
    before any real transfer when declined; (4) run
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
            f"[red]bakar mismatch[/]: local {local_ver!r} vs remote {detail!r}. The remote would "
            "build with different bakar code/overlays (the parenthesised id is a content hash), so "
            "the result may not match a local build. Sync the remote bakar (git pull + "
            "uv tool install --force), or pass --yes to override."
        )
        if not assume_yes:
            return 1
        console.print("[yellow]--yes: proceeding despite the bakar mismatch.[/]")

    # Compute the remote-only top-level dirs BEFORE the preview so both the
    # dry-run and the real transfer keep them out of --delete (the remote node's
    # own repo checkouts must survive a mirror from a local side that lacks them).
    extra_excludes = tuple(_remote_only_dirs(ws_root, host))
    if extra_excludes:
        console.print(f"preserving {len(extra_excludes)} remote-only dir(s) from --delete: {', '.join(extra_excludes)}")

    if not confirm_destructive_sync(ws_root, host, assume_yes=assume_yes, extra_excludes=extra_excludes):
        console.print("[yellow]remote sync declined; nothing transferred.[/]")
        return 1

    rsync_rc = subprocess.run(build_rsync_argv(ws_root, host, extra_excludes=extra_excludes), check=False).returncode
    if rsync_rc != 0:
        console.print(f"[red]rsync failed (exit {rsync_rc})[/]; remote build not started.")
        return rsync_rc

    env_vars = {k: v for k, v in os.environ.items() if k.startswith(("BAKAR_", "KAS_"))}
    script = build_remote_script(strip_dispatch_options(local_args), cwd, env_vars, sccache_off=not sccache_dist)
    try:
        rc, captured, finished = _stream_remote_build(host, script)
    except KeyboardInterrupt:
        console.print("[yellow]Ctrl-C does not stop the remote build[/] - it keeps running on the host.")
        console.print(f"stop it:    bakar stop --on {host}  (or: ssh {host} bakar stop)")
        console.print(f"triage it:  ssh {host} bakar triage <run-id>")
        return 130
    if not finished:
        # A lost log stream is a transport failure, not a build result: the build
        # is still running under its unit and its exit status is unknown.
        # _surface_run_id would take its `rc != 0` branch here and narrate a
        # healthy in-flight build as "no remote run dir was created - the build
        # failed before starting", sending someone hunting an error that does not
        # exist. _stream_remote_build has already printed the re-attach/stop
        # advice, which is the only actionable thing there is to say.
        return _DISPATCH_LOST_EXIT
    _surface_run_id(host, ws_root, captured, rc)
    return rc


def _running_dispatch_units(host: str) -> list[str] | None:
    """Return the active detached dispatch units on ``host``, or None on failure.

    ``--all`` so a unit that has already exited is still listed, then filtered on
    the ACTIVE column: ``systemctl stop`` on a dead unit succeeds and would
    report a build stopped that nobody stopped.

    Delivered as an ssh ARGUMENT rather than over ``bash -s``, unlike every other
    script here, and safely so: the payload is a single command whose only
    metacharacters are the single quotes around the unit glob, which the remote
    login fish parses identically to bash. Anything carrying an assignment, a
    loop or a redirect must use the stdin form (see the module docstring).
    """
    cmd = "systemctl --user list-units --all --plain --no-legend 'bakar-dispatch-*.service'"
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        console.print(f"[red]could not query {host}[/] for detached builds.")
        if result.stderr.strip():
            console.print(result.stderr.strip(), markup=False)
        return None
    units: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        # UNIT LOAD ACTIVE SUB DESCRIPTION, with a leading bullet on a unit
        # systemd wants to draw attention to - split() leaves that as its own
        # field, so anchor on the unit name and read ACTIVE two fields along.
        # Positionally, not by scanning for the word: DESCRIPTION is the unit's
        # command line, which for a bakar dispatch carries the whole build
        # invocation and can contain "active" as a bare token.
        name = next((f for f in fields if f.endswith(".service")), None)
        if name is None:
            continue
        active = fields[fields.index(name) + 2 :][:1]
        # `activating` counts: a unit that is still starting is a build about to
        # run, and skipping it reports "nothing to stop" for a live dispatch.
        if active and active[0] in {"active", "activating"}:
            units.append(name)
    return units


def build_stop_units_script(units: list[str], *, force: bool, grace_seconds: float) -> str:
    """Generate the bash script that stops detached dispatch ``units``.

    Walks the same ladder as the local ``bakar stop``: SIGINT first, because
    bitbake drains its running tasks and writes its run log on it, and only
    ``systemctl stop`` (SIGTERM to the whole cgroup) once the grace period is
    spent. ``--force`` skips straight to the hard stop, which costs the run log.

    One script rather than a call per step: each step is an ssh round trip, and
    the grace wait belongs on the remote where the unit is - a local wait would
    keep stopping dependent on a link that may drop, which is the coupling this
    whole change exists to remove.

    ``grace_seconds`` of 0 or less waits unbounded, matching what ``--timeout 0``
    means for the local stop. A bounded loop with a 0 limit would mean the
    opposite - no wait at all - and hard-stop the build a moment after the SIGINT
    that was meant to let it drain.

    A FRACTIONAL grace still waits. The bound is counted in tenths of a second
    rather than seconds because ``int(0.5)`` is 0, which generated
    ``[ "$waited" -lt 0 ]`` - a loop that never runs, so ``--timeout 0.5`` skipped
    the graceful wait entirely and hard-stopped immediately: the exact behaviour
    documented for ``--timeout 0``, and the opposite of what a positive grace
    asks for. The poll step shrinks to match a sub-2s grace and stays at 2s above
    it, so the common 30s case keeps its original cadence and fork count.
    """
    lines = [f"for unit in {' '.join(shlex.quote(u) for u in units)}; do"]
    if not force:
        lines += ['  systemctl --user kill --signal=SIGINT "$unit" 2>/dev/null']
        if grace_seconds > 0:
            limit_tenths = max(1, round(grace_seconds * 10))
            step_tenths = min(20, limit_tenths)
            step = f"{step_tenths / 10:g}"
            lines += [
                "  waited=0",
                f'  while [ "$waited" -lt {limit_tenths} ] && systemctl --user is-active --quiet "$unit"; do',
                f"    sleep {step}",
                f"    waited=$((waited+{step_tenths}))",
                "  done",
            ]
        else:
            lines += [
                '  while systemctl --user is-active --quiet "$unit"; do',
                "    sleep 2",
                "  done",
            ]
    lines += [
        '  if systemctl --user is-active --quiet "$unit"; then',
        '    systemctl --user stop "$unit"',
        "  fi",
        "done",
    ]
    return "\n".join(lines)


def stop_remote_dispatch(host: str, *, force: bool, grace_seconds: float, stop_all: bool = False) -> bool:
    """Stop the detached build running on ``host``. Returns True if one was.

    A detached build with no kill path is its own trap: it outlives the terminal
    that started it by design, so the ordinary Ctrl-C no longer reaches it.

    Stops exactly ONE build unless ``stop_all``. A host that takes `--on`
    dispatches is by definition a shared builder, and every other running
    ``bakar-dispatch-*`` unit on it is somebody else's build - killing those
    without being asked destroys work nobody offered up. When more than one is
    running, they are listed and nothing is signalled, so the caller can name
    ``--all`` deliberately.

    Returns False when nothing was running, so the caller exits nonzero the same
    way a local ``bakar stop`` with no build does.
    """
    if host.startswith("-"):
        console.print(f"[red]invalid host {host!r}[/]: must not begin with '-' (it would parse as an ssh option).")
        return False

    units = _running_dispatch_units(host)
    if units is None:
        return False
    if not units:
        console.print(f"no detached bakar build is running on {host}.")
        return False
    if len(units) > 1 and not stop_all:
        console.print(f"[yellow]{len(units)} detached bakar builds are running on {host}[/]:")
        for name in units:
            console.print(f"  {name}")
        console.print(
            "refusing to stop more than one - on a shared builder the others are "
            f"someone else's in-flight build.\nstop them all:  bakar stop --on {host} --all"
        )
        return False

    console.print(f"stopping {len(units)} detached build(s) on {host}: {', '.join(units)}")
    script = build_stop_units_script(units, force=force, grace_seconds=grace_seconds)
    # Over `bash -s` stdin, NOT `ssh <host> <script>`. The remote login shell is
    # fish, so an argument form is run as `fish -c '<script>'`: `waited=0` is
    # "Unsupported use of '='" there, and fish validates the WHOLE buffer before
    # executing anything - so nothing ran at all, not even the SIGINT, and this
    # kill path was silently non-functional. Same reason the build path delivers
    # its script this way; see the module docstring.
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "bash", "-s"],
        input=script,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        console.print(f"[red]stop failed on {host}[/] (exit {result.returncode}).")
        return False
    return True
