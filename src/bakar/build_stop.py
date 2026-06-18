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
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.panel import Panel

if TYPE_CHECKING:
    from rich.console import Console

_PID_FILENAME = "build.pid"
_VALID_CMDLINE_TOKENS = ("kas-container", "kas")
_STOP_GRACE_SECONDS = 60
_STOP_TERM_SECONDS = 5
_EVENTS_FILENAME = "events.jsonl"


def write_pid(run_dir: Path, pgid: int) -> None:
    """Write ``pgid`` as a single decimal line to ``run_dir/build.pid``."""
    (run_dir / _PID_FILENAME).write_text(f"{pgid}\n")


def remove_pid(run_dir: Path) -> None:
    """Remove ``run_dir/build.pid``; no-op if it is already absent."""
    (run_dir / _PID_FILENAME).unlink(missing_ok=True)


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


def stop_build(bsp_root: Path, *, force: bool = False) -> None:
    """Stop the most recent build by signalling its recorded process group.

    Resolves the lexically-latest run dir under ``bsp_root/build/runs`` and
    reads its ``build.pid``. When ``force`` is False, sends ``SIGINT`` and waits
    up to ``_STOP_GRACE_SECONDS`` for a graceful exit before escalating to
    ``SIGTERM`` then ``SIGKILL``. When ``force`` is True, skips ``SIGINT`` and
    goes straight to ``SIGTERM`` then ``SIGKILL``. The pidfile is always removed
    before returning, even if a signal call raises.
    """
    runs_dir = bsp_root / "build" / "runs"
    try:
        run_dirs = sorted(runs_dir.iterdir())
    except OSError:
        print("no running build found")
        return
    if not run_dirs:
        print("no running build found")
        return

    run_dir = run_dirs[-1]
    live, pgid, cmdline_ok = is_build_running(run_dir)
    if not live or not cmdline_ok or pgid is None:
        # Dead or recycled PGID: clear the stale pidfile so a later build's
        # check_unclean_stop stops re-warning about it.
        remove_pid(run_dir)
        print("no running build found")
        return

    try:
        if not force:
            print(f"Sent SIGINT to build PGID {pgid}...")
            os.killpg(pgid, signal.SIGINT)
            for _ in range(_STOP_GRACE_SECONDS):
                if not _pgid_alive(pgid):
                    print("stopped")
                    return
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
    except Exception:
        return
