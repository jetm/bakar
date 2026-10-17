"""Per-run build-process lifecycle helpers.

``bakar build`` launches kas-container via ``subprocess.Popen`` with
``start_new_session=True``, so the kas-container process leads a new process
group. This module persists that PGID to a ``build.pid`` file in the run dir
so a separate ``bakar stop`` invocation can target the build precisely instead
of brute-forcing a ``pkill`` that risks hitting other workspaces' daemons.

Mirrors the procfs/PID-liveness pattern in :mod:`bakar.hashserv`: liveness via
``os.kill(pgid, 0)``, identity via a ``/proc/<pgid>/cmdline`` substring check.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess  # noqa: TC003
import sys
import time

# Runtime, not TYPE_CHECKING-guarded: both names annotate fields of
# ``_WaitCtx``, and a guarded import leaves ``get_type_hints`` on that
# dataclass raising ``NameError``. That is why the noqa sits here rather than
# the import moving back. ``Path`` and ``Console`` annotate fields too and are
# already imported at runtime above, so they need nothing here.
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from bakar import mounts, stop_container
from bakar.config import ResolveRequest, resolve
from bakar.eventlog import (
    IncrementalEventLog,
    RunningTask,
)
from bakar.observability import iter_run_events
from bakar.stop_container import (
    _ALIVE as _ALIVE,
)
from bakar.stop_container import (
    _DEAD as _DEAD,
)
from bakar.stop_container import (
    _ERROR as _ERROR,
)
from bakar.stop_container import (
    _RUNTIME_QUERY_TIMEOUT_S as _RUNTIME_QUERY_TIMEOUT_S,
)
from bakar.stop_container import (
    ContainerCandidate as ContainerCandidate,
)
from bakar.stop_container import (
    _container_id_status as _container_id_status,
)
from bakar.stop_container import (
    _container_liveness as _container_liveness,
)
from bakar.stop_container import (
    _detect_runtime as _detect_runtime,
)
from bakar.stop_container import (
    _escalate_container as _escalate_container,
)
from bakar.stop_container import (
    _sigint_bitbake_in_container as _sigint_bitbake_in_container,
)
from bakar.stop_container import (
    dedup_container_candidates as dedup_container_candidates,
)
from bakar.stop_container import (
    detect_runtime as detect_runtime,
)
from bakar.stop_container import (
    discover_running_containers as discover_running_containers,
)
from bakar.stop_container import (
    discover_running_containers_or_warn as discover_running_containers_or_warn,
)
from bakar.stop_container import (
    run_id_label as run_id_label,
)

if TYPE_CHECKING:
    from typing import Literal

    from bakar.config import BuildConfig
    from bakar.observability import RunLogger
    from bakar.user_config import UserConfig

_logger = logging.getLogger(__name__)

_PID_FILENAME = "build.pid"
_META_FILENAME = "build.meta.json"
_VALID_CMDLINE_TOKENS = ("kas-container", "kas")
_STOP_TERM_SECONDS = 5
_EVENTS_FILENAME = "events.jsonl"

# Stale bitbake artifacts left in the build TOPDIR by a forced/killed cooker.
# A stale ``bitbake.lock`` blocks the next build ("Cannot lock ... bitbake.lock").
# ``bitbake-cookerdaemon.log`` is a diagnostic log (like kas.log) and is kept.
_STALE_BITBAKE_FILES = ("bitbake.lock", "bitbake.sock", "hashserve.sock")

# Wait-loop tuning. The graceful wait is UNBOUNDED (no grace cap); these only
# govern the live-progress view and the runtime-death guard, never how long we
# are willing to wait for a build to drain.
_STOP_POLL_SECONDS = 1.0  # liveness/render cadence
_STOP_STALE_SECONDS = 10.0  # running-set unchanged this long -> spinner fallback
_STOP_HINT_SECONDS = 30.0  # cadence of the "press Ctrl-C to force" hint
_RUNTIME_ERROR_CAP = 5  # consecutive container-query errors before giving up

# Module-level Rich console for the out-of-process ``bakar stop`` wait view.
# build_stop sits below the commands tier, so it cannot import the shared
# console from ``commands._app``; it owns its own.
#
# stderr for the same reason ``commands/_app.py`` uses it, and that module
# carries the full rationale: stdout is reserved for machine-readable payloads,
# so everything a human reads goes here. Stop narration is diagnostics by
# definition, so it never belongs on stdout.
console = Console(stderr=True)


def _say(message: str) -> None:
    """Print one line of stop narration to the diagnostic stream.

    Every plain-text line this module emits goes through here rather than
    through a bare ``print``, so the stream choice above is made once instead of
    at thirty-odd call sites. Rich output keeps using ``console``; this is for
    the lines that must NOT be re-interpreted as markup - a signalled process's
    cmdline can contain ``[`` and would be eaten by ``console.print``.
    """
    print(message, file=sys.stderr)


def write_pid(run_dir: Path, pgid: int) -> None:
    """Write ``pgid`` as a single decimal line to ``run_dir/build.pid``."""
    (run_dir / _PID_FILENAME).write_text(f"{pgid}\n")


def remove_pid(run_dir: Path) -> None:
    """Remove ``run_dir/build.pid`` and ``build.meta.json``; no-op if absent."""
    (run_dir / _PID_FILENAME).unlink(missing_ok=True)
    (run_dir / _META_FILENAME).unlink(missing_ok=True)


@dataclass(frozen=True)
class LaunchRecord:
    """Describes how a build was launched, for ``bakar stop`` to target it.

    ``mode`` is ``"container"`` or ``"host"``. ``pgid`` is the recorded
    process-group id, or ``None`` for a missing run. ``runtime`` and
    ``container_label`` are container-targeting hints, both ``None`` when
    unknown (e.g. a host build or a legacy run with only ``build.pid``).
    """

    pgid: int | None
    mode: str
    runtime: str | None = None
    container_label: str | None = None


def write_launch_record(
    run_dir: Path,
    *,
    pgid: int,
    mode: str,
    runtime: str | None = None,
    container_label: str | None = None,
) -> None:
    """Write the ``build.meta.json`` sidecar and the ``build.pid`` back-compat file.

    The JSON sidecar records ``pgid``/``mode``/``runtime``/``container_label`` so
    ``bakar stop`` can target a container by label. ``write_pid`` is still called
    so a ``build.pid`` holding the PGID exists for back-compat with tooling that
    only knows about the pidfile.
    """
    payload = {
        "pgid": pgid,
        "mode": mode,
        "runtime": runtime,
        "container_label": container_label,
    }
    (run_dir / _META_FILENAME).write_text(json.dumps(payload) + "\n")
    write_pid(run_dir, pgid)


def read_launch_record(run_dir: Path) -> LaunchRecord:
    """Read the launch record for ``run_dir``, degrading gracefully.

    Resolution order:

    - If ``build.meta.json`` exists and parses, return its fields.
    - Else if ``build.pid`` exists (a legacy run predating the sidecar), return
      a ``"container"`` record with the PGID from the pidfile and no
      ``container_label`` (so ``stop_build`` can detect it cannot target it).
    - Else return ``pgid=None, mode="container", container_label=None``.

    Never raises on a missing or malformed run: an unparseable sidecar or
    pidfile degrades to the legacy/missing path rather than propagating.
    """
    meta_path = run_dir / _META_FILENAME
    try:
        raw = meta_path.read_text()
    except OSError:
        raw = None
    if raw is not None:
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            pgid = obj.get("pgid")
            mode = obj.get("mode")
            runtime = obj.get("runtime")
            container_label = obj.get("container_label")
            return LaunchRecord(
                pgid=pgid if isinstance(pgid, int) else None,
                mode=mode if isinstance(mode, str) else "container",
                runtime=runtime if isinstance(runtime, str) else None,
                container_label=container_label if isinstance(container_label, str) else None,
            )

    pid_file = run_dir / _PID_FILENAME
    try:
        pid_raw = pid_file.read_text()
    except OSError:
        return LaunchRecord(pgid=None, mode="container", container_label=None)
    try:
        legacy_pgid: int | None = int(pid_raw.strip())
    except ValueError:
        legacy_pgid = None
    return LaunchRecord(pgid=legacy_pgid, mode="container", container_label=None)


def _wait_sigint_handler(_signum: int, _frame: object) -> None:  # pragma: no cover - real signal path
    """Convert a SIGINT delivered during the wait into a ``KeyboardInterrupt``.

    Scoped to the wait loop only (installed/restored by :func:`_graceful_wait`)
    so a Ctrl-C escalates the stop instead of tearing the process down.
    """
    raise KeyboardInterrupt


def _render_running(out: Console, tasks: list[RunningTask], elapsed: float) -> None:
    """Render the live per-task progress view (bitbake is still draining)."""
    out.print(f"Waiting for {len(tasks)} running task(s) to finish (elapsed {elapsed:.0f}s)")
    now = time.time()
    for t in tasks:
        if t.started_epoch is None:
            per = ""
        else:
            per = f" {max(0.0, now - t.started_epoch):.0f}s"
        out.print(f"  {t.recipe}:{t.task}{per}")


def _render_spinner(out: Console, elapsed: float, target_desc: str, *, show_hint: bool) -> None:
    """Render the spinner fallback used when the event log is frozen/unavailable."""
    line = f"Waiting for build to finish (elapsed {elapsed:.0f}s)"
    if show_hint:
        line += f" - still waiting; press Ctrl-C to force [{target_desc}]"
    out.print(line)


@dataclass(frozen=True, kw_only=True)
class _WaitCtx:
    """The fourteen :func:`_graceful_wait` parameters, packed into one argument.

    Field names match the former keyword-only parameter names one-for-one, so a
    call site reads the same after the repack. A transposed or dropped field is
    caught by ``test_stop_container_wait_ctx_carries_every_field_unchanged``
    and by the one-at-a-time
    ``test_stop_container_wait_ctx_sets_only_the_none_field_passed``. Both
    drive the ``_stop_container`` call site; the host path at the second
    construction below has no equivalent field-level test.
    """

    liveness: Callable[[], str]
    escalate: Callable[[], None]
    target_desc: str
    run_dir: Path | None = None
    console_out: Console | None = None
    error_cap: int = _RUNTIME_ERROR_CAP
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    tasks_reader: Callable[[Path], list[RunningTask]] | None = None
    poll_interval: float = _STOP_POLL_SECONDS
    stale_after: float = _STOP_STALE_SECONDS
    hint_interval: float = _STOP_HINT_SECONDS
    install_signal: bool = True
    grace_seconds: float = 0


def _incremental_reader() -> Callable[[Path], list[RunningTask]]:
    """Return a per-wait running-task reader that decodes only appended log bytes.

    One ``IncrementalEventLog`` is held per wait (rebuilt if the run dir changes),
    replacing a full ``normalize`` of the event log on every poll. Its task list
    is selected the same way ``eventlog.running_tasks`` selects it.
    """
    log: IncrementalEventLog | None = None
    log_dir: Path | None = None

    def read(run_dir: Path) -> list[RunningTask]:
        nonlocal log, log_dir
        if log is None or log_dir != run_dir:
            log = IncrementalEventLog(run_dir / "bitbake_eventlog.json")
            log_dir = run_dir
        try:
            log.poll()
            return log.running_tasks()
        except Exception:  # noqa: BLE001 - a poll mid-shutdown must degrade to no tasks, as running_tasks does
            return []

    return read


def _graceful_wait(*, ctx: _WaitCtx) -> str:
    """Wait until ``ctx.liveness()`` says the target is gone, or the grace elapses.

    The exit gate is liveness, never ``tasks == 0``: bitbake may still finalize
    (sstate writes, cooker shutdown) after the last task drains, so only a
    ``_DEAD`` liveness result ends the wait on its own. Returns one of:

    - ``"drained"``      - ``liveness()`` returned ``_DEAD``;
    - ``"escalated"``    - a Ctrl-C, or ``grace_seconds`` elapsing, fired the
      SIGTERM->SIGKILL ``escalate()`` ladder;
    - ``"lost_runtime"`` - ``error_cap`` consecutive ``_ERROR`` liveness queries
      (container runtime unreachable); the caller should exit 1.

    A single ``_ERROR`` keeps waiting (re-query before concluding). Progress is
    read from ``tasks_reader(run_dir)``; when the running set is empty or has not
    changed for ``stale_after`` seconds (a frozen event log), the view degrades to
    a spinner + elapsed with a periodic Ctrl-C hint so stale, non-decrementing
    rows are never left on screen.

    ``grace_seconds`` defaults to 0, which preserves the original unbounded
    wait (a Ctrl-C is the only way to escalate). A caller with no interactive
    terminal - a script, or an agent driving ``bakar stop`` through a
    backgrounded shell - has no way to deliver that Ctrl-C, so a positive
    ``grace_seconds`` gives it a bounded alternative: once elapsed reaches the
    value, the wait escalates on its own exactly as a Ctrl-C would.

    ``clock``/``sleep``/``liveness``/``tasks_reader`` are injectable seams so the
    branching logic is unit-testable without real sleeps or signals; set
    ``install_signal=False`` on the context to skip the SIGINT handler in tests.
    """
    out = ctx.console_out if ctx.console_out is not None else console
    start = ctx.clock()
    error_streak = 0
    last_signature: frozenset[tuple[str, str]] | None = None
    last_change = start
    last_hint = start
    read_tasks = ctx.tasks_reader if ctx.tasks_reader is not None else _incremental_reader()

    prev_handler = None
    if ctx.install_signal:
        prev_handler = signal.signal(signal.SIGINT, _wait_sigint_handler)  # pragma: no cover
    try:
        while True:
            status = ctx.liveness()
            if status == _DEAD:
                return "drained"
            if status == _ERROR:
                error_streak += 1
                if error_streak >= ctx.error_cap:
                    return "lost_runtime"
            else:
                error_streak = 0

            now = ctx.clock()
            elapsed = now - start
            if ctx.grace_seconds > 0 and elapsed >= ctx.grace_seconds:
                ctx.escalate()
                return "escalated"
            tasks = read_tasks(ctx.run_dir) if ctx.run_dir is not None else []
            signature = frozenset((t.recipe, t.task) for t in tasks)
            if signature != last_signature:
                last_signature = signature
                last_change = now
            stale = (now - last_change) >= ctx.stale_after

            if tasks and not stale:
                _render_running(out, tasks, elapsed)
            else:
                show_hint = (now - last_hint) >= ctx.hint_interval
                if show_hint:
                    last_hint = now
                _render_spinner(out, elapsed, ctx.target_desc, show_hint=show_hint)

            ctx.sleep(ctx.poll_interval)
    except KeyboardInterrupt:
        ctx.escalate()
        return "escalated"
    finally:
        if prev_handler is not None:
            signal.signal(signal.SIGINT, prev_handler)  # pragma: no cover


@dataclass(frozen=True, kw_only=True, slots=True)
class _ContainerStopPlan:
    """How to stop one container: escalation policy plus where progress is shown.

    ``failures`` is an out-parameter: when the stop ends ``"incomplete"``,
    :func:`_stop_container` appends each failed runtime command (exit status
    and error text) so the caller can name them in its incomplete-stop report.
    """

    force: bool
    term_secs: int
    run_dir: Path | None = None
    console_out: Console | None = None
    grace_seconds: float = 0
    failures: list[str] = field(default_factory=list)


def _stop_container(runtime: str, cid: str, plan: _ContainerStopPlan) -> str:
    """Stop container ``cid`` via ``runtime`` with a graceful wait.

    When ``plan.force`` is False: send SIGINT to bitbake inside the container first
    (via :func:`_sigint_bitbake_in_container`, falling back to a container-PID-1
    SIGINT if the exec fails), then wait via :func:`_graceful_wait` until the
    container is no longer running, rendering live progress. A Ctrl-C, or
    ``grace_seconds`` elapsing, escalates through
    ``stop --timeout=<term_secs>`` -> ``kill --signal=SIGKILL`` -> ``rm -f``.
    When ``force`` is True: skip the SIGINT step and go straight to that
    escalation ladder.

    Returns the :func:`_graceful_wait` status (``"drained"``/``"escalated"``/
    ``"lost_runtime"``) for the graceful path, or ``"forced"`` for ``force=True``.
    ``"lost_runtime"`` tells the caller the runtime went unreachable (exit 1).

    After an escalation the container is re-checked with
    :func:`_container_gone`. Only a conclusive "gone" answer keeps the status;
    a container still listed, or one whose goneness cannot be determined
    (whether or not a ladder command failed), makes it ``"incomplete"``, and
    the failed runtime commands, each with its exit status and error text, or
    a line naming the unconfirmed container, are appended to ``plan.failures``
    so the caller can name them in its incomplete-stop report. A ladder
    command that failed while the container is gone anyway
    (``rm -f`` racing the ``--rm`` removal) is not reported. This function
    prints no "stopped" line: the caller prints it once, after its own
    verification, so it can never precede an incomplete report.

    Uses ``--timeout`` (docker >= 29 deprecates ``--time``). Every subprocess
    call captures output and never raises on a non-zero exit.
    """
    escalate_failures: list[str] = []

    def _escalate() -> None:
        escalate_failures.extend(_escalate_container(runtime, cid, plan.term_secs) or [])

    if not plan.force:
        _say(f"Sent SIGINT to bitbake in container {cid}...")
        if not _sigint_bitbake_in_container(runtime, cid):
            stop_container._run_runtime([runtime, "kill", "--signal=SIGINT", cid])
        status = _graceful_wait(
            ctx=_WaitCtx(
                liveness=lambda: _container_liveness(runtime, cid),
                escalate=_escalate,
                target_desc=f"container {cid}",
                run_dir=plan.run_dir,
                console_out=plan.console_out,
                grace_seconds=plan.grace_seconds,
            )
        )
        if status == "lost_runtime":
            _say("lost contact with the container runtime")
            return status
        if status == "drained":
            return status
    else:
        _say(f"Sending SIGTERM to container {cid}...")
        _escalate()
        status = "forced"

    # The ladder ran: confirm the container is actually gone. kas-container runs
    # with ``--rm``, so a stopped container is removed and ``inspect <cid>``
    # errors for it exactly as it does for a wedged runtime. Listing by id
    # separates the two: a successful empty listing is conclusive. Only that
    # conclusive answer is success: an unanswered query says nothing about the
    # container, which may still hold bitbake.lock, so it is incomplete even
    # when every ladder command reported success.
    try:
        gone = stop_container._container_gone(runtime, cid)
    except KeyboardInterrupt:
        # A second Ctrl-C during verification cannot confirm the outcome.
        gone = None
    if gone is True:
        return status
    if escalate_failures:
        plan.failures.extend(escalate_failures)
    elif gone is None:
        plan.failures.append(f"could not confirm container {cid} is gone after stop, kill and rm -f")
    else:
        plan.failures.append(f"container {cid} is still running after stop, kill and rm -f")
    return "incomplete"


def stop_running_proc(proc: subprocess.Popen, cfg: BuildConfig, log: RunLogger) -> None:
    """Stop the live build ``proc`` in-process, mode-aware, never raising.

    Shared by the in-process Ctrl-C handler and the stall watchdog. Host mode
    does the byte-for-byte existing ``os.killpg(proc.pid, signal.SIGINT)`` (the
    PGID path that is correct when bitbake is a real descendant). Container mode
    resolves the container by its ``bakar.run_id`` label and sends a graceful
    SIGINT to bitbake inside the container, falling back to the PGID signal when
    the container cannot be resolved or the exec fails.
    """
    if cfg.host_mode:
        os.killpg(proc.pid, signal.SIGINT)
        return

    try:
        runtime = detect_runtime()
        cid = stop_container._container_id(runtime, run_id_label(log.run_id))
        if cid is None:
            os.killpg(proc.pid, signal.SIGINT)
            return
        # Send a single graceful SIGINT to bitbake inside the container and let
        # the caller's proc.wait() reap the wrapper - mirroring the old
        # non-blocking semantics. Signalling the container PID 1 does not reach
        # bitbake (the entrypoint does not forward signals), so signal bitbake
        # directly; fall back to the PGID signal if the exec fails. The
        # grace-poll + SIGTERM/SIGKILL escalation ladder lives in stop_build
        # (the out-of-process `bakar stop`), which has no proc.wait() backstop;
        # running it here would block the Ctrl-C handler / stall watchdog for
        # the full grace period and hang the UI.
        if not _sigint_bitbake_in_container(runtime, cid):
            os.killpg(proc.pid, signal.SIGINT)
    except OSError:
        return


def is_build_running(run_dir: Path) -> tuple[bool, int | None, bool]:
    """Inspect ``run_dir/build.pid`` and report build-process liveness.

    Returns ``(live, pgid, cmdline_ok)``:

    - ``pgid`` is the recorded process-group id, or ``None`` when the pidfile
      is missing or unparseable.
    - ``live`` is True iff ``os.killpg(pgid, 0)`` confirms the group exists.
    - ``cmdline_ok`` is True iff ``/proc/<pgid>/cmdline`` is readable and any
      null-separated field contains ``kas-container`` or ``kas``. It is False
      when the process is dead or the procfs entry is unreadable.
    """
    pid_file = run_dir / _PID_FILENAME
    try:
        raw = pid_file.read_text()
    except OSError:
        return (False, None, False)
    try:
        pgid = int(raw.strip())
    except ValueError:
        return (False, None, False)
    if pgid <= 0:
        # A non-positive pgid would make os.killpg signal our own group (0)
        # or be invalid; treat a corrupted pidfile as not-running.
        return (False, pgid, False)

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return (False, pgid, False)
    except OSError:
        # EPERM: the process exists but is owned by someone else. Treat it as
        # alive and let the cmdline check decide identity.
        pass

    cmdline_path = Path(f"/proc/{pgid}/cmdline")
    try:
        cmdline_bytes = cmdline_path.read_bytes()
    except OSError:
        return (True, pgid, False)

    fields = cmdline_bytes.split(b"\x00")
    cmdline_ok = any(token.encode() in field for field in fields for token in _VALID_CMDLINE_TOKENS)
    return (True, pgid, cmdline_ok)


def read_lock_pid(lock_path: Path) -> int | None:
    """Parse the PID bitbake wrote as the first token of ``bitbake.lock``.

    bitbake creates the lock and writes its PID as a later, separate step, so
    an empty or unparseable lock is a server mid-startup, not a stale
    leftover. Returns ``None`` for a missing or unreadable file, empty
    content, a non-integer first token, or a non-positive pid; callers must
    never treat ``None`` as license to remove the lock.
    """
    try:
        raw = lock_path.read_text()
    except OSError:
        return None
    tokens = raw.split()
    if not tokens:
        return None
    try:
        pid = int(tokens[0])
    except ValueError:
        return None
    # -1 and 0 parse as ints but name no process: kill(-1, sig) signals every
    # process the user owns and kill(0, sig) this process's own group.
    return pid if pid > 0 else None


def _read_bitbake_server_pid(run_dir: Path) -> int | None:
    """Read bitbake-server's own PID from ``bitbake.lock``'s first line.

    bb.server.process writes ``os.getpid()`` as the lock file's first
    line/token (see bitbake/lib/bb/server/process.py), and
    bb.daemonize.createDaemon double-forks + calls os.setsid() to start it -
    the server leads a brand-new session, so it is NEVER a member of the
    kas-container/kas process group ``build.pid`` records. killpg(pgid, ...)
    structurally cannot reach it: the server can (and by design does) outlive
    the client that launched it, e.g. to serve a warm cooker to a later
    bitbake invocation, or here, because something killed the client (a
    SIGQUIT from a job-control keypress) without the server ever seeing an
    interrupt. This is the only liveness signal that actually reaches it.

    Returns None when the lock is missing, empty, or unparseable - already
    exited, a mid-write race, or a container build with no host-side lock.
    """
    return read_lock_pid(run_dir.parent.parent / "bitbake.lock")


def _pid_alive(pid: int) -> bool:
    """Return True while process ``pid`` still exists (single-PID, not group)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM: still alive, just owned by someone else.
        return True
    return True


def _bitbake_server_alive(run_dir: Path) -> bool:
    """True while bitbake-server's own detached PID (from bitbake.lock) is alive
    AND still identifiably this build's, not a PID the kernel has since
    recycled onto an unrelated process.

    Every caller of this function needs to agree with :func:`_escalate_host`'s
    kill decision, or the two diverge on a recycled PID: escalation correctly
    declines to signal it, while this function - unverified - would keep
    reporting the (unrelated) process as this build still running, forever.
    That reads to an operator as ``bakar stop`` never completing on a
    workspace whose real cooker has, in fact, already exited.
    """
    pid = _read_bitbake_server_pid(run_dir)
    return pid is not None and _pid_alive(pid) and _bitbake_server_pid_verified(pid, run_dir.parent.parent)


def _pgid_alive(pgid: int) -> bool:
    """Return True while any member of process group ``pgid`` still exists.

    Uses ``os.killpg(pgid, 0)`` (group semantics) so a build whose leader has
    exited but whose children are still shutting down counts as alive.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM: still alive, just owned by someone else.
        return True
    return True


@dataclass(frozen=True)
class _KilledProc:
    """One process the scoped reaper signalled, recorded for the audit log."""

    pid: int
    signal: str  # "SIGTERM" or "SIGKILL"
    cmdline: str


# bitbake-server's argv carries this build's absolute logfile, lock and socket
# paths (bitbake/lib/bb/server/process.py execServer execl). Matching the FULL
# build-specific path - never a bare basename - is what scopes discovery to THIS
# build: a second build on the same host has a different TOPDIR, so its cooker's
# argv references a different bitbake.lock path and can never match here.
_COOKER_ARGV_FILES = ("bitbake.lock", "bitbake.sock", "bitbake-cookerdaemon.log")
_PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class _ScopedProcs:
    """The process set attributable to one build dir.

    ``cooker`` is the argv-matched set - the wedged cooker and anything else
    spawned with this build's lock/sock/cookerdaemon.log paths (the lock
    holders). ``all_pids`` is ``cooker`` plus the recorded wrapper-PGID members
    plus the transitive /proc-ppid descendants of both, minus this ``bakar
    stop`` process and its own group. Signalling ``all_pids`` reaches the
    bitbake-worker and reparented-orphan processes that the PGID and lock-PID
    paths miss.
    """

    cooker: frozenset[int]
    all_pids: frozenset[int]


def _all_pids(proc_root: Path = _PROC_ROOT) -> list[int]:
    """Return every numeric PID under ``proc_root`` (/proc); [] on error."""
    try:
        return sorted(int(entry.name) for entry in proc_root.iterdir() if entry.name.isdigit())
    except OSError:
        return []


def _proc_cmdline(pid: int, proc_root: Path = _PROC_ROOT) -> str:
    """Return ``pid``'s space-joined argv, or '' when unreadable (gone/EPERM)."""
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _proc_ppid(pid: int, proc_root: Path = _PROC_ROOT) -> int | None:
    """Return ``pid``'s parent PID from /proc/<pid>/status, or None on error."""
    try:
        raw = (proc_root / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        if line.startswith("PPid:"):
            fields = line.split()
            if len(fields) < 2:
                return None
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


def _short_cmd(cmdline: str, limit: int = 60) -> str:
    """Trim ``cmdline`` to ``limit`` chars for a one-line audit note."""
    trimmed = cmdline.strip()
    return trimmed if len(trimmed) <= limit else trimmed[: limit - 3] + "..."


@dataclass(frozen=True, kw_only=True, slots=True)
class _ProcView:
    """How the reaper observes /proc, and which process is the observer itself."""

    self_pid: int | None = None
    self_pgid: int | None = None
    pids_reader: Callable[[], list[int]] = _all_pids
    cmdline_reader: Callable[[int], str] = _proc_cmdline
    ppid_reader: Callable[[int], int | None] = _proc_ppid
    pgid_reader: Callable[[int], int] = os.getpgid


_LIVE_PROC_VIEW = _ProcView()


def _collect_build_pids(
    topdir: Path,
    pgid: int | None,
    view: _ProcView = _LIVE_PROC_VIEW,
) -> _ScopedProcs:
    """Collect the process set attributable to the build rooted at ``topdir``.

    Seeds are the argv-matched cooker (any process whose cmdline references this
    build's lock/sock/cookerdaemon.log path) plus the recorded wrapper-PGID
    members. The result adds their transitive /proc-ppid descendants (workers,
    task subprocesses, orphans reparented to init that are still descendants),
    then drops this ``bakar stop`` process and its own group so the reaper can
    never signal itself. The readers are injectable so the /proc walk is
    unit-testable against a fake procfs without real processes.
    """
    resolved_self_pid = os.getpid() if view.self_pid is None else view.self_pid
    resolved_self_pgid = os.getpgrp() if view.self_pgid is None else view.self_pgid
    markers = [str(topdir / name) for name in _COOKER_ARGV_FILES]

    pids = view.pids_reader()
    cmdlines = {pid: view.cmdline_reader(pid) for pid in pids}
    cooker = {pid for pid in pids if any(marker in cmdlines[pid] for marker in markers)}

    seeds = set(cooker)
    if pgid is not None and pgid > 0:
        for pid in pids:
            try:
                if view.pgid_reader(pid) == pgid:
                    seeds.add(pid)
            except OSError:
                continue

    if not seeds:
        # cooker is a subset of seeds, so an empty seed set means no cooker and
        # no group member - nothing attributable to this build. Skip the ppid
        # walk entirely (the common clean-tree and hermetic-test case).
        return _ScopedProcs(cooker=frozenset(), all_pids=frozenset())

    children: dict[int, list[int]] = {}
    for pid in pids:
        parent = view.ppid_reader(pid)
        if parent is not None:
            children.setdefault(parent, []).append(pid)

    reached = set(seeds)
    stack = list(seeds)
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child not in reached:
                reached.add(child)
                stack.append(child)

    def _is_self(pid: int) -> bool:
        if pid == resolved_self_pid:
            return True
        try:
            return view.pgid_reader(pid) == resolved_self_pgid
        except OSError:
            return False

    return _ScopedProcs(
        cooker=frozenset(pid for pid in cooker if not _is_self(pid)),
        all_pids=frozenset(pid for pid in reached if not _is_self(pid)),
    )


def _discover_host_cookers(
    *,
    pids_reader: Callable[[], list[int]] = _all_pids,
    cmdline_reader: Callable[[int], str] = _proc_cmdline,
) -> dict[Path, frozenset[int]]:
    """Scan every PID on the host for a bitbake cooker's argv markers.

    Unlike :func:`_collect_build_pids`, this takes no ``topdir`` up front - it
    walks every host PID looking for the three bare marker filenames
    (``bitbake.lock``, ``bitbake.sock``, ``bitbake-cookerdaemon.log``)
    anywhere in that process's cmdline, rather than a full path under one
    known build dir. For each match the topdir is recovered by stripping the
    trailing marker filename off the matched path (``.../nxp/build/
    bitbake.lock`` -> ``.../nxp/build``).

    A ``bitbake-worker`` subprocess is spawned with piped file descriptors
    and carries no lock/socket/log path in its own argv, so the same argv
    match that discovers a topdir also identifies its cooker - there is no
    separate worker-exclusion step. This is treated as a verified assumption
    here, checked by a dedicated multi-worker test rather than asserted only
    in this docstring.

    A PID whose cmdline cannot be read (permission denied on a multi-user
    host) resolves to ``""`` from ``cmdline_reader`` and is skipped, not
    raised on.

    Returns a dict mapping each discovered topdir to the frozenset of cooker
    PID(s) whose argv matched there. Deduplication across topdirs (e.g. the
    same build discovered via both its lock and its log path) and
    correlation with a recorded run_id are out of scope - later tasks own
    those.
    """
    discovered: dict[Path, set[int]] = {}
    for pid in pids_reader():
        # Guards the call site itself rather than trusting the documented
        # "never raises" contract alone: the default `_proc_cmdline` already
        # honors it, but `cmdline_reader` is an injectable parameter with no
        # type-level enforcement, and one PID's unreadable cmdline must not
        # abort the whole host-wide scan.
        try:
            cmdline = cmdline_reader(pid)
        except OSError:
            continue
        if not cmdline:
            continue
        for token in cmdline.split():
            for marker in _COOKER_ARGV_FILES:
                suffix = f"/{marker}"
                if token.endswith(suffix):
                    topdir = Path(token[: -len(suffix)])
                    discovered.setdefault(topdir, set()).add(pid)
                    break

    return {topdir: frozenset(pids) for topdir, pids in discovered.items()}


def _kill_pid(pid: int, sig: int) -> bool:
    """``os.kill`` ``pid`` with ``sig``, guarding the dangerous pids.

    A ``pid <= 0`` is refused outright, like :func:`_killpg` refuses a
    non-positive group id: ``kill(-1, ...)`` signals every process the user can
    reach and ``kill(0, ...)`` this process's own group. Returns True when the
    signal was delivered, False when the process is gone, denied, or refused.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


def _killpg(pgid: int, sig: int) -> bool:
    """``os.killpg`` ``pgid`` with ``sig``, guarding the dangerous group ids.

    A ``pgid <= 0`` is refused outright: ``killpg(0, ...)`` would signal this
    ``bakar stop`` process's own group and ``killpg(-1, ...)`` every process the
    user can reach. Returns True when the signal was delivered, False when the
    group is gone, denied, or refused by the guard.
    """
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, sig)
    except OSError:
        return False
    return True


def _host_build_alive(pgid: int | None, run_dir: Path) -> bool:
    """True while ANY part of the host build still runs.

    Checks three layers so a wedged cooker whose wrapper already died still
    reads as alive: the wrapper process group, bitbake-server's detached PID
    from ``bitbake.lock``, and the argv-scoped cooker set (which catches a
    cooker whose lock first line is unreadable).
    """
    if pgid is not None and pgid > 0 and _pgid_alive(pgid):
        return True
    if _bitbake_server_alive(run_dir):
        return True
    return bool(_collect_build_pids(run_dir.parent.parent, None).cooker)


def _bitbake_server_pid_verified(bb_pid: int, topdir: Path) -> bool:
    """True unless ``bb_pid`` is POSITIVELY known not to be this build's own
    bitbake-server.

    ``_read_bitbake_server_pid`` returns a raw PID parsed from ``bitbake.lock``,
    with no check tying that number back to a live bitbake-server process. The
    gap between reading the lock and signalling widens with every escalation
    ladder this ladder now feeds (a 900s capture timeout routes here too), and
    across a wide enough gap the kernel can recycle that PID onto an unrelated
    process - SIGKILL-ing it would kill something this build never launched.
    bitbake's own execServer (bb.server.process) execs with the lock file's and
    socket's absolute paths as literal argv tokens, so a genuine bitbake-server
    always carries one; anything else reading at that PID does not.

    Returns False only on positive evidence: the PID has already exited, or its
    live cmdline names a different lock/sock path. An UNREADABLE cmdline
    (``/proc`` mounted ``hidepid=1``/``2``, the cooker running as another uid) is
    not that evidence - conflating "could not verify" with "verified not ours"
    would silently disable this layer on any host with a restricted ``/proc``,
    and :func:`_bitbake_server_alive` would then report a live cooker as
    running forever with no path left to ever kill it. An unreadable cmdline
    therefore falls back to True: the escalation ladder accepts the same
    residual recycled-PID risk this function otherwise closes, rather than
    trade a rare bad kill for a permanently stuck lock.
    """
    if not _pid_alive(bb_pid):
        return False
    cmdline = _proc_cmdline(bb_pid)
    if not cmdline:
        return True
    markers = [str(topdir / name) for name in ("bitbake.lock", "bitbake.sock")]
    return any(marker in cmdline for marker in markers)


def _escalate_host(pgid: int | None, run_dir: Path | None = None) -> list[_KilledProc]:
    """SIGTERM->SIGKILL the whole scoped process set for this build.

    Three targeting layers, unioned so a wedged, detached cooker cannot survive:

    1. the wrapper's process group ``pgid`` (``killpg``);
    2. bitbake-server's own PID from ``bitbake.lock`` - a different session, so
       ``killpg`` structurally cannot reach it (see ``_read_bitbake_server_pid``).
       Verified against its live cmdline first (see
       ``_bitbake_server_pid_verified``) so a stale, recycled PID is never
       signalled;
    3. the argv-scoped set - the cooker matched by this build's
       lock/sock/cookerdaemon.log paths in ``/proc`` plus its descendants
       (bitbake-worker, task subprocesses, orphans reparented to init). Layer 3
       reaches a cooker whose ``bitbake.lock`` first line is unreadable and its
       reparented workers, which neither the PGID nor the lock-PID path finds.

    Each layer gets SIGTERM, one ``_STOP_TERM_SECONDS`` grace window, then
    SIGKILL only for whatever is still alive. Every signalled PID is printed
    (SIGTERM/SIGKILL + cmdline) and returned as the layer-3 audit list. The
    grace window itself is skipped when the SIGTERM rung delivered nothing -
    there is nothing to wait out, and the caller (a build already reporting
    its own completion, or an interrupt handler) should not pay a real
    ``_STOP_TERM_SECONDS`` for a no-op. Layer 2 is re-verified against
    :func:`_bitbake_server_pid_verified` again before the SIGKILL, not only
    before the SIGTERM: the two rungs are ``_STOP_TERM_SECONDS`` apart, which
    is the same recycled-PID window this function exists to close, only wider
    - the PID it SIGTERM'd can exit and be reused by the kernel during the
    wait, and the SIGKILL must not trust a verification that old.
    """
    topdir = run_dir.parent.parent if run_dir is not None else None
    bb_pid = _read_bitbake_server_pid(run_dir) if run_dir is not None else None
    if bb_pid is not None and topdir is not None and not _bitbake_server_pid_verified(bb_pid, topdir):
        _say(f"  bitbake.lock pid {bb_pid} no longer matches this build (stale/recycled) - not signalling")
        bb_pid = None
    scoped = _collect_build_pids(run_dir.parent.parent, pgid) if run_dir is not None else None
    scoped_pids = sorted(scoped.all_pids) if scoped is not None else []
    killed: list[_KilledProc] = []

    # --- SIGTERM rung ---
    signalled_anything = False
    if pgid is not None and pgid > 0 and _killpg(pgid, signal.SIGTERM):
        _say(f"  SIGTERM process group {pgid}")
        signalled_anything = True
    if bb_pid is not None and _kill_pid(bb_pid, signal.SIGTERM):
        _say(f"  SIGTERM bitbake-server pid {bb_pid}")
        signalled_anything = True
    for pid in scoped_pids:
        cmdline = _proc_cmdline(pid)
        if _kill_pid(pid, signal.SIGTERM):
            killed.append(_KilledProc(pid, "SIGTERM", cmdline))
            _say(f"  SIGTERM pid {pid} ({_short_cmd(cmdline)})")
            signalled_anything = True

    if not signalled_anything:
        return killed

    time.sleep(_STOP_TERM_SECONDS)

    # --- SIGKILL rung (survivors only) ---
    if pgid is not None and pgid > 0 and _pgid_alive(pgid) and _killpg(pgid, signal.SIGKILL):
        _say(f"  SIGKILL process group {pgid}")
    if (
        bb_pid is not None
        and _pid_alive(bb_pid)
        and topdir is not None
        and _bitbake_server_pid_verified(bb_pid, topdir)
        and _kill_pid(bb_pid, signal.SIGKILL)
    ):
        _say(f"  SIGKILL bitbake-server pid {bb_pid}")
    for pid in scoped_pids:
        if _pid_alive(pid):
            cmdline = _proc_cmdline(pid)
            if _kill_pid(pid, signal.SIGKILL):
                killed.append(_KilledProc(pid, "SIGKILL", cmdline))
                _say(f"  SIGKILL pid {pid} ({_short_cmd(cmdline)})")
    return killed


def escalate_process_tree(leader_pid: int, run_dir: Path | None = None) -> list[_KilledProc]:
    """Public entry onto the SIGTERM->SIGKILL ladder for a self-led subprocess.

    ``leader_pid`` is a child's PID, not a process-group id, and the group is
    derived here rather than trusted from the caller. A child spawned WITHOUT
    ``start_new_session=True`` sits in bakar's own process group, so handing its
    pid to :func:`_escalate_host` as a pgid would signal bakar itself along with
    everything else sharing that group. The equality check below is what makes
    that unrepresentable: it refuses unless the child leads a group containing
    only itself and its descendants.

    Exists so callers outside this module (the post-build graph capture in
    :mod:`bakar.steps.kas_build`) can reach the ladder without reaching for a
    private name. Escalating rather than merely abandoning the child matters
    because the timed-out process is a ``bitbake -g`` whose cooker holds
    ``bitbake.lock``: killing the parent alone would strand that cooker and
    refuse the next build on this directory.

    Falls back to ``pgid=None`` - never an early ``return []`` - when the
    leader is already gone or fails the self-led check: ``_escalate_host``'s
    ``bitbake.lock``-PID and argv-scoped cooker layers key off ``run_dir``,
    not off this leader's pgid, so a leader that already exited (or that this
    function correctly refuses to signal) says nothing about whether the
    cooker it spawned is still alive and still holding the lock. Returning
    early here would skip those two run_dir-scoped layers entirely on exactly
    the timing where the wrapper died first and the detached cooker outlived
    it - the case this function exists to catch.

    Returns the layer-3 audit list from :func:`_escalate_host`.
    """
    try:
        pgid: int | None = os.getpgid(leader_pid)
    except OSError:
        pgid = None
    else:
        if pgid != leader_pid:
            _say(
                f"  refusing to signal pid {leader_pid}: its process group is {pgid}, "
                "which is not its own - signalling it would reach bakar too"
            )
            pgid = None
    return _escalate_host(pgid, run_dir)


def escalate_container_tree(run_id: str) -> bool:
    """Public entry onto the container stop ladder for one run's own container.

    Sibling to :func:`escalate_process_tree`, for container-mode builds.
    Escalating the host-side kas-container client process (what
    :func:`escalate_process_tree` targets) stops that client but not the
    container it launched - the runtime does not stop a container merely
    because the client that started it exits. The bitbake cooker inside
    keeps running and keeps holding ``bitbake.lock``, which is exactly the
    strand this module exists to prevent.

    Resolves the container by its ``bakar.run_id`` label
    (:func:`run_id_label`) and force-stops it through the same
    stop -> kill -> rm -f ladder :func:`_stop_container` uses.

    Returns True when a container was found AND verified gone afterward,
    False when none resolved (already gone, or the runtime is unreachable)
    OR the ladder ran without the container actually disappearing - the
    caller should fall back to :func:`escalate_process_tree` in either case.
    ``_escalate_container`` returns the failed commands' descriptions, but
    this caller drops them: a command's failure text cannot say whether the
    container is gone (``rm -f`` can fail because the container is already
    being removed), so re-querying by the same label is the only way to tell
    "escalated" from "issued the commands and hoped".

    The post-escalation check uses :func:`_container_id_status`, not
    :func:`_container_id`, on purpose: ``_container_id`` collapses "no
    container matches" and "the query itself failed" into the same ``None``,
    and the two must not collapse into one "escalated" verdict here - a
    wedged runtime after the ladder ran must read as unverified, not as
    success. (``_container_liveness`` on the specific ``cid`` would not work
    either: after a successful ``rm -f`` that id no longer exists at all, so
    ``inspect`` on it returns non-zero - indistinguishable from a query
    failure - rather than the clean "not running" `_DEAD` this needs.)
    """
    runtime = detect_runtime()
    cid = stop_container._container_id(runtime, run_id_label(run_id))
    if cid is None:
        return False
    _escalate_container(runtime, cid, _STOP_TERM_SECONDS)
    status, _cid = _container_id_status(runtime, run_id_label(run_id))
    return status == _DEAD


# ---------------------------------------------------------------------------
# Lock-ownership gate.
#
# On a shared NFS TOPDIR (see ``tmpdir-local-override``), every node-local
# liveness probe below (os.kill/os.killpg/argv scan) is meaningless against a
# PID owned by a different fleet node: two nodes can each build a distinct
# TOPDIR from one shared checkout, so a peer's live ``bitbake.lock`` must
# never be read, deleted, or have its PID signalled by another node. The
# primitives here are the single shared gate every lock mutator (this
# module's stale-file cleanup, the doctor preflight, the stress-parse wipe)
# and every lock acquirer (``run_build``, ``run_shell_live``,
# ``run_shell_capture``) consults before touching the lock/socket files or a
# PID read from them.
# ---------------------------------------------------------------------------

_LOCK_MARKER_FILENAME = ".bakar-lock-host"


@dataclass(frozen=True)
class LockRefusal:
    """Why a lock mutator/acquirer refused to touch the build's lock state.

    ``reason`` is one of four verdicts:

    - ``"peer-held"`` - the ownership marker names another fleet node.
    - ``"held-locally"`` - this node owns the marker (or is the sole
      candidate on a confirmed-local filesystem) but a live cooker is using
      the lock. Not produced by :func:`lock_mutation_guard` itself (which has
      no activity probe); reserved for callers that layer an activity check
      on top, e.g. ``clear_stale_bitbake_locks``.
    - ``"unattributable"`` - no reliable owner (marker absent or garbled) and
      ``bitbake.lock`` is present on a shared or unverifiable filesystem.
    - ``"shared-inaction"`` - no reliable owner and the lock reads absent on
      a shared or unverifiable filesystem. NFS negative-lookup caching can
      report a peer's freshly-created lock as absent for up to ~60s, so a
      bare "safe" verdict here would let a caller run a blind unconditional
      unlink on the strength of that stale absence view.
    """

    reason: Literal["peer-held", "held-locally", "unattributable", "shared-inaction"]
    host: str | None = None
    pid: int | None = None
    detail: str = ""
    marker: Path | None = None


def manual_clear_hint(marker: Path) -> str:
    """One sentence telling the user how to clear a marker by hand.

    NFS attribute caching can leave a marker (and its ``bitbake.lock``) that
    no node will clear on its own; the user is the only party who knows
    whether a build still runs anywhere.
    """
    return f"If no build runs on any node, delete the marker {marker} and the bitbake.lock beside it by hand."


def _say_stop_refusal(refusal: LockRefusal) -> None:
    """Print why a stop was refused, plus the manual-clear hint when a marker is known."""
    if refusal.reason == "peer-held":
        # _say prints plain text to stderr, never through Rich markup
        # rendering, so the host name needs no escaping here.
        host = refusal.host if refusal.host else "another host"
        message = f"build owned by {host}; run `bakar stop` there"
    else:
        message = (
            f"cannot confirm this node owns the build lock ({refusal.reason}); "
            "refusing to send any signal - resolve ownership manually"
        )
    if refusal.marker is not None:
        message = f"{message}. {manual_clear_hint(refusal.marker)}"
    _say(message)


@dataclass
class LockClearOutcome:
    """Result of a stale-lock clearing attempt: what was removed, if anything.

    ``refusal`` is ``None`` when the clear proceeded (``removed`` may still be
    empty - nothing was stale). A non-``None`` ``refusal`` means nothing was
    touched: ``removed`` is always ``[]`` in that case.
    """

    removed: list[Path]
    refusal: LockRefusal | None = None
    note: str = ""


def lock_marker_path(cfg: BuildConfig) -> Path:
    """Path to the ownership marker for ``cfg``'s TOPDIR.

    Lives at ``bsp_root/build_dir_name/.bakar-lock-host`` - the resolved
    TOPDIR, not a hardcoded ``"build"`` (qcom's BUILDDIR is
    ``build-<distro>``; see :attr:`BuildConfig.build_dir_name`).
    """
    return cfg.bsp_root / cfg.build_dir_name / _LOCK_MARKER_FILENAME


def read_marker(cfg: BuildConfig) -> tuple[Literal["absent", "unreadable", "empty", "garbled", "owner"], str | None]:
    """Classify the ownership marker once: ``(state, owner)``.

    The torn/garbled rules live here so every reader decides from the same
    classification. ``owner`` is the hostname only when ``state`` is
    ``"owner"``; every other state carries ``None``, and none of them may ever
    be treated as a peer:

    - ``absent`` - no marker file.
    - ``unreadable`` - ``OSError`` reading it.
    - ``empty`` - blank content, possibly a peer's mid-write on a filesystem
      with weak visibility.
    - ``garbled`` - an interrupted write left embedded control bytes or more
      than one line of content; a partial write from this node's own crash
      must not read as a peer lock.
    """
    try:
        raw = lock_marker_path(cfg).read_text()
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    owner = raw.strip()
    if not owner:
        return "empty", None
    if "\n" in owner or "\x00" in owner or not owner.isprintable():
        return "garbled", None
    return "owner", owner


def read_marker_owner(cfg: BuildConfig) -> str | None:
    """Read the hostname recorded in the ownership marker, or ``None``.

    ``None`` covers every state :func:`read_marker` does not classify as
    ``"owner"``: absent, empty, unreadable or garbled.
    """
    return read_marker(cfg)[1]


def read_marker_state(cfg: BuildConfig) -> Literal["absent", "unreadable", "empty", "garbled", "owner"]:
    """The state half of :func:`read_marker`.

    The lock-acquire path needs to tell "could not read it" (``unreadable``, or
    ``empty``) apart from "read it and it is nonsense" (``garbled``), because
    only the former may belong to a live peer.
    """
    return read_marker(cfg)[0]


def lock_mutation_guard(cfg: BuildConfig) -> LockRefusal | None:
    """Return why ``cfg``'s lock/socket state must not be touched, or ``None``.

    ``None`` means the mutation (or the signal, for ``bakar stop``) is safe.
    Four verdicts, evaluated in order:

    1. The marker names a host other than this one -> ``"peer-held"``.
    2. The marker names this host -> ``None`` (this node owns the lock
       unconditionally, regardless of filesystem or lock-file state).
    3. The marker is absent/garbled, ``bitbake.lock`` is PRESENT, and the
       TOPDIR is on a shared or unverifiable filesystem -> ``"unattributable"``.
    4. The marker is absent/garbled, ``bitbake.lock`` is ABSENT, and the
       TOPDIR is on a shared or unverifiable filesystem -> ``"shared-inaction"``.
    5. Otherwise (marker absent/garbled but the filesystem is CONFIRMED
       local) -> ``None``.

    Calls ``mounts.is_path_on_nfs`` through the module, never a from-import, so a
    test patching ``bakar.mounts.is_path_on_nfs`` is seen at call time instead of
    leaving this guard reading the real ``/proc/mounts``.
    """
    marker = lock_marker_path(cfg)
    owner = read_marker_owner(cfg)
    if owner is not None:
        if owner != socket.gethostname():
            return LockRefusal(
                reason="peer-held", host=owner, detail=f"lock marker names {escape(owner)}", marker=marker
            )
        return None

    build_dir = cfg.bsp_root / cfg.build_dir_name
    nfs = mounts.is_path_on_nfs(build_dir)
    shared_or_unknown = nfs is not False  # True (nfs) or None (unverifiable) both fail closed
    if not shared_or_unknown:
        return None
    if (build_dir / "bitbake.lock").exists():
        return LockRefusal(
            reason="unattributable",
            detail="lock present, no reliable owner, shared/unverifiable filesystem",
            marker=marker,
        )
    return LockRefusal(
        reason="shared-inaction",
        detail="lock absent, no reliable owner, shared/unverifiable filesystem",
        marker=marker,
    )


def _clean_stale_bitbake_files(run_dir: Path) -> list[Path]:
    """Remove stale bitbake lock/socket files from the build TOPDIR.

    The TOPDIR is ``run_dir.parent.parent`` - the directory that CONTAINS the
    ``runs/`` dir (a resolved run dir is ``<TOPDIR>/runs/<timestamp>/``). A
    forced or killed bitbake leaves ``bitbake.lock`` and ``bitbake.sock``
    behind; a stale ``bitbake.lock`` makes the next build fail with
    "Cannot lock ... bitbake.lock". Call ONLY after the build is confirmed no
    longer running, so these files are guaranteed stale (never a live lock).
    ``bitbake-cookerdaemon.log`` is a diagnostic log and is left in place.

    Never raises: a missing or unremovable file is skipped (OSError). Returns
    the paths actually removed.
    """
    topdir = run_dir.parent.parent
    removed: list[Path] = []
    for name in _STALE_BITBAKE_FILES:
        path = topdir / name
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def _report_stale_cleanup(run_dir: Path, cfg: BuildConfig | None = None) -> list[Path]:
    """Remove stale bitbake lock/socket - only when nothing holds them - and log.

    ``cfg`` gates the removal on the shared ownership marker BEFORE the
    node-local argv holder-check: the argv scan alone cannot see a peer
    node's processes on a shared TOPDIR, so a refusal here must win over an
    empty (falsely-clear) local scan. ``cfg=None`` (no ``BuildConfig``
    available) skips the gate and preserves prior node-local-only behavior.

    A live process whose argv references this build's ``bitbake.lock`` /
    ``bitbake.sock`` is a holder; removing the lock out from under a running
    cooker would corrupt an in-flight build, so removal is gated on the
    argv-scan being empty. Returns the paths actually removed ([] when a holder
    is present, ownership is refused, or nothing was stale)."""
    if cfg is not None:
        refusal = lock_mutation_guard(cfg)
        if refusal is not None:
            # _say prints plain text to stderr, never through Rich markup
            # rendering, so the host name needs no escaping here.
            detail = f" ({refusal.host})" if refusal.host else ""
            _say(f"leaving bitbake.lock/bitbake.sock in place: ownership refused - {refusal.reason}{detail}")
            return []
    topdir = run_dir.parent.parent
    holders = _collect_build_pids(topdir, None).cooker
    if holders:
        _say(f"leaving bitbake.lock/bitbake.sock in place: still held by pid(s) {sorted(holders)}")
        return []
    removed = _clean_stale_bitbake_files(run_dir)
    if removed:
        _say(f"removed stale bitbake files: {', '.join(p.name for p in removed)}")
    return removed


def _verify_clean(
    run_dir: Path,
    pgid: int | None,
    *,
    runtime: str | None = None,
    container_label: str | None = None,
    cfg: BuildConfig | None = None,
) -> list[str]:
    """Return reasons the build is NOT fully stopped ([] means verified clean).

    Confirms every target class named in the escalation is gone: the argv-scoped
    cooker/workers, the wrapper process group, bitbake-server's detached PID,
    the build container (when a label is known), and the stale ``bitbake.lock``
    / ``bitbake.sock``. ``bakar stop`` reports success only when this is empty.

    ``cfg`` makes this refusal-aware: when the shared ownership gate refuses
    (peer-held/unattributable/shared-inaction), the stale lock/socket files
    that ``_report_stale_cleanup`` correctly left untouched are NOT reported
    as "still present" (that refusal is itself a successful stop, not a
    failure), and the ``bitbake-server`` liveness probe - a node-local PID
    read from a lock this node does not own - is skipped entirely, since this
    node cannot know whether that PID even belongs to bitbake."""
    topdir = run_dir.parent.parent
    reasons: list[str] = []
    # stop_build already refuses before the run-dir scan on any guard verdict,
    # so this re-check only ever sees a refusal in the narrow TOCTOU window
    # where ownership changes between stop_build's initial check and this
    # later call (see design.md's Non-Goals). Not dead code.
    refusal = lock_mutation_guard(cfg) if cfg is not None else None
    cooker = _collect_build_pids(topdir, pgid).cooker
    if cooker:
        reasons.append(f"bitbake cooker/worker still running (pids {sorted(cooker)})")
    if pgid is not None and pgid > 0 and _pgid_alive(pgid):
        reasons.append(f"build process group {pgid} still alive")
    if refusal is None and _bitbake_server_alive(run_dir):
        reasons.append("bitbake-server (from bitbake.lock) still alive")
    if runtime is not None and container_label is not None:
        # _container_id_status, not _container_id: a runtime query error here
        # must not read the same as "confirmed gone" - this check runs right
        # after the stop ladder signals the container, so a transient runtime
        # hiccup at exactly this moment would otherwise clear the one reason
        # that keeps the caller from declaring "stopped" it cannot back up.
        container_status, _cid = _container_id_status(runtime, container_label)
        if container_status != _DEAD:
            reasons.append("build container still running")
    if refusal is None:
        reasons.extend(f"{name} still present in {topdir}" for name in _STALE_BITBAKE_FILES if (topdir / name).exists())
    return reasons


def stop_build(
    bsp_root: Path, cfg: BuildConfig | None = None, *, force: bool = False, grace_seconds: float = 0
) -> bool:
    """Stop the most recent build, targeting it by execution mode.

    ``cfg`` gates PID trust, not just file mutation: on a shared NFS TOPDIR,
    ``bitbake.lock`` is also a kill-target selector with no identity check, so
    a peer-owned marker must refuse the ENTIRE stop before any signal is sent
    - reading a peer's PID number and signalling whatever local process holds
    it would be strictly worse than the file-deletion race this gate exists to
    prevent elsewhere. The gate is consulted FIRST, before the run-dir scan
    even starts: on ``"peer-held"``, ``"unattributable"``, or
    ``"shared-inaction"`` this refuses immediately and returns ``False``
    without touching anything (no run dir is scanned, no PID is read, no
    signal is sent, and the launch record is left in place). ``"peer-held"``
    and ``"unattributable"`` mean a live-looking lock exists and ownership
    cannot be confirmed; ``"shared-inaction"`` means the lock reads absent but
    the same node-local liveness probes used by the scan below cannot
    distinguish a genuinely idle workspace from a peer's live build hidden by
    NFS negative-lookup caching, so it must refuse just as hard rather than
    falling through to a scan that has no way to tell the two cases apart.
    Only a ``None`` verdict (this node owns the marker, or the filesystem is
    confirmed local) skips the gate entirely.
    ``cfg=None`` (no ``BuildConfig`` available) skips the gate entirely and
    preserves prior node-local-only behavior.

    Scans run dirs under ``bsp_root/<build_dir_name>/runs`` newest-first and
    targets the first whose build is still live (host: a verified live PGID;
    container: a recorded container label). Taking only the lexically-latest
    run missed a live build whenever a later clean-recipe or second build left
    a newer but finished run dir. Returns ``True`` when a build was targeted
    (host PGID signalled or container stopped), ``False`` when none is
    targetable (no run dir, every run is finished/unresolvable, or the
    ownership gate refused).

    Host mode sends SIGINT then waits (via ``_graceful_wait``) until the PGID
    is gone, escalating through SIGTERM -> SIGKILL on Ctrl-C, ``force``, or
    ``grace_seconds`` elapsing (0, the default, waits unbounded - only a
    Ctrl-C or ``force`` escalates). Container mode resolves the container via
    its recorded label and stops it through the runtime daemon, without
    touching the wrapper PGID; ``grace_seconds`` applies there too. The launch
    record (``build.pid`` + ``build.meta.json``) is removed before returning
    on every path that reaches the run-dir scan; a refused stop (see above)
    returns before that point and leaves the launch record untouched.
    """
    if cfg is not None:
        refusal = lock_mutation_guard(cfg)
        if refusal is not None:
            _say_stop_refusal(refusal)
            return False

    build_dir_name = cfg.build_dir_name if cfg is not None else "build"
    runs_dir = bsp_root / build_dir_name / "runs"
    try:
        run_dirs = sorted(runs_dir.iterdir())
    except OSError:
        _say("no running build found")
        return False
    if not run_dirs:
        _say("no running build found")
        return False
    # Target the newest run whose build is actually live/targetable, not just
    # the lexically-latest: a clean-recipe or a second build creates a newer run
    # dir, so the running build is often not runs[-1]. A host run is targetable
    # while its PGID is live+verified; a container run whenever a container label
    # was recorded (the wrapper may be dead while the container lives, so PGID
    # liveness must not gate it). Fall back to the latest run when none is
    # targetable, so the not-found messaging and stale-record cleanup below still
    # apply to it exactly as before.
    target = None
    for candidate in reversed(run_dirs):
        candidate_record = read_launch_record(candidate)
        if candidate_record.mode == "host":
            live, _pgid, cmdline_ok = is_build_running(candidate)
            if live and cmdline_ok:
                target = candidate
                break
            # A wedged detached cooker (wrapper dead, but bitbake-server or an
            # argv-scoped cooker still alive) is targetable too, so a newer
            # finished run does not hide the build that actually needs stopping.
            if _bitbake_server_alive(candidate) or _collect_build_pids(candidate.parent.parent, None).cooker:
                target = candidate
                break
        elif candidate_record.container_label is not None:
            target = candidate
            break
    run_dir = target if target is not None else run_dirs[-1]
    return stop_run(run_dir, cfg, force=force, grace_seconds=grace_seconds)


def stop_run(run_dir: Path, cfg: BuildConfig | None = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
    """Stop one already-selected run directory's build.

    Holds the SIGINT/escalate/verify sequence ``stop_build`` used to run
    inline once it had picked a run dir - extracted so a caller with its own
    run-dir selection (workspace-wide discovery, ``--run <id>``) can target a
    specific run without duplicating this ladder per candidate.

    ``cfg`` gates PID trust exactly as it does in ``stop_build``, and the
    NFS lock-ownership gate is checked here independently: ``stop_build``
    already checks it before it scans for a run to select, and that check
    stays in place unchanged. This is a second, independent layer for any
    caller that reaches ``stop_run`` directly with a pre-selected
    ``run_dir`` - skipping it would let a caller that bypasses the
    root-level scan (workspace-wide discovery targeting a run under a
    peer-held root) signal a build it does not own. Both copies are
    deliberate; this one is not dead code even when every existing caller
    still goes through ``stop_build`` first. ``cfg=None`` skips the gate
    entirely, matching ``stop_build``'s own no-``BuildConfig``-available
    degraded mode - ``stop_build`` passes its own possibly-``None`` ``cfg``
    straight through once it has picked a run dir, so this stays optional
    rather than mandatory.
    """
    if cfg is not None:
        refusal = lock_mutation_guard(cfg)
        if refusal is not None:
            _say_stop_refusal(refusal)
            return False

    record = read_launch_record(run_dir)
    if record.mode == "host":
        try:
            live, pgid, cmdline_ok = is_build_running(run_dir)
            wrapper_live = bool(live and cmdline_ok and pgid is not None)
            if not wrapper_live:
                # The wrapper is gone, but a wedged detached cooker (killpg can
                # not see it) or bitbake-server may still hold the build. Scan
                # by argv; if nothing runs, this is an idempotent clean-tree
                # stop: clear any stale lock/sock and succeed (requirement 5).
                topdir = run_dir.parent.parent
                if not _collect_build_pids(topdir, None).cooker and not _bitbake_server_alive(run_dir):
                    removed = _report_stale_cleanup(run_dir, cfg)
                    _say("no running build" + ("; cleaned stale lock/socket" if removed else ""))
                    return True
                _say("wrapper process gone; a detached cooker is still running - escalating")
            if not force:
                if pgid is not None and pgid > 0:
                    _say(f"Sent SIGINT to build PGID {pgid}...")
                    os.killpg(pgid, signal.SIGINT)
                bb_pid = _read_bitbake_server_pid(run_dir)
                if bb_pid is not None and _bitbake_server_pid_verified(bb_pid, run_dir.parent.parent):
                    _kill_pid(bb_pid, signal.SIGINT)
                _graceful_wait(
                    ctx=_WaitCtx(
                        liveness=lambda: _ALIVE if _host_build_alive(pgid, run_dir) else _DEAD,
                        escalate=lambda: _escalate_host(pgid, run_dir),
                        target_desc=f"PGID {pgid}" if pgid else "detached cooker",
                        run_dir=run_dir,
                        grace_seconds=grace_seconds,
                    )
                )
            else:
                label = f"PGID {pgid}" if pgid else "detached cooker"
                _say(f"Force-stopping build ({label}) - SIGTERM -> SIGKILL...")
                _escalate_host(pgid, run_dir)
            _report_stale_cleanup(run_dir, cfg)
            reasons = _verify_clean(run_dir, pgid, cfg=cfg)
            if reasons:
                _say("stop incomplete - the following remain:")
                for reason in reasons:
                    _say(f"  - {reason}")
                return False
            _say("stopped")
            return True
        finally:
            remove_pid(run_dir)

    # Container mode: resolve and stop the container via the runtime daemon.
    # Do NOT gate on is_build_running/PGID liveness; the wrapper may be dead
    # while the container lives.
    if record.container_label is None:
        _say("cannot target build: run predates container tracking; stop it manually")
        # No container identity was ever recorded for this run, so there is
        # nothing left to preserve - unlike the _ERROR refusal below, wiping
        # the launch record here loses no lead worth keeping.
        remove_pid(run_dir)
        return False

    runtime = record.runtime or detect_runtime()
    if shutil.which(runtime) is None:
        _say(f"cannot target build: container runtime {runtime!r} is not installed")
        remove_pid(run_dir)
        return False

    # _container_id_status, not _container_id: the latter collapses a
    # confirmed-dead container and a failed runtime query into the same
    # `None` - live_workspace_runs now treats that same query failure as
    # "still live" (fail conservative), so collapsing it here to "dead"
    # would declare a false success and clean up stale state for a build
    # that may still be running. Only a confirmed _DEAD reaches the
    # idempotent clean-tree stop below; an _ERROR refuses without
    # claiming a stop happened.
    container_status, cid = _container_id_status(runtime, record.container_label)
    if container_status == _ERROR:
        _say(f"cannot confirm container state ({runtime!r} query failed); refusing to report a stop - resolve manually")
        # Deliberately does NOT remove_pid: this record still names a real
        # runtime and container_label, and an unanswerable query today may
        # answer tomorrow. Wiping it here - inside the try/finally below via
        # an early return - would destroy the only lead to retry against for
        # a build that may still be alive, which is exactly the data loss
        # this whole function exists to avoid on an ambiguous answer.
        return False

    if cid is None:
        # Confirmed dead: idempotent clean-tree stop - clear any stale
        # lock/sock and succeed (requirement 5).
        removed = _report_stale_cleanup(run_dir, cfg)
        _say("no running build container" + ("; cleaned stale lock/socket" if removed else ""))
        remove_pid(run_dir)
        return True

    stop_plan = _ContainerStopPlan(
        force=force,
        term_secs=_STOP_TERM_SECONDS,
        run_dir=run_dir,
        grace_seconds=grace_seconds,
    )
    stop_status = _stop_container(runtime, cid, stop_plan)
    # A runtime we lost contact with mid-wait is a hard failure (exit 1) -
    # and, like the pre-check _ERROR above, an unconfirmed outcome: the
    # container may still be running, so the launch record is left in place
    # rather than removed, matching the pre-check's own reasoning.
    if stop_status == "lost_runtime":
        return False

    if stop_status == "incomplete":
        # The container may be alive and kas-container mounts the build dir at
        # /work, so the host argv scan cannot see its cooker: it would call the
        # lock stale and delete it out from under a running build.
        _say(
            "leaving bitbake.lock/bitbake.sock in place: the build container is still running"
            " or could not be confirmed stopped"
        )
    else:
        _report_stale_cleanup(run_dir, cfg)
    # The runtime's own refusals lead the report: they say WHY the container
    # survived, which _verify_clean's "still running" cannot.
    reasons = [
        *stop_plan.failures,
        *_verify_clean(run_dir, None, runtime=runtime, container_label=record.container_label, cfg=cfg),
    ]
    if reasons:
        _say("stop incomplete - the following remain:")
        for reason in reasons:
            _say(f"  - {reason}")
        # Same reasoning as lost_runtime above: a non-empty reasons list
        # means the container is confirmed still running, or its state
        # could not be confirmed - either way the launch record must
        # survive so a later attempt can still find and target this build.
        return False
    _say("stopped")
    remove_pid(run_dir)
    return True


# ---------------------------------------------------------------------------
# Workspace-wide live-build discovery.
#
# ``bakar stop`` (and any future host-wide listing, e.g. ``bakar ps``) needs
# to see every family root in a workspace, not just the one CLI args happened
# to resolve. This section defines its own literal root list directly - it
# does NOT import anything from ``bakar.commands.*`` (this module has no such
# import today) - and resolves a per-root :class:`~bakar.config.BuildConfig`
# directly via :func:`bakar.config.resolve`, which lives in the project's
# core configuration module rather than the CLI command layer.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRoot:
    """One family/build root scanned for workspace-wide live-build discovery.

    ``bsp_root`` is where ``build/runs`` actually lives on disk.
    ``resolve_workspace``/``resolve_family`` are the two :func:`resolve`
    inputs that reproduce it: the nxp/ti roots pass the top-level workspace
    with their own family (``resolve()`` computes ``bsp_root = workspace /
    bsp_family`` for those), while the plain ``build/runs`` root and every
    ``build-*`` root pass the root itself with ``bsp_family="bbsetup"``,
    whose :attr:`BuildConfig.bsp_root` is unconditionally its own
    ``workspace`` input - mirroring
    ``resolve_run_config`` in ``commands/_helpers.py`` (not reused directly,
    because this module must not import ``bakar.commands.*``).

    A caller with just one already-known root - group 9's future host-mode
    correlation, scanning a topdir discovered via a ``/proc`` walk with no
    workspace in hand - builds a single ``RunRoot`` the same way instead of
    going through :func:`_workspace_roots`.
    """

    bsp_root: Path
    family: Literal["nxp", "ti", "generic"]
    resolve_workspace: Path
    resolve_family: Literal["nxp", "ti", "bbsetup"]

    @property
    def runs_dir(self) -> Path:
        """``bsp_root/build/runs``.

        Always literally ``"build"``, never ``build_dir_name`` off the
        resolved config: ``resolve_family`` is never ``"qcom"`` here (the only
        family whose ``build_dir_name`` differs), so the two agree by
        construction.
        """
        return self.bsp_root / "build" / "runs"


@dataclass(frozen=True)
class SkippedRoot:
    """A candidate root excluded from discovery by the NFS lock-ownership gate."""

    root: RunRoot
    refusal: LockRefusal


@dataclass(frozen=True)
class RunCandidate:
    """One run directory discovered under a scanned root, with its root's config."""

    run_dir: Path
    root: RunRoot
    cfg: BuildConfig


@dataclass(frozen=True)
class RunScan:
    """Result of :func:`enumerate_workspace_runs`: what was found, and what was skipped."""

    candidates: list[RunCandidate]
    skipped: list[SkippedRoot]


def _workspace_roots(workspace: Path) -> list[RunRoot]:
    """The four literal family/build roots this change covers for ``workspace``.

    ``<workspace>/nxp``, ``<workspace>/ti``, ``<workspace>`` itself (plain
    ``build/runs``), and every ``<workspace>/build-*`` (meta-avocado/generic
    fanout). Does not cover preset-fanout archival subdirectories or a
    non-``"build"`` TOPDIR name - out of scope per design.md's Non-Goals.
    This list is this module's own literal (Assumption A5): it is defined
    directly here, never imported from ``bakar.commands.*``.
    """
    roots = [
        RunRoot(bsp_root=workspace / "nxp", family="nxp", resolve_workspace=workspace, resolve_family="nxp"),
        RunRoot(bsp_root=workspace / "ti", family="ti", resolve_workspace=workspace, resolve_family="ti"),
        RunRoot(bsp_root=workspace, family="generic", resolve_workspace=workspace, resolve_family="bbsetup"),
    ]
    try:
        build_dirs = sorted(p for p in workspace.glob("build-*") if p.is_dir())
    except OSError:
        build_dirs = []
    roots.extend(
        RunRoot(bsp_root=p, family="generic", resolve_workspace=p, resolve_family="bbsetup") for p in build_dirs
    )
    return roots


def enumerate_workspace_runs(path: Path, *, user_config: UserConfig | None = None) -> RunScan:
    """Enumerate every candidate run directory across accessible roots.

    ``path`` is either a full workspace (scanned for the four
    :func:`_workspace_roots` family/build roots) or a bare runs directory -
    e.g. ``<topdir>/build/runs``, the shape a host-mode ``/proc`` scan
    discovers without ever resolving a workspace. bakar always names this
    directory literally ``"runs"`` (:attr:`BuildConfig.runs_dir`), so a
    ``path`` whose final component is ``"runs"`` is treated as one
    pre-resolved root instead of being probed for ``nxp``/``ti``/``build``/
    ``build-*`` subdirectories.

    For each candidate root, :func:`bakar.config.resolve` is called directly
    - only the workspace path is a required input; ``workspace_config`` (the
    per-workspace ``.bakar.toml`` tier) is auto-loaded by ``resolve()``
    itself from each root's own ``resolve_workspace``, with no action needed
    here. ``user_config`` (the ``~/.config/bakar/config.toml`` tier) is NOT
    auto-loaded - pass it explicitly via this function's own ``user_config``
    parameter when the caller has one (e.g. from the CLI layer's
    ``_state._USER_CONFIG``), or every root resolves as if that tier were
    empty, silently diverging from a caller that resolves the same
    workspace with it supplied. A root whose family directory does not exist
    on disk contributes nothing and is never resolved. A root whose
    resolution raises (e.g. a malformed ``.bakar.toml``) is excluded from
    the result entirely - the same isolation a malformed launch record gets
    in :func:`live_workspace_runs` below - so one broken root cannot crash
    the whole scan.

    The resolved config gates two things: the NFS lock-ownership check runs
    BEFORE anything under the root is read - a root the gate refuses is
    excluded from ``candidates`` and reported in :attr:`RunScan.skipped` with
    the refusal reason, never silently treated as "nothing running there" -
    and it supplies every run discovered under that root with its
    family/machine metadata, since that is a property of the root, not of
    the individual run.
    """
    if path.name == "runs":
        bsp_root = path.parent.parent
        # Mirror _workspace_roots' own per-root convention: an nxp/ti root is
        # identified by its directory name and resolved against its PARENT
        # workspace with the matching family, exactly as _workspace_roots
        # builds `workspace / "nxp"` and `workspace / "ti"`. Anything else
        # (a bare `build/runs` root, or a `build-*` fanout root) falls back
        # to the generic/bbsetup resolution the previous unconditional
        # assignment always used - a host-mode /proc scan has no workspace
        # in hand, so the directory name is the only signal available here.
        if bsp_root.name in ("nxp", "ti"):
            roots = [
                RunRoot(
                    bsp_root=bsp_root,
                    family=bsp_root.name,
                    resolve_workspace=bsp_root.parent,
                    resolve_family=bsp_root.name,
                )
            ]
        else:
            roots = [RunRoot(bsp_root=bsp_root, family="generic", resolve_workspace=bsp_root, resolve_family="bbsetup")]
    else:
        roots = _workspace_roots(path)

    candidates: list[RunCandidate] = []
    skipped: list[SkippedRoot] = []
    for root in roots:
        if not root.bsp_root.is_dir():
            continue
        try:
            cfg = resolve(
                ResolveRequest(
                    workspace=root.resolve_workspace, bsp_family=root.resolve_family, user_config=user_config
                )
            )
        except Exception as exc:  # noqa: BLE001 - one broken root's config must not crash the scan
            # Not recorded in `skipped`: that list is reserved for the NFS
            # lock-ownership gate's own refusal reasons (SkippedRoot.refusal
            # is typed as LockRefusal), and a config-resolution failure - a
            # malformed .bakar.toml, for instance - is a different failure
            # class. Logged instead so the drop is traceable rather than
            # fully silent.
            _logger.warning("skipping root %s: config resolution failed: %s", root.bsp_root, exc)
            continue
        refusal = lock_mutation_guard(cfg)
        if refusal is not None:
            skipped.append(SkippedRoot(root=root, refusal=refusal))
            continue
        try:
            run_dirs = sorted(root.runs_dir.iterdir())
        except OSError:
            continue
        candidates.extend(RunCandidate(run_dir=d, root=root, cfg=cfg) for d in run_dirs if d.is_dir())

    return RunScan(candidates=candidates, skipped=skipped)


def is_candidate_live(candidate: RunCandidate) -> bool:
    """Report whether ``candidate`` is a currently live build.

    Host mode: the launch record parses as ``mode="host"`` and
    :func:`is_build_running` confirms a live, cmdline-verified PGID.
    Container mode: a recorded container label is a NECESSARY but not
    sufficient signal - queries the runtime via :func:`_container_id_status`
    directly, not :func:`_container_id`, since an unanswerable query
    (``_ERROR``) must not be read as "dead"; only a confirmed ``_DEAD``
    excludes the candidate. Anything else (no ``container_label`` and not
    host mode) is not live. Extracted from :func:`live_workspace_runs` so a
    caller in another module can evaluate a single candidate's liveness
    without going through the whole enumerate-and-filter pipeline; see that
    function's docstring for the full rationale behind each branch.
    """
    record = read_launch_record(candidate.run_dir)
    if record.mode == "host":
        alive, _pgid, cmdline_ok = is_build_running(candidate.run_dir)
        return alive and cmdline_ok
    if record.container_label is not None:
        runtime = record.runtime or detect_runtime()
        status, _cid = _container_id_status(runtime, record.container_label)
        return status != _DEAD
    return False


def live_workspace_runs(path: Path, *, user_config: UserConfig | None = None) -> list[RunCandidate]:
    """Live-only filter over :func:`enumerate_workspace_runs`'s candidates.

    Host mode: the launch record parses as ``mode="host"`` and
    :func:`is_build_running` confirms a live, cmdline-verified PGID.
    Container mode: a recorded container label is a NECESSARY but not
    sufficient signal - :func:`remove_pid` deletes the launch record on
    every stop path that reaches it, so a label surviving on disk usually
    means either a genuinely live container or one that exited outside
    ``bakar stop``'s own cleanup (a runtime restart, a manual ``docker
    kill``, a host crash). Treating the label alone as "live" without
    querying the runtime made every such stale record permanently
    unreachable: it counted toward "two or more live builds" in the
    no-``--run`` refuse-and-list branch, yet a targeted ``--run``/pick on it
    reported "not currently live" - a workspace could accumulate stale
    container records with no path that ever cleaned them up. Querying the
    runtime here (the same call an explicit target already had to make)
    closes that gap for every caller of this function, not only the
    explicitly-targeted one.

    The query uses :func:`_container_id_status` directly, not
    :func:`_container_id` - the two-state collapse the latter does (a
    matching container found, or not - folding "confirmed dead" and "the
    query itself failed" into the same "no id" result) is wrong here. An
    unanswerable query (missing runtime binary, unreachable daemon,
    timeout) is not proof the container is gone, and treating it as such
    would drop a genuinely live build out of discovery the moment the
    runtime has a hiccup - reporting "no live builds" or "not currently
    live" for a build that is still running. Only a confirmed ``_DEAD``
    excludes a candidate; ``_ERROR`` fails conservative and keeps it,
    matching how the NFS lock-ownership gate elsewhere in this module
    refuses rather than guesses when it cannot confirm an answer.

    A candidate whose launch record is malformed or unreadable is excluded
    rather than raising: :func:`read_launch_record` already degrades
    gracefully for that case (see its own docstring), so it never produces
    an entry this loop would need to special-case.

    ``user_config`` forwards verbatim to :func:`enumerate_workspace_runs` -
    see its own docstring for why this must be passed explicitly rather than
    relying on an auto-load.
    """
    return [
        candidate
        for candidate in enumerate_workspace_runs(path, user_config=user_config).candidates
        if is_candidate_live(candidate)
    ]


def correlate_host_discoveries(
    discovered: dict[Path, frozenset[int]], *, user_config: UserConfig | None = None
) -> list[RunCandidate]:
    """Correlate group 7's ``/proc``-walk discoveries to live run directories.

    ``discovered`` is :func:`_discover_host_cookers`'s return value: a topdir
    (the directory containing ``runs/``) mapped to the cooker PID(s) matched
    there. For each topdir, this scans ``topdir / "runs"`` via
    :func:`live_workspace_runs` - the same per-run enumeration/filter group 2
    already exposes for a bare runs-directory path, not a second reader of
    the launch-record format.

    A topdir whose ``runs/`` has no live candidate underneath it - a
    launch-record write race, or a cooker started outside the normal build
    entry point - is silently dropped: :func:`live_workspace_runs` already
    returns ``[]`` for that case, so no candidate is reported and none is
    fabricated.

    ``user_config`` forwards verbatim to :func:`live_workspace_runs` (and
    from there to :func:`enumerate_workspace_runs`) - see its docstring for
    why an omitted value resolves every root as if that config tier were
    empty.
    """
    candidates: list[RunCandidate] = []
    for topdir in discovered:
        candidates.extend(live_workspace_runs(topdir / "runs", user_config=user_config))
    return candidates


def dedup_across_sources(
    host_candidates: list[RunCandidate], container_candidates: list[ContainerCandidate]
) -> tuple[list[RunCandidate], list[ContainerCandidate]]:
    """Drop host-mode rows whose run id is already reported by container-mode discovery.

    Group 9's host-mode correlation and group 10/11's container-mode
    discovery are independent sources scanning the same set of running
    builds, so the same run id can legitimately show up in both: a
    container-mode build still has a launch record on the host, and
    :func:`correlate_host_discoveries` has no way to know the build it found
    is already reported by the container runtime. When a run id appears in
    both, the container-mode row wins - it carries the container id needed
    to stop the build - so the matching host candidate is dropped here
    rather than rendered as a second, less-actionable row for the same run.

    Container candidates are never dropped by this function; only host
    candidates colliding with a container-mode run id are removed. Both
    lists are returned - the shrunk host list and the unchanged container
    list - because group 15's ``bakar ps`` needs to render rows from both
    sources side by side.
    """
    container_run_ids = {candidate.run_id for candidate in container_candidates}
    deduped_host = [c for c in host_candidates if c.run_dir.name not in container_run_ids]
    return deduped_host, container_candidates


def _interrupted_step(run_dir: Path) -> str | None:
    """Return the name of an interrupted step from ``run_dir/events.jsonl``.

    Each line is a JSON object with an ``event`` discriminator
    (``step_start`` paired with one of the terminal events ``step_ok`` /
    ``step_fail`` / ``step_skip``) and a coarse ``step`` label such as
    ``kas_build`` (NOT a recipe name). A step whose ``step_start`` has no
    matching terminal event is the interrupted step. Returns ``None`` when the
    file is absent, unreadable, or contains no unmatched ``step_start``.

    An unparseable line is skipped rather than aborting the read: this runs on
    the interrupted-build path, which is exactly when the log ends in a
    half-written record, so giving up on it would report "no interrupted step"
    for the one case the lookup exists to answer.
    """
    events_path = run_dir / _EVENTS_FILENAME

    started: list[str] = []
    ended: set[str] = set()
    try:
        for obj in iter_run_events(events_path):
            event = obj.get("event")
            step = obj.get("step")
            if not isinstance(step, str):
                continue
            if event == "step_start":
                started.append(step)
            elif event in ("step_ok", "step_fail", "step_skip"):
                ended.add(step)
    except OSError:
        return None

    for step in started:
        if step not in ended:
            return step
    return None


def check_unclean_stop(bsp_root: Path, console: Console) -> None:
    """Warn at build start about a build already running or interrupted uncleanly.

    Scans every run dir under ``bsp_root/build/runs`` for a ``build.pid``. If a
    live build is found (PGID alive and cmdline verified), prints a warning that
    a build is already running and returns. Otherwise, for each stale pidfile
    (dead PGID), prints a warning naming the interrupted step (from
    ``events.jsonl``) and pointing at ``kas.log`` for the in-flight recipe.

    Never raises: the entire body is wrapped in ``try/except`` so a detection
    bug cannot block a build. Returns ``None`` in all cases.
    """
    try:
        runs_dir = bsp_root / "build" / "runs"
        try:
            run_dirs = sorted(runs_dir.iterdir())
        except OSError:
            return

        for run_dir in run_dirs:
            if not (run_dir / _PID_FILENAME).exists():
                continue

            live, pgid, cmdline_ok = is_build_running(run_dir)
            if live and cmdline_ok:
                console.print(
                    Panel.fit(
                        f"A build is already running (PGID {pgid}) in\n"
                        f"  {run_dir}\n\n"
                        f"Stop it first with [bold]bakar stop[/] before starting a new build.",
                        title="[bold yellow]build already running[/]",
                        border_style="yellow",
                    )
                )
                return

            if live:
                continue

            step = _interrupted_step(run_dir)
            during = f" during step [bold]{step}[/]" if step is not None else ""
            body = (
                f"The previous build in\n"
                f"  {run_dir}\n"
                f"was interrupted uncleanly{during}.\n\n"
                f"Check [bold]kas.log[/] in that run dir for the recipe that was building."
            )
            console.print(
                Panel.fit(
                    body,
                    title="[bold yellow]previous build interrupted uncleanly[/]",
                    border_style="yellow",
                )
            )
    except Exception:  # noqa: BLE001 - safety guard; detection bug must never block a build
        return
