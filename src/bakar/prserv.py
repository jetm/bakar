"""Workspace-scoped bitbake-prserv daemon helpers.

This module owns the lifecycle of a ``bitbake-prserv`` (PR service) daemon keyed
to a *state directory*, mirroring :mod:`bakar.hashserv`. A persistent, shared PR
service keeps package revisions (PR) monotonic across builds: bitbake's own
autostart (``PRSERV_HOST = "localhost:0"``) keeps its SQLite DB under the
volatile ``${PERSISTENT_DIR}`` (``TMPDIR/cache``), so wiping a build tree resets
PRs to r0 while buildhistory (kept at ``TOPDIR``) still records the old r0.N -
which fails the ``version-going-backwards`` QA on ``do_packagedata_setscene``
and forces cache rebuilds. Co-locating one persistent daemon's DB with the
shared sstate fixes that.

Unlike the autostart server, this daemon binds a *configurable* address so other
cluster nodes can reach it. ``is_local_special`` (prserv/serv.py) only autostarts
``localhost:0``; binding a real IP therefore requires a managed daemon plus
``PRSERV_HOST = "<host>:<port>"`` so bitbake connects instead of autostarting.

Two locations are kept deliberately separate (see :mod:`bakar.hashserv`):

- The **state key** (``state_key``) keys the daemon's port and SQLite DB and is
  what the listen port is derived from. Keying it to the shared ``SSTATE_DIR``
  (``BuildConfig.prserv_state_key``) lets every workspace that shares one sstate
  cache share one daemon and one PR DB - the cache and its revision index stay
  paired.
- The **binary root** (``binary_root``) is where the ``bitbake-prserv``
  executable is found, sourced exclusively from the synced workspace so its wire
  protocol matches the bitbake the build runs against.

``bitbake-prserv --start`` double-forks into a daemon (writing its own pidfile
under ``/tmp/PRServer_<ip>_<port>.pid``) and the launcher exits, so this module
tracks liveness by TCP-probing the listen port rather than by a Popen PID.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from bakar import central_service, daemon_log
from bakar.hashserv import NetworkStateDirError, network_state_reason

_DB_FILENAME = "prserv.sqlite3"
_LOG_FILENAME = "prserv.log"
_STDERR_FILENAME = "prserv.stderr"
_STATE_SUBDIR = ".bakar"
_STARTUP_PROBE_DEADLINE_SECONDS = 5.0
_STOP_DEADLINE_SECONDS = 5.0
# Salt keeps prserv's port distinct from hashserv's for the same shared state key.
_PORT_SALT = b"prserv:"
_BINARY_NAME = "bitbake-prserv"


def _workspace_port(state_key: Path) -> int:
    """Port for ``state_key``, salted so it never collides with hashserv's."""
    return central_service.workspace_port(state_key, _PORT_SALT)


def _find_binary(binary_root: Path) -> Path | None:
    """Return the workspace ``bitbake-prserv`` path, or None if absent."""
    return central_service.find_workspace_tool(binary_root, _BINARY_NAME)


def _state_dir(state_key: Path) -> Path:
    """Return ``<state_key>/.bakar`` (the daemon state directory)."""
    return state_key / _STATE_SUBDIR


def _clean_stale_pidfiles(port: int) -> None:
    """Remove any ``/tmp/PRServer_*_<port>.pid`` left by a crashed daemon.

    ``bitbake-prserv --start`` refuses to start when its pidfile already exists
    (prserv/serv.py: "Daemon already running?"). A daemon that died without
    cleaning up leaves the pidfile behind, which would then block every future
    start. The port is unique per state key, so globbing on the port suffix
    cannot match an unrelated daemon. Only called after a probe has shown the
    port is not actually listening.
    """
    for stale in Path("/tmp").glob(f"PRServer_*_{port}.pid"):
        with suppress(OSError):
            stale.unlink()


def _argv_option(argv: tuple[str, ...], name: str) -> str | None:
    """Return the value of ``name`` in ``argv`` (``--x v`` or ``--x=v``), or None."""
    for index, arg in enumerate(argv):
        if arg == name:
            return argv[index + 1] if index + 1 < len(argv) else None
        if arg.startswith(f"{name}="):
            return arg.partition("=")[2]
    return None


def _state_daemons(state_key: Path) -> list[central_service.OwnedDaemon]:
    """Return every live ``bitbake-prserv`` serving this state key's database.

    Whatever host or port each is bound to: all of them hold the one SQLite file,
    so every spawn, pidfile removal and stop decision is made against this set.
    """
    return central_service.find_owned_daemons(_BINARY_NAME, _state_dir(state_key) / _DB_FILENAME)


def _serving(
    daemons: list[central_service.OwnedDaemon], port: int, bind_host: str
) -> list[central_service.OwnedDaemon]:
    """Return the ``daemons`` whose argv binds ``bind_host`` and ``port``.

    Only these own the listener on ``<bind_host>:<port>``; a missing or
    unparsable ``--host``/``--port`` in the argv counts as not serving it.
    """
    return [
        daemon
        for daemon in daemons
        if _argv_option(daemon.argv, "--port") == str(port) and _argv_option(daemon.argv, "--host") == bind_host
    ]


def _own_pidfile_matches(bind_host: str, port: int, daemons: list[central_service.OwnedDaemon]) -> bool:
    """True when bitbake's pidfile for ``bind_host:port`` is ours and names one of ``daemons``.

    ``/tmp`` is world-writable, so another user could plant a pidfile naming an
    arbitrary pid; bitbake's ``--stop`` would then signal it. ``--stop`` reads
    exactly ``/tmp/PRServer_<ip>_<port>.pid`` with ``<ip>`` the resolved host, so
    only that path is checked: it must be owned by the current uid and its first
    line must be a found daemon's pid.
    """
    try:
        ip = socket.gethostbyname(bind_host)
    except OSError:
        return False
    pidfile = Path("/tmp") / f"PRServer_{ip}_{port}.pid"
    try:
        if pidfile.stat().st_uid != os.getuid():
            return False
        return int(pidfile.read_text().splitlines()[0].strip()) in {daemon.pid for daemon in daemons}
    except OSError, IndexError, ValueError:
        return False


def is_running(state_key: Path, *, bind_host: str = "localhost") -> bool:
    """Return True iff this state key's own daemon is listening on its port.

    Both conditions must hold: the derived port answers AND a live
    ``bitbake-prserv`` serving this state key's database exists. A listener on
    the derived port that no such process owns is a foreign holder, not this
    state key's daemon, so it reports False.
    """
    if not central_service.is_listening(bind_host, _workspace_port(state_key)):
        return False
    return bool(_serving(_state_daemons(state_key), _workspace_port(state_key), bind_host))


def ensure_running(state_key: Path, *, binary_root: Path, bind_host: str = "localhost") -> str | None:
    """Ensure a prserv daemon for ``state_key`` is running; return ``host:port``.

    The daemon's port and DB are keyed to ``state_key`` (so callers sharing a
    state key share one daemon and one PR DB), while the ``bitbake-prserv``
    binary is sourced from ``binary_root``. The daemon binds ``bind_host`` so a
    cluster-reachable IP makes the PR service usable from other nodes.

    An address is returned only for this state key's own daemon serving
    ``bind_host`` and the derived port, found in the process table by its
    database path. The derived port alone proves nothing:

    - listening and owned: return ``f"{bind_host}:{port}"``.
    - listening and no daemon of the state key exists: spawn nothing, record the
      refusal in ``<state_dir>/prserv.stderr`` and return ``None``.
    - a daemon of the state key serves another host or port: spawn nothing,
      remove no pidfile, record a refusal naming its pid and return ``None``.
    - owned but not listening: wait up to ``_STARTUP_PROBE_DEADLINE_SECONDS``
      for it to answer; on failure record the reason and return ``None``.
      bitbake's pidfile is never removed here, since the daemon is alive.
    - no daemon of the state key: spawn ``bitbake-prserv --start`` and probe as
      below.

    Returns ``None`` silently when the workspace bitbake-prserv binary has not
    been synced yet, or when a fresh spawn never reached the probe within
    ``_STARTUP_PROBE_DEADLINE_SECONDS`` (in which case the daemon's launcher
    output is captured to ``<state_dir>/prserv.stderr`` and any stale pidfile
    is cleared).

    Raises :class:`bakar.hashserv.NetworkStateDirError` before creating any
    state dir, PID file, or process when ``state_key``'s state directory is on
    NFS or an undetermined filesystem - see
    :func:`bakar.hashserv.network_state_reason`.
    """
    reason = network_state_reason(state_key, service="prserv", setting="prserv_host")
    if reason is not None:
        raise NetworkStateDirError(reason)

    port = _workspace_port(state_key)
    state_dir = _state_dir(state_key)
    address = f"{bind_host}:{port}"
    # Probe the port before scanning: a daemon exists before it listens, so a
    # listener seen first always has its process in the scan that follows.
    listening = central_service.is_listening(bind_host, port)
    daemons = _state_daemons(state_key)

    if listening and _serving(daemons, port, bind_host):
        return address
    if listening and not daemons:
        daemon_log.record_refusal(
            state_dir / _STDERR_FILENAME,
            state_dir,
            f"{address} is held by a process that is not this state key's bitbake-prserv; "
            "bitbake-prserv was not started",
        )
        return None
    if daemons:
        return _adopt_existing(daemons, port, bind_host, state_dir)

    binary = _find_binary(binary_root)
    if binary is None:
        return None

    state_dir.mkdir(parents=True, exist_ok=True)
    conflict = central_service.bind_conflict(bind_host, port)
    if conflict is not None:
        daemon_log.record_refusal(
            state_dir / _STDERR_FILENAME,
            state_dir,
            f"{conflict}; bitbake-prserv was not started",
        )
        return None
    # A daemon may have started since the scan above; its pidfile is live.
    raced = _state_daemons(state_key)
    if raced:
        return _adopt_existing(raced, port, bind_host, state_dir)
    _clean_stale_pidfiles(port)

    # --start double-forks into a daemon and the launcher exits, so we do not
    # track the returned process - liveness is the TCP probe below. Redirect the
    # launcher's stderr to a file so a startup failure leaves a diagnostic.
    with daemon_log.stderr_target(str(binary), state_dir, log_path=state_dir / _STDERR_FILENAME) as stderr_fh:
        subprocess.Popen(
            [
                str(binary),
                "--start",
                "--host",
                bind_host,
                "--port",
                str(port),
                "-f",
                str(state_dir / _DB_FILENAME),
                "-l",
                str(state_dir / _LOG_FILENAME),
            ],
            stdout=subprocess.DEVNULL,
            stderr=stderr_fh if stderr_fh is not None else subprocess.DEVNULL,
            start_new_session=True,
        )

    deadline = time.monotonic() + _STARTUP_PROBE_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if central_service.is_listening(bind_host, port):
            return f"{bind_host}:{port}"
        time.sleep(0.1)

    # A daemon that is still starting owns its pidfile; only clear it when no
    # process of this state key exists.
    if not _state_daemons(state_key):
        _clean_stale_pidfiles(port)
    return None


def _adopt_existing(
    daemons: list[central_service.OwnedDaemon], port: int, bind_host: str, state_dir: Path
) -> str | None:
    """Handle a state key whose daemon already exists: adopt it or refuse, never spawn.

    A daemon serving this host and port is waited on. One serving another host
    or port holds the SQLite database, so a second daemon would corrupt it:
    refuse with its process id and the stop hint, leaving its pidfile alone.
    """
    address = f"{bind_host}:{port}"
    owned = _serving(daemons, port, bind_host)
    if owned:
        return _await_owned(owned, address, state_dir)
    other = daemons[0]
    where = f"{_argv_option(other.argv, '--host') or 'unknown'}:{_argv_option(other.argv, '--port') or 'unknown'}"
    daemon_log.record_refusal(
        state_dir / _STDERR_FILENAME,
        state_dir,
        f"bitbake-prserv pid {other.pid} already serves {state_dir / _DB_FILENAME} on {where}, not {address}; "
        "run 'bakar prserv stop'",
    )
    return None


def _await_owned(owned: list[central_service.OwnedDaemon], address: str, state_dir: Path) -> str | None:
    """Wait for an already-running owned daemon to answer; record a refusal if it never does."""
    host, _, port = address.rpartition(":")
    if central_service.wait_listening(host, int(port), _STARTUP_PROBE_DEADLINE_SECONDS):
        return address
    daemon_log.record_refusal(
        state_dir / _STDERR_FILENAME,
        state_dir,
        f"bitbake-prserv pid {owned[0].pid} already serves {state_dir / _DB_FILENAME} but does not answer on "
        f"{address}; run 'bakar prserv stop'",
    )
    return None


def _wait_stopped(bind_host: str, port: int, database: Path) -> bool:
    """Poll until the port stops answering and no daemon serves ``database``.

    Returns True when both hold, False when ``_STOP_DEADLINE_SECONDS`` passes
    first. Probes at least once.
    """
    end = time.monotonic() + _STOP_DEADLINE_SECONDS
    while True:
        if not central_service.is_listening(bind_host, port) and not central_service.find_owned_daemons(
            _BINARY_NAME, database
        ):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)


def _terminate(daemons: list[central_service.OwnedDaemon]) -> None:
    """Send SIGTERM to each daemon; a process already gone or not ours is skipped."""
    for daemon in daemons:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(daemon.pid, signal.SIGTERM)


def stop(state_key: Path, *, binary_root: Path, bind_host: str = "localhost") -> bool:
    """Stop the daemon(s) serving ``state_key``'s database; True when one was found.

    The daemon is found in the process table by its database path, not by its
    host, port or pidfile, so a daemon bound elsewhere or whose bitbake pidfile
    is gone is still stopped.
    With no daemon found this returns False: stale pidfiles are cleared only when
    the derived port is not listening, and a foreign listener is neither stopped
    nor reported as stopped.

    When the binary is found and a pidfile for the port is owned by this uid and
    names a found daemon, bitbake's own ``--stop`` runs first and no signal is sent until it has been waited on,
    so the SQLite DB is flushed cleanly. Otherwise each found process gets
    SIGTERM. A process still found after the ``--stop`` wait (a second daemon on
    one database) is sent SIGTERM and waited on once more. SIGKILL is never sent:
    the PR database must not see a kill. A process still found at the final
    deadline leaves its pidfile in place and the call still returns True.
    """
    port = _workspace_port(state_key)
    database = _state_dir(state_key) / _DB_FILENAME
    found = _state_daemons(state_key)
    if not found:
        if not central_service.is_listening(bind_host, port):
            _clean_stale_pidfiles(port)
        return False

    binary = _find_binary(binary_root)
    if binary is not None and _own_pidfile_matches(bind_host, port, found):
        subprocess.run(
            [str(binary), "--stop", "--host", bind_host, "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        _wait_stopped(bind_host, port, database)
        found = _state_daemons(state_key)
        if found:
            _terminate(found)
            _wait_stopped(bind_host, port, database)
    else:
        _terminate(found)
        _wait_stopped(bind_host, port, database)

    if not _state_daemons(state_key):
        _clean_stale_pidfiles(port)
    return True


# --- Central cross-node tier (Rust/PostgreSQL prserv) ------------------------
#
# The avocado-linux Rust prserv is a PostgreSQL-backed reimplementation of
# bitbake's PR service (prserv/docs/integration.md). Postgres-backed PR
# allocation is the shared, monotonic source of package revisions for the build
# cluster: unlike the per-workspace bitbake-prserv daemon above (Python +
# single-writer SQLite), one instance serves every node and survives a
# TMPDIR wipe, so PRs never go backwards. Liveness is the TCP probe (mirroring
# the bitbake daemon's model); the postgres DB is the durable state.

CENTRAL_DEFAULT_PORT = 8585  # prserv/docs/integration.md default bind port


def central_prserv_host(host: str, port: int = CENTRAL_DEFAULT_PORT) -> str:
    """The ``PRSERV_HOST`` value (``host:port``) for the central Rust prserv."""
    return central_service.endpoint(host, port)


def central_listening(host: str, port: int = CENTRAL_DEFAULT_PORT, *, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to the central prserv endpoint succeeds."""
    return central_service.is_listening(host, port, timeout=timeout)


def central_service_argv(binary: str, *, bind: str, database: str) -> list[str]:
    """argv to start the avocado-prserv Rust service against ``database``."""
    return central_service.service_argv(binary, bind=bind, database=database)


def central_ensure_running(
    *,
    binary: str,
    bind_host: str,
    database: str,
    port: int = CENTRAL_DEFAULT_PORT,
    startup_deadline_seconds: float = 5.0,
) -> str | None:
    """Ensure the central Rust prserv is listening; return ``host:port``.

    Returns the endpoint when the service is already listening or a fresh spawn
    passes the TCP startup probe. Returns ``None`` when ``binary`` resolves to no
    executable, or when a fresh spawn never reaches the probe within
    ``startup_deadline_seconds``. Liveness is the TCP probe; no PID is tracked
    because the postgres DB - not an on-disk file - is the durable state.
    """
    return central_service.ensure_running(
        binary=binary,
        bind_host=bind_host,
        database=database,
        port=port,
        startup_deadline_seconds=startup_deadline_seconds,
    )
