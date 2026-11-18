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

import errno
import os
import shutil
import socket
import subprocess
import time
from hashlib import sha256
from pathlib import Path

from bakar import daemon_log, mounts

_PORT_FLOOR = 49152
_PORT_SPAN = 16383


def workspace_port(state_key: Path, salt: bytes = b"") -> int:
    """Derive a stable ephemeral port (49152-65534) from the state-key path.

    ``salt`` is prepended to the hashed path so daemons keyed to the same state
    dir (hashserv: ``b""``, prserv: ``b"prserv:"``) land on different ports.
    Lexically normalizes rather than ``Path.resolve()``, which stats every
    component and can hang on a wedged NFS automount before the daemon-state
    guard has a chance to run.
    """
    digest = sha256(salt + str(mounts._lexical_normalize(state_key)).encode()).hexdigest()
    return _PORT_FLOOR + int(digest[:8], 16) % _PORT_SPAN


def find_workspace_tool(binary_root: Path, name: str) -> Path | None:
    """Return the workspace bitbake tool ``name``, or None if absent.

    Only workspace paths are consulted - no host PATH fallback, since a PATH
    binary may speak a different wire protocol than the workspace bitbake.
    Search order: ``sources/poky/bitbake`` (NXP), ``sources/bitbake`` (TI),
    ``bitbake`` under the root, then ``bitbake`` beside it (meta-avocado).
    """
    for candidate in (
        binary_root / "sources" / "poky" / "bitbake" / "bin" / name,
        binary_root / "sources" / "bitbake" / "bin" / name,
        binary_root / "bitbake" / "bin" / name,
        binary_root.parent / "bitbake" / "bin" / name,
    ):
        if candidate.is_file():
            return candidate
    return None


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


def bind_conflict(host: str, port: int) -> str | None:
    """Return why the daemon could not bind ``host:port``, or None when it could.

    Mirrors asyncio's ``create_server``, which the bitbake daemon's server uses:
    every address ``host`` resolves to is bound with SO_REUSEADDR (and
    IPV6_V6ONLY on IPv6), and an address whose family cannot be created or whose
    bind fails with EADDRNOTAVAIL is skipped. SO_REUSEADDR decides the outcome:
    it still refuses an established or recently closed client connection and a
    foreign listener, but accepts the closed connections a stopped daemon leaves
    on its own port, so a restart is not refused.
    """
    try:
        infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except socket.gaierror as exc:
        return f"{host}:{port} cannot be bound: {host!r} does not resolve ({exc})"
    bound = False
    for family, socktype, proto, _canonname, sockaddr in infos:
        try:
            sock = socket.socket(family, socktype, proto)
        except OSError:
            continue
        with sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind(sockaddr)
            except OSError as exc:
                if exc.errno == errno.EADDRNOTAVAIL:
                    continue
                addr = f"[{sockaddr[0]}]" if family == socket.AF_INET6 else sockaddr[0]
                reason = os.strerror(exc.errno).lower() if exc.errno else str(exc)
                return f"{addr}:{port} cannot be bound: {reason}"
            bound = True
    if not bound:
        return f"no address for {host} could be bound (port {port})"
    return None


def endpoint(host: str, port: int) -> str:
    """The ``host:port`` endpoint string (BB_HASHSERVE / PRSERV_HOST value)."""
    return f"{host}:{port}"


def service_argv(binary: str, *, bind: str, database: str) -> list[str]:
    """argv to start an avocado hashserv/prserv Rust service against ``database``."""
    return [binary, "server", "--bind", bind, "--database", database]


# Central daemon stderr logs live under the user state dir (no per-workspace
# state key exists for the central tier - the postgres DB is the durable state).
_STATE_DIR = daemon_log.STATE_DIR


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
    # Redirect daemon stderr to a state-dir log file rather than discarding it,
    # so a service that starts but crashes or never listens leaves its diagnostic
    # output on disk. A file (not PIPE) avoids the daemon blocking when the kernel
    # pipe buffer fills. Matches the per-workspace hashserv/prserv stderr logs.
    # The stderr_target contextmanager guarantees the fh is closed even if
    # Popen() itself raises (binary vanished, fork failure).
    with daemon_log.stderr_target(binary, _STATE_DIR) as stderr_fh:
        proc = subprocess.Popen(
            service_argv(binary, bind=f"{bind_host}:{port}", database=database),
            stdout=subprocess.DEVNULL,
            stderr=stderr_fh if stderr_fh is not None else subprocess.DEVNULL,
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
