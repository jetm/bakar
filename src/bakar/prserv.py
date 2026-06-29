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

import shutil
import socket
import subprocess
import time
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

_DB_FILENAME = "prserv.sqlite3"
_LOG_FILENAME = "prserv.log"
_STDERR_FILENAME = "prserv.stderr"
_STATE_SUBDIR = ".bakar"
_PORT_FLOOR = 49152
_PORT_SPAN = 16383
_STARTUP_PROBE_DEADLINE_SECONDS = 5.0


def _workspace_port(state_key: Path) -> int:
    """Derive a stable port from the state-key path, salted to avoid hashserv.

    Mirrors :func:`bakar.hashserv._workspace_port` but prepends a ``prserv:``
    salt so the prserv and hashserv daemons keyed to the *same* state dir
    (the shared SSTATE_DIR) never collide on one port.
    """
    digest = sha256(b"prserv:" + str(state_key.resolve()).encode()).hexdigest()
    return _PORT_FLOOR + int(digest[:8], 16) % _PORT_SPAN


def _find_binary(binary_root: Path) -> Path | None:
    """Return the workspace ``bitbake-prserv`` path, or None if absent.

    Same search order and no-host-PATH-fallback rationale as
    :func:`bakar.hashserv._find_binary`.
    """
    for candidate in (
        binary_root / "sources" / "poky" / "bitbake" / "bin" / "bitbake-prserv",
        binary_root / "sources" / "bitbake" / "bin" / "bitbake-prserv",
        binary_root / "bitbake" / "bin" / "bitbake-prserv",
        binary_root.parent / "bitbake" / "bin" / "bitbake-prserv",
    ):
        if candidate.is_file():
            return candidate
    return None


def _state_dir(state_key: Path) -> Path:
    """Return ``<state_key>/.bakar`` (the daemon state directory)."""
    return state_key / _STATE_SUBDIR


def _probe_host(bind_host: str) -> str:
    """Return the address to TCP-probe for ``bind_host``.

    0.0.0.0 (and empty) are bind-only addresses, so probe loopback for them;
    a specific host (localhost or a cluster IP) is probed directly.
    """
    return "127.0.0.1" if bind_host in ("0.0.0.0", "") else bind_host


def _probe(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to ``host:port`` succeeds."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    sock.close()
    return True


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


def binary_available(binary_root: Path) -> bool:
    """True when the workspace's bitbake-prserv binary exists under binary_root."""
    return _find_binary(binary_root) is not None


def is_running(state_key: Path, *, bind_host: str = "localhost") -> bool:
    """Return True iff the daemon for ``state_key`` is listening on its port."""
    return _probe(_probe_host(bind_host), _workspace_port(state_key))


def ensure_running(state_key: Path, *, binary_root: Path, bind_host: str = "localhost") -> str | None:
    """Ensure a prserv daemon for ``state_key`` is running; return ``host:port``.

    The daemon's port and DB are keyed to ``state_key`` (so callers sharing a
    state key share one daemon and one PR DB), while the ``bitbake-prserv``
    binary is sourced from ``binary_root``. The daemon binds ``bind_host`` so a
    cluster-reachable IP makes the PR service usable from other nodes.

    Returns ``f"{bind_host}:{port}"`` when the daemon is reachable (already
    running, or freshly spawned and passed the TCP startup probe) - the exact
    string to assign to ``PRSERV_HOST``. Returns ``None`` silently when the
    workspace bitbake-prserv binary has not been synced yet, or when a fresh
    spawn never reached the probe within ``_STARTUP_PROBE_DEADLINE_SECONDS`` (in
    which case the daemon's launcher output is captured to
    ``<state_dir>/prserv.stderr`` and any stale pidfile is cleared).
    """
    port = _workspace_port(state_key)
    probe = _probe_host(bind_host)
    if _probe(probe, port):
        return f"{bind_host}:{port}"

    binary = _find_binary(binary_root)
    if binary is None:
        return None

    state_dir = _state_dir(state_key)
    state_dir.mkdir(parents=True, exist_ok=True)
    _clean_stale_pidfiles(port)

    # --start double-forks into a daemon and the launcher exits, so we do not
    # track the returned process - liveness is the TCP probe below. Redirect the
    # launcher's stderr to a file so a startup failure leaves a diagnostic.
    stderr_log = state_dir / _STDERR_FILENAME
    stderr_fh = stderr_log.open("wb")
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
        stderr=stderr_fh,
        start_new_session=True,
    )
    stderr_fh.close()

    deadline = time.monotonic() + _STARTUP_PROBE_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if _probe(probe, port):
            return f"{bind_host}:{port}"
        time.sleep(0.1)

    _clean_stale_pidfiles(port)
    return None


def stop(state_key: Path, *, binary_root: Path, bind_host: str = "localhost") -> bool:
    """Stop the daemon for ``state_key`` via ``bitbake-prserv --stop``.

    Returns False (no-op) when the binary is missing or the daemon is not
    listening - nothing to stop. Uses prserv's own ``--stop`` so the SQLite DB
    is flushed cleanly (a SIGKILL could corrupt the PR DB). The DB is preserved
    across stop/start cycles; the whole point of the persistent daemon is to
    keep the accumulated PR history. Any stale pidfile is cleared afterwards.
    """
    port = _workspace_port(state_key)
    if not _probe(_probe_host(bind_host), port):
        _clean_stale_pidfiles(port)
        return False

    binary = _find_binary(binary_root)
    if binary is None:
        return False

    subprocess.run(
        [str(binary), "--stop", "--host", bind_host, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
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
    return f"{host}:{port}"


def central_listening(host: str, port: int = CENTRAL_DEFAULT_PORT, *, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to the central prserv endpoint succeeds."""
    return _probe(_probe_host(host), port, timeout=timeout)


def central_service_argv(binary: str, *, bind: str, database: str) -> list[str]:
    """argv to start the avocado-prserv Rust service against ``database``."""
    return [binary, "server", "--bind", bind, "--database", database]


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
    probe = _probe_host(bind_host)
    if _probe(probe, port):
        return central_prserv_host(bind_host, port)
    if shutil.which(binary) is None and not Path(binary).is_file():
        return None
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
        if _probe(probe, port):
            return central_prserv_host(bind_host, port)
        time.sleep(0.1)
    return None
