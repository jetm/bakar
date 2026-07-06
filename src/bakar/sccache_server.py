"""Persistent sccache client-server lifecycle.

bitbake runs each task in its own process group, and the first task to invoke
``sccache`` auto-starts the sccache server as a child of that task. When the
task finishes, bitbake tears down the task's process group and takes the server
with it; the next task then sees "server looks like it shut down unexpectedly,
compiling locally instead" and falls back to local. A compile in flight when
the server dies leaves a truncated object that gets cached and served as a
poisoned hit on later runs (manifesting as ``recompile with -fPIC`` link
failures).

Starting one persistent server, detached from any build process tree, before
the build fixes this: every task connects to the same long-lived server, and a
finished task can no longer kill it. Mirrors the workspace hashserv daemon
(``hashserv.ensure_running``).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

# sccache's default server port; overridable via SCCACHE_SERVER_PORT.
_DEFAULT_PORT = 4226
_STARTUP_DEADLINE_SECONDS = 10.0

# Central daemon stderr log lives under the user state dir (the sccache server is
# detached and long-lived; no per-workspace state key exists for it).
_STATE_DIR = Path.home() / ".local" / "state" / "bakar"


def _stderr_log_path(binary: str) -> Path:
    """State-dir stderr log path for the central ``binary`` daemon."""
    return _STATE_DIR / f"{Path(binary).name}-central.stderr"


def default_uds_path() -> Path:
    """Stable host-mode server socket path.

    A unix-domain socket instead of a TCP port because bitbake runs do_compile in
    a private network namespace: ``127.0.0.1:4226`` inside the task is a different
    loopback than the host daemon's, so a TCP client cannot see the pre-started
    server and auto-starts its own config-less local-only one - every compile runs
    locally and the cluster sits idle. A socket file crosses the namespace
    boundary. Absolute (do_compile's HOME is a kas throwaway temp dir) and kept
    outside the sccache disk-cache dir so a cache wipe never unlinks a live socket.
    """
    return Path.home() / ".cache" / "bakar" / "sccache-server.sock"


def _uds_responding(path: str) -> bool:
    """Return True when a server answers a connect on the unix socket path.

    A pure probe mirroring :func:`_server_responding`: it never starts a server.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.connect(path)
    except OSError:
        return False
    sock.close()
    return True


def _server_port() -> int:
    """Return the sccache server port, honoring SCCACHE_SERVER_PORT."""
    raw = os.environ.get("SCCACHE_SERVER_PORT", "").strip()
    if not raw:
        return _DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_PORT


def _server_responding(port: int) -> bool:
    """Return True when a server answers a TCP connect on the sccache port.

    A pure probe: it never starts a server, unlike ``sccache --show-stats``
    which would auto-spawn one.
    """
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=0.5)
    except OSError:
        return False
    sock.close()
    return True


def ensure_running(scheduler_url: str | None = None, *, binary: str | None = None, uds_path: str | None = None) -> bool:
    """Ensure a persistent, detached sccache server is running. Return True if up.

    Idempotent: returns True without spawning when a server already answers.
    Otherwise spawns ``sccache --start-server`` detached
    (``start_new_session=True``) with ``SCCACHE_IDLE_TIMEOUT=0`` so it survives
    bitbake's per-task process-group teardown, then probes until it answers.

    ``uds_path`` selects a unix-domain socket instead of the TCP port. Host-mode
    do_compile runs in a private network namespace, so a TCP ``127.0.0.1:4226``
    daemon is unreachable and each task auto-starts its own config-less local
    server (the cluster sits idle); a socket file crosses the namespace boundary,
    letting every recipe compile reach the pre-started dist daemon. When set, the
    server binds the socket (its parent dir is created) and the probe checks it.

    Returns False when the ``sccache`` binary is absent from PATH or the server
    never came up within the startup deadline - the caller treats that as
    "sccache unavailable" and the build proceeds without a pre-started server
    (sccache then falls back to its own auto-start behavior).

    ``scheduler_url`` is exported as ``SCCACHE_DIST_SCHEDULER_URL`` for the
    spawned server when given, so the configured scheduler reaches the server
    that does the dist coordination.
    """
    sccache = binary if binary is not None else shutil.which("sccache")
    if sccache is None:
        return False

    env = {**os.environ, "SCCACHE_IDLE_TIMEOUT": "0"}
    if scheduler_url:
        env["SCCACHE_DIST_SCHEDULER_URL"] = scheduler_url

    if uds_path is not None:
        if _uds_responding(uds_path):
            return True
        Path(uds_path).parent.mkdir(parents=True, exist_ok=True)
        env["SCCACHE_SERVER_UDS"] = uds_path

        def responding() -> bool:
            return _uds_responding(uds_path)
    else:
        port = _server_port()
        if _server_responding(port):
            return True

        def responding() -> bool:
            return _server_responding(port)

    # Redirect the detached server's stderr to a state-dir log file rather than
    # discarding it, so a server that starts but crashes or never answers leaves
    # its diagnostic output on disk. A file (not PIPE) avoids the server blocking
    # when the kernel pipe buffer fills. Matches the workspace hashserv/prserv logs.
    stderr_fh = None
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        stderr_fh = _stderr_log_path(sccache).open("wb")
    except OSError:
        # Best-effort diagnostic capture; a state-dir write failure must not
        # block spawning the server itself.
        pass
    subprocess.Popen(
        [sccache, "--start-server"],
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh if stderr_fh is not None else subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    if stderr_fh is not None:
        stderr_fh.close()

    deadline = time.monotonic() + _STARTUP_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if responding():
            return True
        time.sleep(0.1)
    return False
