"""Workspace-scoped bitbake-hashserv daemon helpers.

This module owns the lifecycle of a ``bitbake-hashserv`` daemon keyed to a
*state directory*. A persistent daemon lets cross-build sstate hash equivalence
accumulate instead of being rebuilt from scratch on every ``bakar build``
(which is what ``BB_HASHSERVE = "auto"`` does).

Two locations are kept deliberately separate:

- The **state key** (``state_key``) is where the daemon's port, PID, and SQLite
  DB live, and is what the listen port is derived from. Keying it to the
  shared ``SSTATE_DIR`` (see :attr:`BuildConfig.hashserv_state_key`) lets every
  workspace that shares one sstate cache share one daemon and one equivalence
  DB - the cache and its hash index stay paired.
- The **binary root** (``binary_root``) is where the ``bitbake-hashserv``
  executable is found. The daemon binary is sourced exclusively from the synced
  workspace (``<binary_root>/sources/poky/bitbake/bin/bitbake-hashserv``); we
  deliberately do NOT fall back to a host PATH lookup, because the daemon's wire
  protocol must match the bitbake the build will run against.
"""

from __future__ import annotations

import os
import shutil
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


def _workspace_port(state_key: Path) -> int:
    """Derive a stable ephemeral port from the state-key path.

    Two daemons on the same machine must not collide; a random pick would need
    a port-file lookup to be authoritative. Hashing ``realpath(state_key)`` into
    the 49152-65534 range gives a stable URL for the lifetime of the daemon
    without making any state file load-bearing for routing. Two callers sharing
    one state key therefore land on the same port - the shared-daemon contract.
    """
    digest = sha256(str(state_key.resolve()).encode()).hexdigest()
    return _PORT_FLOOR + int(digest[:8], 16) % _PORT_SPAN


def _find_binary(binary_root: Path) -> Path | None:
    """Return the workspace ``bitbake-hashserv`` path, or None if absent.

    Only workspace paths are consulted - no host PATH fallback. A PATH binary
    may be from a different bitbake version whose hashserv wire protocol does
    not match the workspace bitbake, which silently corrupts the equivalence
    cache.

    Search order:
    1. ``<binary_root>/sources/poky/bitbake/``  - NXP (poky umbrella)
    2. ``<binary_root>/sources/bitbake/``       - TI (bare oe-core)
    3. ``<binary_root>/bitbake/``               - generic workspace-root bitbake
    4. ``<binary_root.parent>/bitbake/``        - meta-avocado style (binary_root
                                                  is ``<workspace>/build-<stem>``;
                                                  the workspace ships bitbake as
                                                  a sibling)
    """
    for candidate in (
        binary_root / "sources" / "poky" / "bitbake" / "bin" / "bitbake-hashserv",
        binary_root / "sources" / "bitbake" / "bin" / "bitbake-hashserv",
        binary_root / "bitbake" / "bin" / "bitbake-hashserv",
        binary_root.parent / "bitbake" / "bin" / "bitbake-hashserv",
    ):
        if candidate.is_file():
            return candidate
    return None


def _state_dir(state_key: Path) -> Path:
    """Return ``<state_key>/.bakar`` (the daemon state directory)."""
    return state_key / _STATE_SUBDIR


def _read_pid(state_key: Path) -> int | None:
    """Return the PID recorded for the daemon, or None on any failure.

    Treats every error path - missing file, unreadable file, unparseable
    content - as "no recorded PID". Callers cannot distinguish the
    failures and do not need to: each one means "we have no live
    reference to a daemon".
    """
    pid_file = _state_dir(state_key) / _PID_FILENAME
    try:
        raw = pid_file.read_text()
    except OSError:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def binary_available(binary_root: Path) -> bool:
    """True when the workspace's bitbake-hashserv binary exists under binary_root.

    When present, ``ensure_running`` can spawn the persistent daemon (the build
    does this automatically), so a not-yet-running daemon is benign. When absent,
    the build silently falls back to bitbake's per-build ``auto`` server and loses
    the persistent cross-build hash-equivalence DB.
    """
    return _find_binary(binary_root) is not None


def is_running(state_key: Path) -> bool:
    """Return True iff the recorded PID is alive AND its cmdline names the daemon.

    The cmdline check guards against PID recycling: a host reboot or a
    long-lived state key can leave a stale PID file pointing at a now-
    unrelated process. ``/proc/<pid>/cmdline`` is NUL-separated, so we
    read bytes and look for the binary name as a substring rather than
    splitting on NUL.
    """
    pid = _read_pid(state_key)
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


def ensure_running(state_key: Path, *, binary_root: Path, bind_host: str = "localhost") -> str | None:
    """Ensure a hashserv daemon for ``state_key`` is running; return its URL.

    The daemon's port, PID, and DB are keyed to ``state_key`` (so callers that
    share a state key share one daemon), while the ``bitbake-hashserv`` binary
    is sourced from ``binary_root`` (the synced workspace).

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
    state_dir = _state_dir(state_key)
    port_file = state_dir / _PORT_FILENAME
    pid_file = state_dir / _PID_FILENAME

    if is_running(state_key):
        try:
            port = int(port_file.read_text().strip())
        except FileNotFoundError, ValueError:
            # Port file missing or corrupt (PID/port written non-atomically;
            # crash between the two writes leaves an orphan PID file). Fall
            # through to re-spawn below by treating the daemon as stopped.
            pid_file.unlink(missing_ok=True)
            port_file.unlink(missing_ok=True)
        else:
            return f"ws://{bind_host}:{port}"

    binary = _find_binary(binary_root)
    if binary is None:
        return None

    port = _workspace_port(state_key)
    # 0.0.0.0 (and empty) are bind-only addresses; probe loopback for them.
    probe_host = "127.0.0.1" if bind_host in ("0.0.0.0", "") else bind_host
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
            f"ws://{bind_host}:{port}",
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
            sock = socket.create_connection((probe_host, port), timeout=0.5)
        except OSError:
            if time.monotonic() > deadline:
                _abort_startup(proc, state_dir)
                return None
            time.sleep(0.1)
            continue
        sock.close()
        pid_file.write_text(f"{proc.pid}\n")
        port_file.write_text(f"{port}\n")
        return f"ws://{bind_host}:{port}"


def stop(state_key: Path) -> bool:
    """Stop the daemon for ``state_key`` and clean PID/port state files.

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
    pid = _read_pid(state_key)
    if pid is None:
        return False

    state_dir = _state_dir(state_key)
    pid_file = state_dir / _PID_FILENAME
    port_file = state_dir / _PORT_FILENAME

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while is_running(state_key) and time.monotonic() < deadline:
        time.sleep(0.1)

    if is_running(state_key):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + 1.0
        while is_running(state_key) and time.monotonic() < kill_deadline:
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


# --- Central cross-node tier (Rust/PostgreSQL hashserv) ----------------------
#
# The avocado-linux Rust hashserv is an asyncrpc-compatible reimplementation
# backed by PostgreSQL (hashserv/docs/integration.md). It is the shared,
# concurrent-writer-safe hash-equivalence service for the build cluster: unlike
# the per-workspace bitbake-hashserv daemon above (Python + single-writer
# SQLite), one instance serves every node. These helpers manage that service by
# TCP probe for liveness rather than a tracked PID - the postgres DB is the
# durable state, so a restart loses nothing and any node may (re)start it.

CENTRAL_DEFAULT_PORT = 8686  # hashserv/docs/integration.md default bind port


def _probe_addr(bind_host: str) -> str:
    """Loopback for bind-only addresses (0.0.0.0/empty), else the host itself."""
    return "127.0.0.1" if bind_host in ("0.0.0.0", "") else bind_host


def central_bb_hashserve(host: str, port: int = CENTRAL_DEFAULT_PORT) -> str:
    """The ``BB_HASHSERVE`` value (``host:port``) for the central Rust hashserv."""
    return f"{host}:{port}"


def central_listening(host: str, port: int = CENTRAL_DEFAULT_PORT, *, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to the central hashserv endpoint succeeds."""
    try:
        sock = socket.create_connection((_probe_addr(host), port), timeout=timeout)
    except OSError:
        return False
    sock.close()
    return True


def central_service_argv(binary: str, *, bind: str, database: str) -> list[str]:
    """argv to start the avocado-hashserv Rust service against ``database``."""
    return [binary, "server", "--bind", bind, "--database", database]


def central_ensure_running(
    *,
    binary: str,
    bind_host: str,
    database: str,
    port: int = CENTRAL_DEFAULT_PORT,
    startup_deadline_seconds: float = 5.0,
) -> str | None:
    """Ensure the central Rust hashserv is listening; return ``host:port``.

    Returns the endpoint when the service is already listening or a fresh spawn
    passes the TCP startup probe. Returns ``None`` when ``binary`` resolves to no
    executable, or when a fresh spawn never reaches the probe within
    ``startup_deadline_seconds``. Liveness is the TCP probe; no PID is tracked
    because the postgres DB - not an on-disk file - is the durable state.
    """
    if central_listening(bind_host, port):
        return central_bb_hashserve(bind_host, port)
    if shutil.which(binary) is None and not Path(binary).is_file():
        return None
    probe = _probe_addr(bind_host)
    proc = subprocess.Popen(
        central_service_argv(binary, bind=f"{bind_host}:{port}", database=database),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + startup_deadline_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        try:
            sock = socket.create_connection((probe, port), timeout=0.5)
        except OSError:
            time.sleep(0.1)
            continue
        sock.close()
        return central_bb_hashserve(bind_host, port)
    # Never reached the probe within the deadline: terminate the spawn so a
    # service that started but never listened is not left orphaned.
    if proc.poll() is None:
        proc.terminate()
    return None
