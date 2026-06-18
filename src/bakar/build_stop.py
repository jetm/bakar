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

import os
import signal
import time
from pathlib import Path

_PID_FILENAME = "build.pid"
_VALID_CMDLINE_TOKENS = ("kas-container", "kas")
_STOP_GRACE_SECONDS = 60
_STOP_TERM_SECONDS = 5


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
    - ``live`` is True iff ``os.kill(pgid, 0)`` confirms the PGID exists.
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

    try:
        os.kill(pgid, 0)
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
    cmdline_ok = any(
        token.encode() in field for field in fields for token in _VALID_CMDLINE_TOKENS
    )
    return (True, pgid, cmdline_ok)


def _pgid_alive(pgid: int) -> bool:
    """Return True while ``pgid`` still exists; signal 0 probes liveness."""
    try:
        os.kill(pgid, 0)
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
        print("no running build found")
        return

    try:
        if force:
            print(f"Sent SIGTERM to build PGID {pgid}...")
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(_STOP_TERM_SECONDS)
            if _pgid_alive(pgid):
                print("escalating to SIGKILL")
                os.killpg(pgid, signal.SIGKILL)
            print("stopped")
            return

        print(f"Sent SIGINT to build PGID {pgid}...")
        os.killpg(pgid, signal.SIGINT)
        for _ in range(_STOP_GRACE_SECONDS):
            if not _pgid_alive(pgid):
                print("stopped")
                return
            time.sleep(1)

        print("escalating to SIGTERM")
        os.killpg(pgid, signal.SIGTERM)
        time.sleep(_STOP_TERM_SECONDS)
        if _pgid_alive(pgid):
            print("escalating to SIGKILL")
            os.killpg(pgid, signal.SIGKILL)
        print("stopped")
    finally:
        remove_pid(run_dir)
