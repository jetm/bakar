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

# sccache's default server port; overridable via SCCACHE_SERVER_PORT.
_DEFAULT_PORT = 4226
_STARTUP_DEADLINE_SECONDS = 10.0


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


def ensure_running(scheduler_url: str | None = None, *, binary: str | None = None) -> bool:
    """Ensure a persistent, detached sccache server is running. Return True if up.

    Idempotent: returns True without spawning when a server already answers on
    the sccache port. Otherwise spawns ``sccache --start-server`` detached
    (``start_new_session=True``) with ``SCCACHE_IDLE_TIMEOUT=0`` so it survives
    bitbake's per-task process-group teardown, then probes until it answers.

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

    port = _server_port()
    if _server_responding(port):
        return True

    env = {**os.environ, "SCCACHE_IDLE_TIMEOUT": "0"}
    if scheduler_url:
        env["SCCACHE_DIST_SCHEDULER_URL"] = scheduler_url

    subprocess.Popen(
        [sccache, "--start-server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )

    deadline = time.monotonic() + _STARTUP_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if _server_responding(port):
            return True
        time.sleep(0.1)
    return False
