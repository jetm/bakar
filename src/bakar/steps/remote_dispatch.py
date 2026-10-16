"""Builders and orchestration for ``bakar build --on <host>`` remote dispatch.

The pure section holds the host-free primitives that construct the additive
rsync invocation, strip the ``--on`` dispatch option from the forwarded argv,
generate the fish-safe remote bash script, and guard the mirror destination.
The orchestration section (host preflight, the remote workspace check, the
printed dispatch plan, rsync transfer, live remote-build streaming, and run-id
surfacing) drives ssh/rsync subprocesses; it is exercised with a mocked
``subprocess`` and no live remote. No dispatch deletes anything on the remote.

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
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from bakar.buildtools import parse_layerseries_corenames
from bakar.config import WORKSPACE_FEED_DIRNAME, WORKSPACE_FEED_STAGE_DIRNAME
from bakar.observability import RUN_ID_TS_FORMAT

if TYPE_CHECKING:
    from collections.abc import Mapping

# stderr so `bakar build --on <host> > log` keeps chrome (plan, run-id) out of
# the piped build log, matching the project convention in commands/_app.py.
console = Console(stderr=True)


def _echo(text: str) -> None:
    """Echo one chunk of the remote build's own output to the diagnostic stream.

    The streamed build output goes to the same stream as the chrome above: a kas
    build never writes its output to stdout, and a remote dispatch that did would
    make ``bakar build --on <host> > payload`` mean something different from the
    same redirect run locally. ``end=""`` because the lines arrive with their
    newline attached, and plain ``print`` rather than ``console`` because build
    output containing ``[`` must not be read as Rich markup.
    """
    print(text, end="", file=sys.stderr)


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
    # the local tree - so a mirror overwriting it costs the snapshots
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
    # the two-node cluster. Measured (under the former deleting mirror): with
    # `/_feed/` the symlink was removed and with `/_feed` it survived. A local
    # feed directory would still replace a remote symlink of the same name in an
    # additive mirror, so the unslashed form stays.
    f"/{WORKSPACE_FEED_DIRNAME}",
    f"/{WORKSPACE_FEED_STAGE_DIRNAME}",
)


# Keepalive and connect bounds for every ssh this module starts, so a dead link
# ends the call instead of hanging it. ServerAliveInterval x CountMax detects a
# dead peer after ~60s; ConnectTimeout bounds the TCP/handshake phase.
SSH_BOUND_OPTS = (
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
    "-o",
    "ConnectTimeout=15",
)

# Subprocess timeout for the short request/response probes. The long build-log
# stream deliberately has none: a healthy build runs for hours.
PROBE_TIMEOUT_SECONDS = 60

# rsync's own I/O stall bound, so a transfer over a dead link exits nonzero.
RSYNC_IO_TIMEOUT_SECONDS = 60

# The worktree ensure/delivery script can check out a large tree, so it gets
# ten minutes (git checkout of a big layer on a loaded host) instead of the probe bound.
DELIVERY_SCRIPT_TIMEOUT_SECONDS = 600


def _ssh_argv(host: str, *remote: str, batch_mode: bool = True) -> list[str]:
    """Build an ``ssh`` argv carrying :data:`SSH_BOUND_OPTS` (and BatchMode when asked)."""
    argv = ["ssh", *SSH_BOUND_OPTS]
    if batch_mode:
        argv += ["-o", "BatchMode=yes"]
    return [*argv, host, *remote]


def _unreachable(host: str, exc: subprocess.TimeoutExpired) -> str:
    return f"could not reach {host}: no response within {exc.timeout:g}s"


def build_rsync_argv(ws_root: Path, host: str) -> list[str]:
    """Construct the additive ``rsync`` argv mirroring ``ws_root`` to ``host``.

    Returns ``rsync -a`` followed by one ``--exclude=<pat>`` per
    :data:`RSYNC_EXCLUDES` entry, and finally the source ``<ws_root>/`` and
    destination ``<host>:<ws_root>/`` (same absolute path, trailing slashes so
    directory contents map 1:1).

    The mirror adds and updates, and never deletes: dispatch must leave every
    remote file that has no local counterpart in place, so no delete or
    remove-source option is ever emitted. A file removed locally lingers on the
    remote until someone removes it by hand.
    """
    argv = ["rsync", "-a", f"--timeout={RSYNC_IO_TIMEOUT_SECONDS}"]
    # A user's RSYNC_RSH is honoured: passing -e would override it.
    if not os.environ.get("RSYNC_RSH"):
        argv += ["-e", shlex.join(["ssh", *SSH_BOUND_OPTS])]
    argv += [f"--exclude={pat}" for pat in RSYNC_EXCLUDES]
    argv += [f"{ws_root}/", f"{host}:{ws_root}/"]
    return argv


# Version-mismatch override flags that exist only to drive `--on` dispatch and
# must never reach the remote build (an older remote bakar rejects `--yes`, and
# it is a no-op there in any case).
_DISPATCH_ONLY_FLAGS = frozenset({"--yes", "-y"})

# `bakar build`'s single-dash options, split by whether they take a value. Click
# reads a short-option token letter by letter: a flag lets the next letter be
# another option, while a value-taking letter swallows the rest of the token as
# its value. A test holds both sets to the real command, so a new short option
# cannot slip past the strippers unclassified.
BOOL_SHORT = frozenset("nky")
VALUE_SHORT = frozenset("mditfbw")


def strip_short_option(token: str, letter: str) -> tuple[str | None, bool]:
    """Remove short option ``letter`` from the single-dash ``token``.

    Returns ``(kept, skip_next)``: ``kept`` is the token to forward, or None when
    nothing of it remains, and ``skip_next`` is True when ``letter`` takes a
    value and that value is the next token. Only the letters before a
    value-taking one are options, so ``-tmyimage`` (target ``myimage``) is kept
    untouched while ``-ky`` loses its ``y``. A token that is not a single-dash
    option is returned unchanged.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return token, False
    kept: list[str] = []
    body = token[1:]
    for index, char in enumerate(body):
        if char == letter:
            if letter in VALUE_SHORT:
                # The rest of the token is this option's value, or the next
                # token is when nothing follows the letter.
                return (f"-{''.join(kept)}" if kept else None), not body[index + 1 :]
            continue
        if char in BOOL_SHORT:
            kept.append(char)
            continue
        # Another option's value starts here; nothing after it is an option.
        kept.append(body[index:])
        break
    return (f"-{''.join(kept)}" if kept else None), False


def strip_dispatch_options(local_args: list[str]) -> list[str]:
    """Return ``local_args`` with the dispatch-only options removed.

    Strips ``--on <host>`` / ``--on=<host>`` (else the remote re-enters dispatch)
    and the version-mismatch override ``--yes`` / ``-y``; every other token is
    left intact so the remote build sees the same flag surface as the local one.

    Short-option clusters are handled too: ``-nky`` becomes ``-nk`` so the
    override never rides to the remote inside a cluster, while ``-tmyimage``
    keeps its ``y`` because it is part of the target. The stripper is otherwise
    position-blind: a literal ``--yes``/``-y`` passed as the separate value of
    another option is out of scope (no build option takes such a value today).
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
        if arg.startswith("--on=") or arg in _DISPATCH_ONLY_FLAGS:
            continue
        kept, _ = strip_short_option(arg, "y")
        if kept is not None:
            result.append(kept)
    return result


# A `recipe:task` target whose task is a clean variant (do_clean, cleanall,
# cleansstate): bitbake deletes the recipe's output, downloads or sstate.
_CLEAN_TASK_RE = re.compile(r"(do_)?clean.*")
_BUILD_TASKS = frozenset({"", "build", "do_build"})


def deleting_requests(target: str | None, env: Mapping[str, str], *, image: str | None = None) -> list[str]:
    """Return each part of a build request that would delete on the remote, as shown to the user.

    kas runs ``KAS_TASK`` as ``bitbake -c $KAS_TASK``, and bakar passes no
    ``--task`` so kas reads it from the forwarded environment: anything but a
    plain build runs a task such as ``cleanall`` against the remote's shared
    download and sstate caches. A ``recipe:task`` target does the same through
    the target list, and every value that becomes a kas target is checked:
    ``--target``, ``--image`` (the manifest flow's generated kas file targets the
    image), the forwarded ``BAKAR_IMAGE`` and the space-separated ``KAS_TARGET``.

    A ``task:`` or ``target:`` written inside a kas file is not inspected: the
    file is user-authored and its clean task is as deliberate as running it by
    hand on the host.
    """
    found: list[str] = []
    task = env.get("KAS_TASK", "").strip()
    if task not in _BUILD_TASKS:
        found.append(f"KAS_TASK={task}")
    sources = (
        ("--target", target or ""),
        ("--image", image or ""),
        ("BAKAR_IMAGE", env.get("BAKAR_IMAGE", "")),
        ("KAS_TARGET", env.get("KAS_TARGET", "")),
    )
    # rsplit: a multiconfig target `mc:<name>:<recipe>:<task>` carries the task last.
    found.extend(
        f"{label} {item}"
        for label, raw in sources
        for item in raw.split()
        if ":" in item and _CLEAN_TASK_RE.fullmatch(item.rsplit(":", 1)[1])
    )
    return found


# Where a detached dispatch parks its log and exit-code sentinel. NOT the
# workspace: the next `--on` dispatch mirrors the workspace over it, so a log
# living there would be overwritten by the very next build - and `bakar stop --on`
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


@dataclass(frozen=True, kw_only=True, slots=True)
class RemoteScriptSpec:
    """The remote build command a dispatch script runs: argv, cwd, environment and guard."""

    remote_argv: list[str]
    cwd: Path
    env_vars: dict[str, str]
    sccache_off: bool
    guard: str | None = None


def build_remote_script(spec: RemoteScriptSpec, *, unit: str | None = None) -> str:
    """Generate the bash script fed to ``ssh <host> bash -s`` over stdin.

    ``spec.guard``, already shell-quoted, is a command prefix the build runs under
    (the delivered worktree's shared build lock).

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
    remote_argv, cwd, env_vars = spec.remote_argv, spec.cwd, spec.env_vars
    sccache_off, guard = spec.sccache_off, spec.guard
    unit = unit or dispatch_unit_name()
    log = remote_log_expr(unit)
    env_tokens = ["env"]
    env_tokens += [shlex.quote(f"{name}={env_vars[name]}") for name in sorted(env_vars)]
    if sccache_off:
        env_tokens.append("BAKAR_SCCACHE_DIST=0")
    build_cmd = " ".join([*([guard] if guard else []), *env_tokens, "bakar", shlex.join(remote_argv)])
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
            f'echo "BAKAR_DISPATCH_START=$(date +{RUN_ID_TS_FORMAT})"',
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
    """Guard the rsync mirror destination.

    Raises :class:`ValueError` when ``ws_root`` is empty, not absolute, or
    equals the home directory or the filesystem root, so a mirror can never
    target ``~`` or ``/``. The mirror is additive, but copying a whole workspace
    over a home directory still overwrites same-named files there.
    """
    if not str(ws_root).strip():
        raise ValueError("workspace root is empty")
    if not ws_root.is_absolute():
        raise ValueError(f"workspace root is not absolute: {ws_root}")
    # Resolve before the equality checks so a symlinked home or root
    # (e.g. /var/home/user -> /home/user) cannot slip past the guard.
    resolved = ws_root.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"workspace root is the filesystem root: {ws_root}")
    if resolved == Path.home().resolve():
        raise ValueError(f"workspace root is the home directory: {ws_root}")


_RUN_ID_RE = re.compile(r"bakar triage (\S+)")
_RUN_DIR_NAME_RE = re.compile(r"^\d{8}-\d{6}")
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

    ``bakar --version`` prints to stderr (its console is the diagnostic
    stream), so the probe folds it into stdout on the remote and keeps ssh's
    own warnings on stderr; stderr is still read when stdout is empty.
    Reading stdout alone saw no version on either side, and the mismatch check
    then passed every time.
    """
    try:
        result = subprocess.run(
            _ssh_argv(host, "bash", "-s"),
            input="command -v bakar >/dev/null 2>&1 || exit 127\nbakar --version 2>&1\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return False, _unreachable(host, exc)
    if result.returncode == 127:
        return False, (
            "bakar not found on the remote non-login PATH (uv-tool ~/.local/bin may be absent from ssh's PATH)"
        )
    if result.returncode != 0:
        return False, (result.stderr.strip() or None)
    return True, (result.stdout.strip() or result.stderr.strip() or None)


def _local_bakar_version() -> str | None:
    """Return the local ``bakar --version`` string, or None when it cannot run.

    Read from stderr when stdout is empty, for the reason :func:`preflight_remote` gives.
    """
    result = subprocess.run(["bakar", "--version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or result.stderr.strip() or None


class CaptureTimedOut(subprocess.CompletedProcess[str]):
    """The result :func:`_ssh_bash_capture` returns when its subprocess timed out.

    Exit status 255 and a "could not reach" stderr, so a caller that only checks
    the status behaves as before. ``timeout`` is the bound that elapsed.
    """

    timeout: float = 0.0


def _ssh_bash_capture(
    host: str, script: str, *, batch_mode: bool = True, timeout: float = PROBE_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` on ``host`` through ``bash -s`` and capture its output.

    Every remote payload in this module goes over stdin to ``bash -s`` rather
    than as an ``ssh <host> '<cmd>'`` argument, for the reason the module
    docstring gives: a command string is executed by the remote LOGIN shell,
    which is fish on the builder this is written against.

    That is not theoretical. ``_discover_newest_run_id``'s find - which uses
    ``-o``, ``-prune``, ``-printf`` and a pipe - produced NO output under fish
    while working under bash, so run-id discovery returned None and a build
    that had just succeeded was announced as "no remote run dir was created -
    the build failed before starting".

    Payloads that fish happens to parse identically today (a bare ``ls``, a
    bare ``systemctl``) are routed through here too, deliberately. Leaving them
    on the login-shell path keeps a standing trap where adding one operator to
    an existing command breaks it silently, and the breakage surfaces nowhere
    near the edit.
    """
    argv = _ssh_argv(host, "bash", "-s", batch_mode=batch_mode)
    try:
        return subprocess.run(argv, input=script, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Surface as a failed ssh so every caller's existing nonzero-exit path
        # handles it; 255 is ssh's own connection-failure code. The default
        # stderr says "could not reach", which is only true of a short probe;
        # a caller whose script ran on a host that had already answered tells
        # the two apart by the type and words the timeout itself.
        timed_out = CaptureTimedOut(argv, 255, stdout="", stderr=_unreachable(host, exc))
        timed_out.timeout = float(exc.timeout)
        return timed_out


@dataclass
class DispatchPlan:
    """What a dispatch is about to change on the remote host, printed before it does.

    ``deletes`` exists so the plan states the rule outright: no dispatch path ever
    fills it, and a test holds every plan dispatch builds to an empty list.
    """

    writes: list[str]
    deletes: list[str] = field(default_factory=list)


def print_dispatch_plan(host: str, plan: DispatchPlan) -> None:
    """Print ``plan`` for ``host``. There is no prompt: nothing in it destroys data."""
    console.print(f"[bold]dispatch plan[/] -> {host}")
    for write in plan.writes:
        # markup=False: paths may contain '[' which Rich would parse as markup.
        console.print(f"  + {write}", markup=False)
    for delete in plan.deletes:
        console.print(f"  - {delete}", markup=False)
    if not plan.deletes:
        console.print("  nothing is deleted on the host")


class DispatchError(Exception):
    """A dispatch pre-write check failed; the message is shown to the user as is."""


_WS_EXISTS_RE = re.compile(r"^BAKAR_WS_EXISTS=([01])$")
_WS_GIT_RE = re.compile(r"^BAKAR_WS_GIT=([01])$")
_WS_LAYERCONF_RE = re.compile(r"^BAKAR_WS_LAYERCONF=([01])$")
_OE_LAYER_CONF = "openembedded-core/meta/conf/layer.conf"


def build_workspace_check_script(workspace: str) -> str:
    """Return the read-only bash script :func:`check_remote_workspace` runs on the host.

    It prints one ``BAKAR_WS_*`` marker per check and then, when oe-core's
    layer.conf is readable, the file itself, so the series is parsed locally by
    the same parser the local release probe uses. Nothing in it writes.
    """
    q_ws = shlex.quote(workspace)
    return "\n".join(
        [
            f"ws={q_ws}",
            "if [ ! -d \"$ws\" ]; then printf 'BAKAR_WS_EXISTS=0\\n'; exit 0; fi",
            "printf 'BAKAR_WS_EXISTS=1\\n'",
            # The `.git` test keeps a plain directory inside an enclosing repository
            # from passing as a repository of its own.
            'if [ -e "$ws/meta-avocado/.git" ] && git -C "$ws/meta-avocado" rev-parse --git-dir >/dev/null 2>&1; then',
            "  printf 'BAKAR_WS_GIT=1\\n'",
            "else",
            "  printf 'BAKAR_WS_GIT=0\\n'",
            "fi",
            f'conf="$ws/{_OE_LAYER_CONF}"',
            'if [ -r "$conf" ]; then printf \'BAKAR_WS_LAYERCONF=1\\n\'; cat "$conf"; '
            "else printf 'BAKAR_WS_LAYERCONF=0\\n'; fi",
            "",
        ]
    )


def _missing_workspace_message(host: str, workspace: str, release: str) -> str:
    q_ws = shlex.quote(workspace)
    q_avocado = shlex.quote(f"{workspace}/meta-avocado")
    # The placeholder stays outside the quoting so it reads as one to replace.
    q_kas = shlex.quote(f"{workspace}/meta-avocado/kas/machine") + "/<machine>.yml"
    return (
        f"the release workspace {workspace} does not exist on {host}. Create it once on {host}:\n"
        f"  mkdir -p {q_ws}\n"
        f"  git clone --branch {shlex.quote(release)} <meta-avocado origin URL> {q_avocado}\n"
        f"  env KAS_WORK_DIR={q_ws} kas checkout {q_kas}"
    )


def check_remote_workspace(host: str, workspace: str, release: str) -> None:
    """Verify on ``host`` that ``workspace`` is a usable ``release`` workspace.

    One read-only ssh script reports whether the workspace exists, whether its
    ``meta-avocado`` is a git repository, and the text of its oe-core layer.conf;
    the series is parsed locally with :func:`buildtools.parse_layerseries_corenames`.
    Raises :class:`DispatchError` on the first failed check, so a caller stops
    before any remote write. ``--yes`` has no bearing on any of this.
    """
    result = _ssh_bash_capture(host, build_workspace_check_script(workspace))
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise DispatchError(
            f"could not inspect {workspace} on {host} (ssh exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    lines = result.stdout.splitlines()
    patterns = (_WS_EXISTS_RE, _WS_GIT_RE, _WS_LAYERCONF_RE)
    matches = [pattern.match(line) for pattern, line in zip(patterns, lines, strict=False)]
    if matches and matches[0] is not None and matches[0].group(1) == "0":
        raise DispatchError(_missing_workspace_message(host, workspace, release))
    if len(matches) < len(patterns) or any(m is None for m in matches):
        raise DispatchError(f"unexpected output inspecting {workspace} on {host}: {result.stdout.strip()[:200]!r}")
    _exists, is_git, has_conf = (m.group(1) for m in matches if m is not None)
    if is_git != "1":
        raise DispatchError(
            f"{workspace}/meta-avocado on {host} is missing or is not a git repository; "
            f"clone meta-avocado there at branch {release!r}"
        )
    if has_conf != "1":
        raise DispatchError(
            f"{workspace}/{_OE_LAYER_CONF} on {host} is missing or unreadable, so the workspace's release "
            f"cannot be checked; run `kas checkout` in that workspace first"
        )
    series = parse_layerseries_corenames("\n".join(lines[len(patterns) :]))
    if series is None:
        raise DispatchError(
            f"{workspace}/{_OE_LAYER_CONF} on {host} declares no LAYERSERIES_CORENAMES, "
            f"so it cannot be confirmed as a {release!r} workspace"
        )
    if series != release:
        raise DispatchError(
            f"release mismatch: the build needs {release!r} but {workspace} on {host} "
            f"declares {series!r} in {_OE_LAYER_CONF}"
        )


def _ssh_bash_popen(host: str) -> subprocess.Popen:
    """Open a non-PTY ``ssh <host> bash -s`` with stdin/stdout piped.

    The remote bakar sees a non-TTY and renders plain output. ``errors="replace"``
    matches kas_build's decode convention so a non-UTF-8 byte in Yocto output
    cannot crash the stream.
    """
    return subprocess.Popen(
        _ssh_argv(host, "bash", "-s", batch_mode=False),
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
            _echo(pending)
            captured.append(pending)
        pending = line
    if pending is not None:
        match = _DISPATCH_RC_RE.search(pending)
        if match:
            # Transport, not build output: consume it rather than echoing it.
            rc = int(match.group(1))
        elif not _DISPATCH_LOST_RE.search(pending):
            _echo(pending)
            captured.append(pending)
    proc.wait()
    if rc is None:
        return _FOLLOW_LOST_RC, list(captured), False
    return rc, list(captured), True


@dataclass(slots=True)
class _StreamState:
    """Whether the current dispatch announced a transient unit.

    Out-of-band because a Ctrl-C raises out of :func:`_stream_remote_build`
    before it can return anything, and the interrupt message must differ on it:
    a build detached into a unit survives the session, one that never got that
    far dies with it.
    """

    unit_seen: bool = False


_stream_state = _StreamState()


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
                if _DISPATCH_UNIT_RE.search(line):
                    _stream_state.unit_seen = True
                launch.append(line)
                continue
            if _DISPATCH_START_RE.search(line):
                launch.append(line)
                _echo(line)
                continue
            in_launch_phase = False
        _echo(line)
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


@dataclass(frozen=True, slots=True)
class RunLookupFailed:
    """The run-id lookup itself failed: ssh exit status and its first stderr line."""

    rc: int
    detail: str


def _discover_newest_run_id(host: str, ws_root: Path, not_before: str | None = None) -> str | RunLookupFailed | None:
    """Find the newest ``<build dir>/runs/<run-id>/`` dir under ``ws_root`` on ``host``.

    Returns the run id, ``None`` when the lookup ran and found no run directory
    (at or after ``not_before``, when given), or :class:`RunLookupFailed` when
    ssh itself failed (a hung link included: the capture helper's timeout
    surfaces as a nonzero exit, worded as a lookup timeout because the host
    answered the preflight).

    The success stream does not carry the run-id (RunLogger writes ``run_start``
    to events.jsonl only, observability.py:126-141), so discover it from the
    run directory names. Any build directory counts, not only ``build/``: qcom
    runs live under ``build-DISTRO/runs``.

    The pick is by RUN ID, not mtime: ids are ``%Y%m%d-%H%M%S-<pid>`` and sort
    chronologically, and ``not_before`` is the dispatch-start fence in the same
    clock. Newest-by-mtime then fence would let a build started later in another
    build dir win, and a touched older run dir (an mtime bump) shadow or evict
    the dir this dispatch created. Two runs in the same second compare by pid as
    a string, which is arbitrary but never crosses seconds.

    Every ``tmp*`` directory is pruned (qcom's TMPDIR is ``tmp-glibc``), so the
    walk never enters a bitbake work tree.
    """
    find_cmd = (
        f"find {shlex.quote(str(ws_root))} -mindepth 1 "
        "-type d -name 'tmp*' -prune -o "
        "-type d -name sstate-cache -prune -o "
        "-type d -name downloads -prune -o "
        "-type d -name .git -prune -o "
        "-type d -path '*/runs/20*' -prune -printf '%T@ %p\\n'"
    )
    result = _ssh_bash_capture(host, find_cmd)
    if isinstance(result, CaptureTimedOut):
        return RunLookupFailed(
            result.returncode, f"the run-id lookup timed out: no response within {result.timeout:g}s"
        )
    if result.returncode != 0:
        stderr_lines = result.stderr.strip().splitlines()
        return RunLookupFailed(result.returncode, stderr_lines[0] if stderr_lines else "")
    ids = [
        name
        for line in result.stdout.splitlines()
        if line.strip() and _RUN_DIR_NAME_RE.match(name := Path(line.split(maxsplit=1)[-1]).name)
    ]
    if not_before is not None:
        ids = [name for name in ids if name >= not_before]
    return max(ids, default=None)


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
    lookup_failed: RunLookupFailed | None = None
    if run_id is None:
        discovered = _discover_newest_run_id(host, ws_root, dispatch_start)
        if isinstance(discovered, RunLookupFailed):
            lookup_failed = discovered
            discovered = None
        run_id = discovered

    if run_id:
        console.print(f"remote run-id: {run_id}")
        console.print(f"inspect the remote run: ssh {host} bakar triage {run_id}")
    elif lookup_failed is not None:
        console.print(
            f"could not look up the remote run-id (ssh exit {lookup_failed.rc}): {lookup_failed.detail}",
            markup=False,
        )
    elif rc != 0:
        console.print("no remote run dir was created - the build failed before starting")
    else:
        console.print("the build succeeded but no remote run directory was found")


def check_versions(remote_version: str | None, *, assume_yes: bool) -> bool:
    """Refuse a remote whose bakar differs from the local one, unless ``assume_yes``."""
    local_ver = _local_bakar_version()
    if local_ver and remote_version and remote_version != local_ver:
        console.print(
            f"[red]bakar mismatch[/]: local {local_ver!r} vs remote {remote_version!r}. The remote would "
            "build with different bakar code/overlays (the parenthesised id is a content hash), so "
            "the result may not match a local build. Sync the remote bakar (git pull + "
            "uv tool install --force), or pass --yes to override."
        )
        if not assume_yes:
            return False
        console.print("[yellow]--yes: proceeding despite the bakar mismatch.[/]")
    return True


def preflight_or_report(host: str, *, assume_yes: bool) -> bool:
    """Run the host checks every dispatch shares, printing the reason when one refuses.

    The hyphen guard comes first and runs no subprocess: a host beginning with
    ``-`` would parse as an ssh or rsync option. Then :func:`preflight_remote`
    and :func:`check_versions`, the latter overridable by ``assume_yes``.
    """
    if host.startswith("-"):
        console.print(
            f"[red]invalid host {host!r}[/]: must not begin with '-' (it would parse as an ssh/rsync option)."
        )
        return False
    ok, detail = preflight_remote(host)
    if not ok:
        console.print(
            f"[red]remote preflight failed for {host}[/] - check connectivity, ssh key auth, "
            f"and that a matching bakar is installed on the remote."
        )
        if detail:
            console.print(detail, markup=False)
        return False
    return check_versions(detail, assume_yes=assume_yes)


def run_remote_build(host: str, script: str, run_root: Path) -> int:
    """Stream ``script`` on ``host`` and report the run id found under ``run_root``.

    Returns the remote exit code, 130 on Ctrl-C, or ``_DISPATCH_LOST_EXIT`` when
    the log stream is lost while the build keeps running.
    """
    _stream_state.unit_seen = False
    try:
        rc, captured, finished = _stream_remote_build(host, script)
    except KeyboardInterrupt:
        if _stream_state.unit_seen:
            # The unit marker is printed before systemd-run runs (the script ends in
            # `exec systemd-run`, so nothing can be printed after it succeeds), so
            # it means the unit was requested, not that it is running.
            console.print(
                "[yellow]Ctrl-C does not stop the remote build[/] - a unit was requested and, if it started, "
                f"it keeps running on the host. `bakar ps` / `bakar stop` on {host} show whether it is."
            )
            console.print(f"stop it:    bakar stop --on {host}  (or: ssh {host} bakar stop)")
            console.print(f"triage it:  ssh {host} bakar triage <run-id>")
        else:
            console.print(
                "[yellow]interrupted before the remote build detached[/] - the dispatch session was closed and the "
                "build was not started under a unit. If unsure whether anything is still running, check with "
                f"`bakar ps` / `bakar stop` on {host}."
            )
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
    _surface_run_id(host, run_root, captured, rc)
    return rc


@dataclass(frozen=True, kw_only=True, slots=True)
class MirrorRequest:
    """The local workspace, invoking cwd and argv a mirrored ``--on`` build replays remotely."""

    ws_root: Path
    cwd: Path
    local_args: list[str]


@dataclass(frozen=True, kw_only=True, slots=True)
class DispatchFlags:
    """The user's ``--sccache-dist`` and ``--yes`` choices for one dispatch."""

    sccache_dist: bool
    assume_yes: bool


def dispatch_remote_build(host: str, request: MirrorRequest, flags: DispatchFlags) -> int:
    """Mirror the workspace to ``host`` additively and run the build there, in strict order.

    Each step gates the next: (1) guard the rsync destination; (2) reject a
    hyphen-prefixed host, preflight the host (reachable + bakar on the non-login
    PATH) and abort when the remote bakar id/version differs from local unless
    ``flags.assume_yes`` overrides (:func:`preflight_or_report`), with NO
    rsync/build when any of them refuses; (3) print the dispatch plan, without a
    prompt; (4) run the additive ``rsync -a`` for ``request.ws_root``, which adds
    and updates remote files and never removes a path that exists only on the
    host; (5) stream the remote build over ``ssh <host> bash -s`` stdin;
    (6) surface the run-id + triage command; (7) return the remote build's exit
    code.
    """
    ws_root, cwd, local_args = request.ws_root, request.cwd, request.local_args
    sccache_dist, assume_yes = flags.sccache_dist, flags.assume_yes
    try:
        assert_safe_workspace(ws_root)
    except ValueError as exc:
        console.print(f"[red]unsafe workspace for remote sync:[/] {exc}")
        return 1

    if not preflight_or_report(host, assume_yes=assume_yes):
        return 1

    plan = DispatchPlan(
        writes=[
            f"{ws_root}/ -> {host}:{ws_root}/: adds and updates files; never removes a path that exists "
            "only on the host; a path whose type differs from the local one (file, directory, symlink) "
            "is replaced"
        ]
    )
    print_dispatch_plan(host, plan)

    # An argv list run without a shell, and ``preflight_or_report`` above already
    # refused a host that begins with ``-``, so the host cannot parse as an rsync
    # option. The directive must stay on the line directly above the call.
    # nosemgrep: python.django.security.injection.command.subprocess-injection.subprocess-injection
    rsync_rc = subprocess.run(build_rsync_argv(ws_root, host), check=False).returncode
    if rsync_rc != 0:
        console.print(f"[red]rsync failed (exit {rsync_rc})[/]; remote build not started.")
        return rsync_rc

    env_vars = {k: v for k, v in os.environ.items() if k.startswith(("BAKAR_", "KAS_"))}
    script = build_remote_script(
        RemoteScriptSpec(
            remote_argv=strip_dispatch_options(local_args), cwd=cwd, env_vars=env_vars, sccache_off=not sccache_dist
        )
    )
    return run_remote_build(host, script, ws_root)


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
    result = _ssh_bash_capture(host, cmd)
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
        _ssh_argv(host, "bash", "-s"),
        input=script,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        console.print(f"[red]stop failed on {host}[/] (exit {result.returncode}).")
        return False
    return True
