"""Container-runtime control for the stop path.

Leaf module: imports nothing from the bakar package.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

_RUN_ID_LABEL_KEY = "bakar.run_id"

# Bound on every runtime CLI query (docker/podman ps|stop|kill|rm|inspect).
# These back the escalation ladder a capture timeout falls into - a wedged
# runtime daemon must not turn a bounded capture timeout into an unbounded
# hang at teardown. Generous: a query is normally sub-second; this is sized
# to catch a wedged daemon, not to cap ordinary runtime latency.
_RUNTIME_QUERY_TIMEOUT_S = 30.0

# Liveness tri-state. ``_ERROR`` is a query that could not be answered (a
# transient runtime failure), distinct from a definitive ``_DEAD``; the wait
# loop keeps polling on a single ``_ERROR`` and only concludes the runtime is
# gone after ``_RUNTIME_ERROR_CAP`` in a row.
_ALIVE = "alive"
_DEAD = "dead"
_ERROR = "error"


def run_id_label(run_id: str) -> str:
    """Return the container label (``bakar.run_id=<run_id>``) for a build run.

    Single source of truth shared by the launch-time ``--label`` injection in
    ``kas_build``, the recorded ``container_label``, and the
    ``docker|podman ps -f label=`` query, so the three never drift apart.
    """
    return f"{_RUN_ID_LABEL_KEY}={run_id}"


def detect_runtime() -> str:
    """Resolve the container runtime the way kas-container does.

    Honors ``KAS_CONTAINER_ENGINE`` when set (a name or a full path; only the
    basename matters), otherwise picks the first of ``docker``/``podman`` found
    on ``PATH``. Falls back to ``"docker"`` when neither is installed; the
    caller is responsible for handling an unresolvable runtime.
    """
    engine = os.environ.get("KAS_CONTAINER_ENGINE")
    if engine:
        return os.path.basename(engine.strip())
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return "docker"


# Old private name, still read by steps/kas_pty.py and tests/test_build_stop_container.py.
_detect_runtime = detect_runtime


def _container_id_status(runtime: str, container_label: str) -> tuple[str, str | None]:
    """Tri-state query backing :func:`_container_id` and :func:`escalate_container_tree`.

    Runs ``<runtime> ps -q -f label=<container_label>`` and returns
    ``(_ALIVE, cid)`` when a matching container is running, ``(_DEAD, None)``
    when the query succeeded and found none - a label matching nothing is not
    an error, ``docker ps`` returns a clean exit with empty output - and
    ``(_ERROR, None)`` when the query itself could not be trusted (the runtime
    binary is absent, the daemon is unreachable, it timed out, or it exited
    non-zero). ``_DEAD`` and ``_ERROR`` both read as "no id" to a caller that
    only wants the id, which is why :func:`_container_id` collapses them - but
    a caller deciding whether an escalation actually succeeded needs the
    difference: an unanswerable query is not proof the container is gone.
    """
    try:
        result = subprocess.run(
            [runtime, "ps", "-q", "-f", f"label={container_label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUNTIME_QUERY_TIMEOUT_S,
        )
    except OSError, subprocess.TimeoutExpired:
        return _ERROR, None
    if result.returncode != 0:
        return _ERROR, None
    for line in result.stdout.splitlines():
        cid = line.strip()
        if cid:
            return _ALIVE, cid
    return _DEAD, None


def _container_id(runtime: str, container_label: str) -> str | None:
    """Resolve the running container id for ``container_label`` via ``runtime``.

    Returns ``None`` when no container matches or the query itself failed
    (the container is gone or the runtime is unusable) - callers needing to
    tell those two apart use :func:`_container_id_status` directly.
    """
    _status, cid = _container_id_status(runtime, container_label)
    return cid


@dataclass(frozen=True)
class ContainerCandidate:
    """One running container discovered by :func:`discover_running_containers`.

    Group 10 only: the run id and container id as reported by the selected
    runtime, with no attempt yet at collapsing more than one container per
    run id (group 12's within-source dedup) or at resolving family/machine
    via mount inspection (group 15). Kept deliberately thin so those later
    groups can consume a list of these without this function knowing about
    either concern.
    """

    run_id: str
    container_id: str


def discover_running_containers(runtime: str) -> list[ContainerCandidate]:
    """Query ``runtime`` for every running container carrying a ``bakar.run_id`` label.

    Host-wide discovery for the listing command: unlike
    :func:`_container_id_status`, which asks "is THIS run id's container
    alive", this asks "which run ids have a live container at all" - so the
    filter is on the bare label KEY (``bakar.run_id``), not one specific
    ``label=bakar.run_id=<value>``. This is discovery, not confirmation: the
    caller does not know which run ids exist in advance.

    Queries only the single runtime ``detect_runtime`` selected - never a
    second runtime "just in case" a build happens to be running under the
    other one - and only currently-running containers (no ``-a``), so a
    stopped or exited container that still carries the label is correctly
    excluded rather than reported as live.

    Raises ``RuntimeError`` when the query itself could not be trusted (the
    runtime binary is absent, the daemon is unreachable, it timed out, or it
    exited non-zero) or its output could not be parsed. This function has no
    fallback of its own; group 11 wraps this call and degrades gracefully to
    host-mode-only results.
    """
    label_filter = f"label={_RUN_ID_LABEL_KEY}"
    format_template = '{{.ID}}\t{{.Label "' + _RUN_ID_LABEL_KEY + '"}}'
    try:
        result = subprocess.run(
            [runtime, "ps", "--filter", label_filter, "--format", format_template],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUNTIME_QUERY_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{runtime} ps query failed or timed out: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{runtime} ps exited {result.returncode}: {result.stderr.strip()}")

    candidates: list[ContainerCandidate] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            container_id, run_id = line.split("\t", 1)
        except ValueError as exc:
            raise RuntimeError(f"malformed {runtime} ps output line: {line!r}") from exc
        container_id = container_id.strip()
        run_id = run_id.strip()
        # A blank field here is unreachable in practice: `line` above is
        # already whole-line-stripped, so a genuinely blank container_id or
        # run_id (nothing but whitespace on one side of the tab) means that
        # side was pure whitespace in the raw line too - which the whole-line
        # strip already consumed, taking the tab itself with it and routing
        # through the ValueError branch above instead. This truthiness check
        # is therefore defense-in-depth for a shape the current two-field,
        # single-separator format cannot produce, not a silent-drop bug.
        if container_id and run_id:
            candidates.append(ContainerCandidate(run_id=run_id, container_id=container_id))
    return candidates


def dedup_container_candidates(candidates: list[ContainerCandidate]) -> list[ContainerCandidate]:
    """Collapse ``candidates`` to one entry per ``run_id`` (group 12 within-source dedup).

    A runtime reporting more than one running container for the same
    ``bakar.run_id`` label is an expected, documented case - not an anomaly -
    because this project's own build-launch code deliberately labels an
    auxiliary ``kas shell`` or timeout-escalation container with the same
    ``bakar.run_id`` as the main build container. When that happens, this
    keeps whichever candidate the runtime's query returned FIRST for that
    label: ``discover_running_containers`` preserves the runtime's own
    reported order, so first-seen-wins here is deterministic without needing
    to re-sort or otherwise second-guess that order.
    """
    seen: dict[str, ContainerCandidate] = {}
    for candidate in candidates:
        seen.setdefault(candidate.run_id, candidate)
    return list(seen.values())


def discover_running_containers_or_warn(runtime: str) -> tuple[list[ContainerCandidate], str | None]:
    """Wrap :func:`discover_running_containers`, degrading any failure to a warning.

    :func:`discover_running_containers` raises ``RuntimeError`` on any query
    failure by design (missing binary, unreachable/timed-out daemon,
    non-zero exit, or malformed output) - it has no fallback of its own and
    defers the degradation decision here. This wrapper catches that
    ``RuntimeError`` and collapses every failure mode to the same outcome:
    an empty candidate list plus a warning naming that container discovery
    specifically could not be completed, rather than a generic failure
    message or a raised exception the caller would need its own handler for.

    On success, returns ``(candidates, None)``. On failure, returns
    ``([], warning)`` where ``warning`` is a human-readable string - the
    caller (``bakar ps``, group 15) is responsible for emitting it, and must
    route it to stderr via ``_say`` or an equivalent diagnostic channel,
    never to stdout: stdout is reserved for the eventual ``--json`` payload,
    which must remain a well-formed result containing no warning text. This
    function does not print anything itself, so a caller building
    ``--json`` output never has a stray print to filter out.
    """
    try:
        return discover_running_containers(runtime), None
    except RuntimeError as exc:
        return [], f"container discovery could not be completed: {exc}"


# Runtime error text meaning the container is already gone or already stopped:
# the outcome the ladder wants, so it is not a failure to report.
_CONTAINER_GONE_MARKERS = (
    "no such container",
    "no container with name or id",
    "is not running",
    "can only kill running containers",
)


@dataclass(frozen=True)
class _RuntimeResult:
    """Outcome of one runtime subcommand: its argv, exit status and error text.

    ``returncode`` is ``None`` when the command produced no exit status (the
    runtime binary was absent, or the call timed out); ``detail`` then carries
    the OSError text or a timeout marker. Otherwise ``detail`` is the
    runtime's own stderr, stripped.
    """

    argv: list[str]
    returncode: int | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def already_gone(self) -> bool:
        """True when the runtime says the container no longer exists / is not running."""
        text = self.detail.lower()
        return any(marker in text for marker in _CONTAINER_GONE_MARKERS)

    def describe(self) -> str:
        status = "no exit status" if self.returncode is None else f"exit {self.returncode}"
        suffix = f": {self.detail}" if self.detail else ""
        return f"`{' '.join(self.argv)}` failed ({status}){suffix}"


def _run_runtime(args: list[str]) -> _RuntimeResult:
    """Run a runtime subcommand, capturing output; never raises.

    Returns the exit status and stderr text (or an OSError/timeout marker) so
    a caller can say WHY a command failed. A missing or already-gone container
    is not an error to the ladder: callers filter it with
    :attr:`_RuntimeResult.already_gone`.
    Bounded by ``_RUNTIME_QUERY_TIMEOUT_S``: a wedged runtime daemon must not
    turn this into an unbounded hang for a caller in the middle of its own
    timeout/escalation ladder.
    """
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False, timeout=_RUNTIME_QUERY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _RuntimeResult(args, None, f"timed out after {_RUNTIME_QUERY_TIMEOUT_S}s")
    except OSError as exc:
        return _RuntimeResult(args, None, str(exc))
    return _RuntimeResult(args, proc.returncode, (proc.stderr or "").strip())


def _container_liveness(runtime: str, cid: str) -> str:
    """Return the tri-state liveness of container ``cid``.

    This distinguishes a definitive not-running result from a query that
    could not be answered:

    - ``_ALIVE``  - ``inspect`` reported ``State.Running == true``;
    - ``_DEAD``   - ``inspect`` succeeded and the container is stopped/gone
      (``"false"`` or empty stdout with a clean exit);
    - ``_ERROR``  - the query itself failed (runtime binary absent, daemon
      unreachable, or a non-zero exit) so we cannot yet conclude the container
      drained. The wait loop treats this as "keep polling", not "drained".
    """
    try:
        result = subprocess.run(
            [runtime, "inspect", "-f", "{{.State.Running}}", cid],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUNTIME_QUERY_TIMEOUT_S,
        )
    except OSError, subprocess.TimeoutExpired:
        return _ERROR
    if result.returncode != 0:
        return _ERROR
    return _ALIVE if result.stdout.strip() == "true" else _DEAD


def _container_gone(runtime: str, cid: str) -> bool | None:
    """True when no container with id ``cid`` exists, False when one does, None when unknown.

    ``<runtime> ps -a -q --filter id=<cid>`` lists by id including stopped
    containers: a clean exit with empty output is conclusive, and unlike
    ``inspect`` it does not error for a container that ``--rm`` already removed.
    A runtime error, an absent binary or a timeout is ``None``, never ``True``.
    """
    try:
        result = subprocess.run(
            [runtime, "ps", "-a", "-q", "--filter", f"id={cid}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUNTIME_QUERY_TIMEOUT_S,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return not result.stdout.strip()


def _sigint_bitbake_in_container(runtime: str, cid: str) -> bool:
    """Send SIGINT to the main bitbake process INSIDE container ``cid``.

    The kas-container entrypoint runs under ``docker-init`` and does NOT forward
    SIGINT/SIGTERM to its bitbake child, so signalling the container's PID 1
    (``kill --signal=SIGINT <cid>``) never reaches the cooker. Exec into the
    container and signal the bitbake UI process directly so its handler runs the
    graceful "waiting for N running tasks to finish" shutdown.

    The ``bin/bitbake `` pattern (note the trailing space) matches the UI
    process cmdline (``.../bin/bitbake -c build ...``) but NOT ``bitbake-server``
    or ``bitbake-worker`` - signalling a worker would SIGINT its running compile
    and abort the task instead of letting it finish, which is the opposite of
    graceful (mirrors how a terminal Ctrl-C hits only the foreground bitbake,
    not the setsid'd task subprocess).

    Returns True when ``pkill`` signalled at least one process (exit 0), False
    when nothing matched, ``pkill`` is absent, or the exec errored.
    """
    try:
        result = subprocess.run(
            [runtime, "exec", cid, "pkill", "-INT", "-f", "bin/bitbake "],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUNTIME_QUERY_TIMEOUT_S,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _escalate_container(runtime: str, cid: str, term_secs: int) -> list[str]:
    """Force-stop container ``cid``: ``stop --timeout`` -> ``kill`` -> ``rm -f``.

    The final ``rm -f`` force-removes the container even when a wedged cooker
    inside ignored the stop/kill, so a subsequent build is not blocked by the
    old container still holding the build dir. It also terminates the host-side
    ``docker run`` (kas-container) client, which exits once its container is
    gone. Every step runs regardless of an earlier failure.

    Returns one description per failed command (exit status plus the runtime's
    own error text), so the caller can say why a container survived. A command
    whose error says the container is already gone or not running is the
    wanted outcome, not a failure, and is omitted. ``[]`` means no command
    failed; callers that only want the side effect may ignore the return."""
    failures: list[str] = []
    for args in (
        [runtime, "stop", f"--timeout={term_secs}", cid],
        [runtime, "kill", "--signal=SIGKILL", cid],
        [runtime, "rm", "-f", cid],
    ):
        result = _run_runtime(args)
        if not result.ok and not result.already_gone:
            failures.append(result.describe())
    return failures
