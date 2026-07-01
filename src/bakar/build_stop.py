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

from rich.panel import Panel

if TYPE_CHECKING:
    from rich.console import Console

    from bakar.config import BuildConfig
    from bakar.observability import RunLogger

_PID_FILENAME = "build.pid"
_META_FILENAME = "build.meta.json"
_VALID_CMDLINE_TOKENS = ("kas-container", "kas")
_STOP_GRACE_SECONDS = 60
_STOP_TERM_SECONDS = 5
_EVENTS_FILENAME = "events.jsonl"
_RUN_ID_LABEL_KEY = "bakar.run_id"


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


def _detect_runtime() -> str:
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


def _stop_container(
    runtime: str,
    cid: str,
    *,
    force: bool,
    grace_secs: int,
    term_secs: int,
) -> None:
    """Stop container ``cid`` via ``runtime`` with graceful escalation.

    When ``force`` is False: send SIGINT to bitbake inside the container first
    (via :func:`_sigint_bitbake_in_container`, falling back to a container-PID-1
    SIGINT if the exec fails), poll ``inspect`` once per second for up to
    ``grace_secs`` (stopping early once the container is no longer running),
    then ``stop --timeout=<term_secs>`` and finally ``kill --signal=SIGKILL``.
    When ``force`` is True: skip the SIGINT step and go straight to ``stop``
    then ``kill --signal=SIGKILL``.

    Uses ``--timeout`` (docker >= 29 deprecates ``--time``). Every subprocess
    call captures output and never raises on a non-zero exit.
    """
    if not force:
        print(f"Sent SIGINT to bitbake in container {cid}...")
        if not _sigint_bitbake_in_container(runtime, cid):
            _run_runtime([runtime, "kill", "--signal=SIGINT", cid])
        for _ in range(grace_secs):
            if not _container_running(runtime, cid):
                print("stopped")
                return
            time.sleep(1)
        print("escalating to SIGTERM")
    else:
        print(f"Sending SIGTERM to container {cid}...")

    _run_runtime([runtime, "stop", f"--timeout={term_secs}", cid])
    if _container_running(runtime, cid):
        print("escalating to SIGKILL")
    _run_runtime([runtime, "kill", "--signal=SIGKILL", cid])
    print("stopped")


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
        runtime = _detect_runtime()
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


def stop_build(bsp_root: Path, *, force: bool = False) -> bool:
    """Stop the most recent build, targeting it by execution mode.

    Scans run dirs under ``bsp_root/build/runs`` newest-first and targets the
    first whose build is still live (host: a verified live PGID; container: a
    recorded container label). Taking only the lexically-latest run missed a
    live build whenever a later clean-recipe or second build left a newer but
    finished run dir. Returns ``True`` when a build was targeted (host PGID
    signalled or container stopped), ``False`` when none is targetable (no run
    dir, or every run is finished/unresolvable).

    Host mode keeps the existing ``os.killpg`` SIGINT -> SIGTERM -> SIGKILL
    escalation. Container mode resolves the container via its recorded label and
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
                for _ in range(_STOP_GRACE_SECONDS):
                    if not _pgid_alive(pgid):
                        print("stopped")
                        return True
                    time.sleep(1)
                print("escalating to SIGTERM")
            else:
                print(f"Sent SIGTERM to build PGID {pgid}...")

            os.killpg(pgid, signal.SIGTERM)
            time.sleep(_STOP_TERM_SECONDS)
            if _pgid_alive(pgid):
                print("escalating to SIGKILL")
                os.killpg(pgid, signal.SIGKILL)
            print("stopped")
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

        runtime = record.runtime or _detect_runtime()
        if shutil.which(runtime) is None:
            print(f"cannot target build: container runtime {runtime!r} is not installed")
            return False

        cid = _container_id(runtime, record.container_label)
        if cid is None:
            print("no running build container found")
            return False

        _stop_container(
            runtime,
            cid,
            force=force,
            grace_secs=_STOP_GRACE_SECONDS,
            term_secs=_STOP_TERM_SECONDS,
        )
        return True
    finally:
        remove_pid(run_dir)


def _interrupted_step(run_dir: Path) -> str | None:
    """Return the name of an interrupted step from ``run_dir/events.jsonl``.

    Each line is a JSON object with an ``event`` discriminator
    (``step_start`` / ``step_end``) and a coarse ``step`` label such as
    ``kas_build`` (NOT a recipe name). A step whose ``step_start`` has no
    matching ``step_end`` is the interrupted step. Returns ``None`` when the
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
        elif event == "step_end":
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
