"""Workspace-scoped bitbake-hashserv daemon helpers.

This module owns the lifecycle of a per-workspace ``bitbake-hashserv``
daemon. A persistent daemon lets cross-build sstate hash equivalence
accumulate instead of being rebuilt from scratch on every ``bakar
build`` (which is what ``BB_HASHSERVE = "auto"`` does).

The daemon binary is sourced exclusively from the synced workspace at
``<bsp_root>/sources/poky/bitbake/bin/bitbake-hashserv``. We deliberately
do NOT fall back to a host PATH lookup: the daemon's wire protocol must
match the bitbake the build will run against, and the workspace bitbake
is the only version we can guarantee that for.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from hashlib import sha256
from pathlib import Path

_PID_FILENAME = "hashserv.pid"
_PORT_FILENAME = "hashserv.port"
_DB_FILENAME = "hashserv.db"
_STDERR_FILENAME = "hashserv.stderr"
_STATE_SUBDIR = ".bakar"
_PORT_FLOOR = 49152
_PORT_SPAN = 16383
_TERM_GRACE_SECONDS = 5
_STARTUP_PROBE_DEADLINE_SECONDS = 2.0


def _workspace_port(bsp_root: Path) -> int:
    """Derive a stable ephemeral port from the workspace path.

    Two workspaces on the same machine must not collide; a random pick
    would need a port-file lookup to be authoritative. Hashing
    ``realpath(bsp_root)`` into the 49152-65534 range gives a stable URL
    for the lifetime of the workspace without making any state file
    load-bearing for routing.
    """
    digest = sha256(str(bsp_root.resolve()).encode()).hexdigest()
    return _PORT_FLOOR + int(digest[:8], 16) % _PORT_SPAN


def _find_binary(bsp_root: Path) -> Path | None:
    """Return the workspace ``bitbake-hashserv`` path, or None if absent.

    Only workspace paths are consulted - no host PATH fallback. A PATH binary
    may be from a different bitbake version whose hashserv wire protocol does
    not match the workspace bitbake, which silently corrupts the equivalence
    cache.

    Search order:
    1. ``<bsp_root>/sources/poky/bitbake/``  - NXP (poky umbrella)
    2. ``<bsp_root>/sources/bitbake/``       - TI (bare oe-core)
    3. ``<bsp_root>/bitbake/``               - generic workspace-root bitbake
    4. ``<bsp_root.parent>/bitbake/``        - meta-avocado style (bsp_root is
                                              ``<workspace>/build-<stem>``; the
                                              workspace ships bitbake as a sibling)
    """
    for candidate in (
        bsp_root / "sources" / "poky" / "bitbake" / "bin" / "bitbake-hashserv",
        bsp_root / "sources" / "bitbake" / "bin" / "bitbake-hashserv",
        bsp_root / "bitbake" / "bin" / "bitbake-hashserv",
        bsp_root.parent / "bitbake" / "bin" / "bitbake-hashserv",
    ):
        if candidate.is_file():
            return candidate
    return None


def _state_dir(bsp_root: Path) -> Path:
    """Return ``<bsp_root>/.bakar`` (the workspace state directory)."""
    return bsp_root / _STATE_SUBDIR


def _read_pid(bsp_root: Path) -> int | None:
    """Return the PID recorded for the workspace daemon, or None on any failure.

    Treats every error path - missing file, unreadable file, unparseable
    content - as "no recorded PID". Callers cannot distinguish the
    failures and do not need to: each one means "we have no live
    reference to a daemon".
    """
    pid_file = _state_dir(bsp_root) / _PID_FILENAME
    try:
        raw = pid_file.read_text()
    except OSError:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def is_running(bsp_root: Path) -> bool:
    """Return True iff the recorded PID is alive AND its cmdline names the daemon.

    The cmdline check guards against PID recycling: a host reboot or a
    long-lived workspace can leave a stale PID file pointing at a now-
    unrelated process. ``/proc/<pid>/cmdline`` is NUL-separated, so we
    read bytes and look for the binary name as a substring rather than
    splitting on NUL.
    """
    pid = _read_pid(bsp_root)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM means the process exists but is owned by someone else -
        # treat that as "alive" and let the cmdline check below decide.
        pass
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        cmdline_bytes = cmdline_path.read_bytes()
    except OSError:
        return False
    return b"bitbake-hashserv" in cmdline_bytes


def ensure_running(bsp_root: Path) -> str | None:
    """Ensure a workspace-scoped hashserv daemon is running; return its URL.

    Returns ``f"ws://localhost:{port}"`` when the daemon is reachable (either
    already running, or freshly spawned and passed the TCP startup probe).
    Returns ``None`` silently when the workspace bitbake-hashserv binary has
    not been synced yet, or when a fresh spawn never reached the TCP probe
    success within ``_STARTUP_PROBE_DEADLINE_SECONDS`` (in which case the
    daemon's stderr is captured to ``<state_dir>/hashserv.stderr`` and any
    surviving child process is sent SIGTERM).

    The PID/port files are only written after the TCP probe succeeds, so a
    failed startup never leaves authoritative state behind for the next
    invocation to mis-interpret.
    """
    state_dir = _state_dir(bsp_root)
    port_file = state_dir / _PORT_FILENAME
    pid_file = state_dir / _PID_FILENAME

    if is_running(bsp_root):
        try:
            port = int(port_file.read_text().strip())
        except FileNotFoundError, ValueError:
            # Port file missing or corrupt (PID/port written non-atomically;
            # crash between the two writes leaves an orphan PID file). Fall
            # through to re-spawn below by treating the daemon as stopped.
            pid_file.unlink(missing_ok=True)
            port_file.unlink(missing_ok=True)
        else:
            return f"ws://localhost:{port}"

    binary = _find_binary(bsp_root)
    if binary is None:
        return None

    port = _workspace_port(bsp_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    # Redirect daemon stderr directly to a log file rather than PIPE so the
    # daemon never blocks when the kernel pipe buffer fills (default 64 KiB)
    # on verbose or long-running builds.
    stderr_log = state_dir / _STDERR_FILENAME
    stderr_fh = stderr_log.open("wb")
    proc = subprocess.Popen(
        [
            str(binary),
            "--bind",
            f"ws://localhost:{port}",
            "--database",
            str(state_dir / _DB_FILENAME),
        ],
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        start_new_session=True,
    )
    stderr_fh.close()

    deadline = time.monotonic() + _STARTUP_PROBE_DEADLINE_SECONDS
    while True:
        if proc.poll() is not None:
            _abort_startup(proc, state_dir)
            return None
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        except OSError:
            if time.monotonic() > deadline:
                _abort_startup(proc, state_dir)
                return None
            time.sleep(0.1)
            continue
        sock.close()
        pid_file.write_text(f"{proc.pid}\n")
        port_file.write_text(f"{port}\n")
        return f"ws://localhost:{port}"


def stop(bsp_root: Path) -> bool:
    """Stop the workspace daemon and clean PID/port state files.

    Returns False (no-op) when no PID file is recorded - the daemon was
    never started, or its state has already been cleaned up. Returns True
    in every other case, even when the recorded PID was already dead by
    the time we tried to signal it: a missing process is a successful
    stop, not a failure.

    The SQLite database under ``<state_dir>/hashserv.db`` is deliberately
    preserved across stop/start cycles. The whole reason to run a
    persistent daemon is to keep the accumulated hash equivalence cache;
    wiping the DB here would defeat that. Workspace teardown
    (``bakar clean --all``) is the one path that removes the DB.
    """
    pid = _read_pid(bsp_root)
    if pid is None:
        return False

    state_dir = _state_dir(bsp_root)
    pid_file = state_dir / _PID_FILENAME
    port_file = state_dir / _PORT_FILENAME

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while is_running(bsp_root) and time.monotonic() < deadline:
        time.sleep(0.1)

    if is_running(bsp_root):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + 1.0
        while is_running(bsp_root) and time.monotonic() < kill_deadline:
            time.sleep(0.1)

    pid_file.unlink(missing_ok=True)
    port_file.unlink(missing_ok=True)
    return True


def _abort_startup(proc: subprocess.Popen[bytes], state_dir: Path) -> None:
    """Tear down a failed daemon spawn.

    Sends SIGTERM to the spawned process if it is still alive (the OS may
    have reaped it already - ProcessLookupError is benign here). The daemon's
    stderr was already redirected to <state_dir>/hashserv.stderr at spawn
    time, so no explicit drain is needed here.

    Deliberately does NOT touch the PID or port file: a failed startup must
    leave no authoritative state for the next call.
    """
    if proc.poll() is None:
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
