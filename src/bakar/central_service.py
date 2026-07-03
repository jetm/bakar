"""Shared spawn/probe logic for the central Rust hashserv and prserv services.

The central hashserv (``BB_HASHSERVE``) and prserv (``PRSERV_HOST``) tiers are
independent daemons, but bakar drives both the same way: a loopback-aware TCP
liveness probe, a ``<binary> server --bind <host:port> --database <db>`` argv,
and an ensure-running spawn that terminates a service which starts but never
listens (no PID is tracked - the postgres DB is the durable state). Only the
default port and the env-var name differ, so that shared machinery lives here;
:mod:`bakar.hashserv` and :mod:`bakar.prserv` keep their own default port and a
thin wrapper naming the env var.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path


def probe_addr(bind_host: str) -> str:
    """Loopback for bind-only addresses (0.0.0.0/empty), else the host itself."""
    return "127.0.0.1" if bind_host in ("0.0.0.0", "") else bind_host


def is_listening(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to ``probe_addr(host):port`` succeeds."""
    try:
        sock = socket.create_connection((probe_addr(host), port), timeout=timeout)
    except OSError:
        return False
    sock.close()
    return True


def endpoint(host: str, port: int) -> str:
    """The ``host:port`` endpoint string (BB_HASHSERVE / PRSERV_HOST value)."""
    return f"{host}:{port}"


def service_argv(binary: str, *, bind: str, database: str) -> list[str]:
    """argv to start an avocado hashserv/prserv Rust service against ``database``."""
    return [binary, "server", "--bind", bind, "--database", database]


def ensure_running(
    *,
    binary: str,
    bind_host: str,
    database: str,
    port: int,
    startup_deadline_seconds: float = 5.0,
) -> str | None:
    """Ensure the central service is listening; return its ``host:port`` endpoint.

    Returns the endpoint when the service is already listening or a fresh spawn
    passes the TCP startup probe. Returns ``None`` when ``binary`` resolves to no
    executable, or when a fresh spawn never reaches the probe within
    ``startup_deadline_seconds`` - in which case the spawn is terminated so a
    service that started but never listened is not left orphaned. Liveness is the
    TCP probe; no PID is tracked because the postgres DB is the durable state.
    """
    if is_listening(bind_host, port):
        return endpoint(bind_host, port)
    if shutil.which(binary) is None and not Path(binary).is_file():
        return None
    proc = subprocess.Popen(
        service_argv(binary, bind=f"{bind_host}:{port}", database=database),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + startup_deadline_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        if is_listening(bind_host, port):
            return endpoint(bind_host, port)
        time.sleep(0.1)
    if proc.poll() is None:
        proc.terminate()
    return None
