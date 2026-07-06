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
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel

from bakar.eventlog import running_tasks

if TYPE_CHECKING:
    from collections.abc import Callable

    from bakar.config import BuildConfig
    from bakar.eventlog import RunningTask
    from bakar.observability import RunLogger

_PID_FILENAME = "build.pid"
_META_FILENAME = "build.meta.json"
_VALID_CMDLINE_TOKENS = ("kas-container", "kas")
_STOP_TERM_SECONDS = 5
_EVENTS_FILENAME = "events.jsonl"
_RUN_ID_LABEL_KEY = "bakar.run_id"

# Stale bitbake artifacts left in the build TOPDIR by a forced/killed cooker.
# A stale ``bitbake.lock`` blocks the next build ("Cannot lock ... bitbake.lock").
# ``bitbake-cookerdaemon.log`` is a diagnostic log (like kas.log) and is kept.
_STALE_BITBAKE_FILES = ("bitbake.lock", "bitbake.sock")

# Wait-loop tuning. The graceful wait is UNBOUNDED (no grace cap); these only
# govern the live-progress view and the runtime-death guard, never how long we
# are willing to wait for a build to drain.
_STOP_POLL_SECONDS = 1.0  # liveness/render cadence
_STOP_STALE_SECONDS = 10.0  # running-set unchanged this long -> spinner fallback
_STOP_HINT_SECONDS = 30.0  # cadence of the "press Ctrl-C to force" hint
_RUNTIME_ERROR_CAP = 5  # consecutive container-query errors before giving up

# Liveness tri-state. ``_ERROR`` is a query that could not be answered (a
# transient runtime failure), distinct from a definitive ``_DEAD``; the wait
# loop keeps polling on a single ``_ERROR`` and only concludes the runtime is
# gone after ``_RUNTIME_ERROR_CAP`` in a row.
_ALIVE = "alive"
_DEAD = "dead"
_ERROR = "error"

# Module-level Rich console for the out-of-process ``bakar stop`` wait view.
# build_stop sits below the commands tier, so it cannot import the shared
# console from ``commands._app``; it owns its own.
console = Console()


def run_id_label(run_id: str) -> str:
    """Return the container label (``bakar.run_id=<run_id>``) for a build run.

    Single source of truth shared by the launch-time ``--label`` injection in
    ``kas_build``, the recorded ``container_label``, and the
    ``docker|podman ps -f label=`` query, so the three never drift apart.
    """
    return f"{_RUN_ID_LABEL_KEY}={run_id}"


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


# Back-compat alias: steps/kas_build.py (owned by a different task/round) still
# imports this module's runtime detection as ``_detect_runtime``. Keep the old
# private name bound to the same function until that call site is migrated,
# so this rename does not break an out-of-scope module mid-round.
_detect_runtime = detect_runtime


def _container_id(runtime: str, container_label: str) -> str | None:
    """Resolve the running container id for ``container_label`` via ``runtime``.

    Runs ``<runtime> ps -q -f label=<container_label>`` and returns the first
    line of stdout (a container id), or ``None`` when the output is empty or the
    command errors (the container is gone or the runtime is unusable).
    """
    try:
        result = subprocess.run(
            [runtime, "ps", "-q", "-f", f"label={container_label}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        cid = line.strip()
        if cid:
            return cid
    return None


def _run_runtime(args: list[str]) -> None:
    """Run a runtime subcommand, capturing output and swallowing all errors.

    A missing or already-gone container is not an error here, so a non-zero
    exit (or the runtime binary being absent) is ignored rather than raised.
    """
    try:
        subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError:
        pass


def _container_running(runtime: str, cid: str) -> bool:
    """Return True while ``cid`` reports ``State.Running == true``.

    Anything else (the inspect command erroring, empty output, ``"false"``)
    means the container is no longer running.
    """
    try:
        result = subprocess.run(
            [runtime, "inspect", "-f", "{{.State.Running}}", cid],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "true"


def _container_liveness(runtime: str, cid: str) -> str:
    """Return the tri-state liveness of container ``cid``.

    Unlike :func:`_container_running`, this distinguishes a definitive
    not-running result from a query that could not be answered:

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
        )
    except OSError:
        return _ERROR
    if result.returncode != 0:
        return _ERROR
    return _ALIVE if result.stdout.strip() == "true" else _DEAD


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
        )
    except OSError:
        return False
    return result.returncode == 0


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


def _graceful_wait(
    *,
    liveness: Callable[[], str],
    escalate: Callable[[], None],
    target_desc: str,
    run_dir: Path | None = None,
    console_out: Console | None = None,
    error_cap: int = _RUNTIME_ERROR_CAP,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    tasks_reader: Callable[[Path], list[RunningTask]] = running_tasks,
    poll_interval: float = _STOP_POLL_SECONDS,
    stale_after: float = _STOP_STALE_SECONDS,
    hint_interval: float = _STOP_HINT_SECONDS,
    install_signal: bool = True,
) -> str:
    """Wait UNBOUNDED until ``liveness()`` says the target is gone.

    The exit gate is liveness, never ``tasks == 0``: bitbake may still finalize
    (sstate writes, cooker shutdown) after the last task drains, so only a
    ``_DEAD`` liveness result ends the wait. Returns one of:

    - ``"drained"``      - ``liveness()`` returned ``_DEAD``;
    - ``"escalated"``    - a Ctrl-C fired the SIGTERM->SIGKILL ``escalate()`` ladder;
    - ``"lost_runtime"`` - ``error_cap`` consecutive ``_ERROR`` liveness queries
      (container runtime unreachable); the caller should exit 1.

    A single ``_ERROR`` keeps waiting (re-query before concluding). Progress is
    read from ``tasks_reader(run_dir)``; when the running set is empty or has not
    changed for ``stale_after`` seconds (a frozen event log), the view degrades to
    a spinner + elapsed with a periodic Ctrl-C hint so stale, non-decrementing
    rows are never left on screen.

    ``clock``/``sleep``/``liveness``/``tasks_reader`` are injectable seams so the
    branching logic is unit-testable without real sleeps or signals; pass
    ``install_signal=False`` to skip the SIGINT handler in tests.
    """
    out = console_out if console_out is not None else console
    start = clock()
    error_streak = 0
    last_signature: frozenset[tuple[str, str]] | None = None
    last_change = start
    last_hint = start

    prev_handler = None
    if install_signal:
        prev_handler = signal.signal(signal.SIGINT, _wait_sigint_handler)  # pragma: no cover
    try:
        while True:
            status = liveness()
            if status == _DEAD:
                return "drained"
            if status == _ERROR:
                error_streak += 1
                if error_streak >= error_cap:
                    return "lost_runtime"
            else:
                error_streak = 0

            now = clock()
            elapsed = now - start
            tasks = tasks_reader(run_dir) if run_dir is not None else []
            signature = frozenset((t.recipe, t.task) for t in tasks)
            if signature != last_signature:
                last_signature = signature
                last_change = now
            stale = (now - last_change) >= stale_after

            if tasks and not stale:
                _render_running(out, tasks, elapsed)
            else:
                show_hint = (now - last_hint) >= hint_interval
                if show_hint:
                    last_hint = now
                _render_spinner(out, elapsed, target_desc, show_hint=show_hint)

            sleep(poll_interval)
    except KeyboardInterrupt:
        escalate()
        return "escalated"
    finally:
        if prev_handler is not None:
            signal.signal(signal.SIGINT, prev_handler)  # pragma: no cover


def _escalate_container(runtime: str, cid: str, term_secs: int) -> None:
    """Run the container SIGTERM->SIGKILL ladder: ``stop --timeout`` then ``kill``."""
    _run_runtime([runtime, "stop", f"--timeout={term_secs}", cid])
    _run_runtime([runtime, "kill", "--signal=SIGKILL", cid])


def _stop_container(
    runtime: str,
    cid: str,
    *,
    force: bool,
    term_secs: int,
    run_dir: Path | None = None,
    console_out: Console | None = None,
) -> str:
    """Stop container ``cid`` via ``runtime`` with an unbounded graceful wait.

    When ``force`` is False: send SIGINT to bitbake inside the container first
    (via :func:`_sigint_bitbake_in_container`, falling back to a container-PID-1
    SIGINT if the exec fails), then wait UNBOUNDED via :func:`_graceful_wait`
    until the container is no longer running, rendering live progress. A Ctrl-C
    during the wait escalates through ``stop --timeout=<term_secs>`` ->
    ``kill --signal=SIGKILL``. When ``force`` is True: skip the SIGINT step and go
    straight to that escalation ladder.

    Returns the :func:`_graceful_wait` status (``"drained"``/``"escalated"``/
    ``"lost_runtime"``) for the graceful path, or ``"forced"`` for ``force=True``.
    ``"lost_runtime"`` tells the caller the runtime went unreachable (exit 1).

    Uses ``--timeout`` (docker >= 29 deprecates ``--time``). Every subprocess
    call captures output and never raises on a non-zero exit.
    """
    if not force:
        print(f"Sent SIGINT to bitbake in container {cid}...")
        if not _sigint_bitbake_in_container(runtime, cid):
            _run_runtime([runtime, "kill", "--signal=SIGINT", cid])
        status = _graceful_wait(
            liveness=lambda: _container_liveness(runtime, cid),
            escalate=lambda: _escalate_container(runtime, cid, term_secs),
            target_desc=f"container {cid}",
            run_dir=run_dir,
            console_out=console_out,
        )
        if status == "lost_runtime":
            print("lost contact with the container runtime")
        else:
            print("stopped")
        return status

    print(f"Sending SIGTERM to container {cid}...")
    _escalate_container(runtime, cid, term_secs)
    print("stopped")
    return "forced"


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
        cid = _container_id(runtime, run_id_label(log.run_id))
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


def _escalate_host(pgid: int) -> None:
    """Run the host SIGTERM->SIGKILL ladder against process group ``pgid``.

    SIGTERM, wait ``_STOP_TERM_SECONDS``, then SIGKILL only if the group is
    still alive - the existing escalation, unchanged.
    """
    os.killpg(pgid, signal.SIGTERM)
    time.sleep(_STOP_TERM_SECONDS)
    if _pgid_alive(pgid):
        os.killpg(pgid, signal.SIGKILL)


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


def _report_stale_cleanup(run_dir: Path) -> None:
    """Clean stale bitbake files for ``run_dir`` and print a one-line note."""
    removed = _clean_stale_bitbake_files(run_dir)
    if removed:
        print(f"removed stale bitbake files: {', '.join(p.name for p in removed)}")


def stop_build(bsp_root: Path, *, force: bool = False) -> bool:
    """Stop the most recent build, targeting it by execution mode.

    Scans run dirs under ``bsp_root/build/runs`` newest-first and targets the
    first whose build is still live (host: a verified live PGID; container: a
    recorded container label). Taking only the lexically-latest run missed a
    live build whenever a later clean-recipe or second build left a newer but
    finished run dir. Returns ``True`` when a build was targeted (host PGID
    signalled or container stopped), ``False`` when none is targetable (no run
    dir, or every run is finished/unresolvable).

    Host mode sends SIGINT then waits UNBOUNDED (via ``_graceful_wait``) until
    the PGID is gone, escalating through SIGTERM -> SIGKILL only on Ctrl-C or
    ``force``. Container mode resolves the container via its recorded label and
    stops it through the runtime daemon, without touching the wrapper PGID. The
    launch record (``build.pid`` + ``build.meta.json``) is always removed before
    returning.
    """
    runs_dir = bsp_root / "build" / "runs"
    try:
        run_dirs = sorted(runs_dir.iterdir())
    except OSError:
        print("no running build found")
        return False
    if not run_dirs:
        print("no running build found")
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
        elif candidate_record.container_label is not None:
            target = candidate
            break
    run_dir = target if target is not None else run_dirs[-1]
    record = read_launch_record(run_dir)
    if record.mode == "host":
        try:
            live, pgid, cmdline_ok = is_build_running(run_dir)
            if not live or not cmdline_ok or pgid is None:
                print("no running build found")
                return False
            if not force:
                print(f"Sent SIGINT to build PGID {pgid}...")
                os.killpg(pgid, signal.SIGINT)
                _graceful_wait(
                    liveness=lambda: _ALIVE if _pgid_alive(pgid) else _DEAD,
                    escalate=lambda: _escalate_host(pgid),
                    target_desc=f"PGID {pgid}",
                    run_dir=run_dir,
                )
            else:
                print(f"Sent SIGTERM to build PGID {pgid}...")
                _escalate_host(pgid)
            print("stopped")
            _report_stale_cleanup(run_dir)
            return True
        finally:
            remove_pid(run_dir)

    # Container mode: resolve and stop the container via the runtime daemon.
    # Do NOT gate on is_build_running/PGID liveness; the wrapper may be dead
    # while the container lives.
    try:
        if record.container_label is None:
            print("cannot target build: run predates container tracking; stop it manually")
            return False

        runtime = record.runtime or detect_runtime()
        if shutil.which(runtime) is None:
            print(f"cannot target build: container runtime {runtime!r} is not installed")
            return False

        cid = _container_id(runtime, record.container_label)
        if cid is None:
            print("no running build container found")
            return False

        status = _stop_container(
            runtime,
            cid,
            force=force,
            term_secs=_STOP_TERM_SECONDS,
            run_dir=run_dir,
        )
        # A drained/escalated/forced stop is success (exit 0); only a runtime we
        # lost contact with mid-wait is a failure (exit 1).
        if status != "lost_runtime":
            _report_stale_cleanup(run_dir)
        return status != "lost_runtime"
    finally:
        remove_pid(run_dir)


def _interrupted_step(run_dir: Path) -> str | None:
    """Return the name of an interrupted step from ``run_dir/events.jsonl``.

    Each line is a JSON object with an ``event`` discriminator
    (``step_start`` paired with one of the terminal events ``step_ok`` /
    ``step_fail`` / ``step_skip``) and a coarse ``step`` label such as
    ``kas_build`` (NOT a recipe name). A step whose ``step_start`` has no
    matching terminal event is the interrupted step. Returns ``None`` when the
    file is absent, unreadable, contains no unmatched ``step_start``, or any
    line fails to parse.
    """
    events_path = run_dir / _EVENTS_FILENAME
    try:
        raw = events_path.read_text()
    except OSError:
        return None

    started: list[str] = []
    ended: set[str] = set()
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError, TypeError:
            return None
        if not isinstance(obj, dict):
            return None
        event = obj.get("event")
        step = obj.get("step")
        if not isinstance(step, str):
            continue
        if event == "step_start":
            started.append(step)
        elif event in ("step_ok", "step_fail", "step_skip"):
            ended.add(step)

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
