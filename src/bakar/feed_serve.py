"""Serve the rendered feed, and report what it holds.

A static file server is the whole requirement: the render output is plain files
with repodata over them, and a client resolves everything by relative path from
``repomd.xml``. Nothing needs an application in front of it, which is also why
the local feed can be trusted to behave like production - the same bytes are
served the same way.

**Loopback by default.** ``python -m http.server`` binds every interface when
given no ``--bind``, which was measured publishing the entire feed root - every
package, every snapshot, and the state directory - to anything that could reach
the host, while the CLI printed ``http://localhost``. A feed is build output, not
a service, so the default is loopback and a wider bind has to be asked for.

The lifecycle is where the rest of the care goes. Two things must not be trusted:

- **A state file that outlives its process.** If a stale file read as "running",
  ``serve`` would refuse to start against a server that is not there, and the
  only recovery is deleting a file the user does not know exists. Liveness is
  therefore checked against the process, not against the file.
- **A spawn that returned.** ``Popen`` succeeding says the fork happened, not
  that the server bound. With output going to ``DEVNULL``, a port collision is
  invisible - the child writes "Address already in use" nowhere and exits. So a
  start is confirmed by connecting to the port, mirroring ``prserv``.

The bound port is recorded alongside the PID rather than re-derived, because
``status`` has no other way to know it: the port is a command-line argument, so a
server started on one port would otherwise be reported on the default.

Docker is deliberately not used, though production's local tier offers it. The
render output needs nothing more than a static server, and the container path's
only advantage - reaching the feed by hostname from inside a container network -
matters on macOS rather than here. The two-image confusion it brings is the top
troubleshooting item in the existing tier's own guide.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from bakar.feed import DEFAULT_CHANNEL, DEFAULT_RELEASE, channel_root
from bakar.feed_index import derive_targets
from bakar.feed_retention import pinned_snapshots, pool_entries

if TYPE_CHECKING:
    from pathlib import Path

_STATE_SUBDIR = ".bakar"
_STATEFILE = "serve.json"

DEFAULT_PORT = 8080

# Loopback, not 0.0.0.0. See the module docstring.
DEFAULT_BIND = "127.0.0.1"

_STARTUP_PROBE_DEADLINE_SECONDS = 5.0
_STARTUP_PROBE_INTERVAL_SECONDS = 0.1


def _state_dir(feed_root: Path) -> Path:
    """Return ``<feed_root>/.bakar``, mirroring the prserv state convention."""
    return feed_root / _STATE_SUBDIR


def _statefile(feed_root: Path) -> Path:
    return _state_dir(feed_root) / _STATEFILE


def serve_argv(feed_root: Path, *, port: int = DEFAULT_PORT, bind: str = DEFAULT_BIND) -> list[str]:
    """Return the argv for a static server rooted at the feed.

    ``--directory`` rather than a chdir so the server has no ambient dependence
    on the caller's working directory, and ``--bind`` always passed explicitly so
    the interface is never left to http.server's all-interfaces default.
    """
    return [
        sys.executable,
        "-m",
        "http.server",
        str(port),
        "--bind",
        bind,
        "--directory",
        str(feed_root),
    ]


def _probe_host(bind: str) -> str:
    """Return the address to TCP-probe for ``bind`` (mirrors prserv)."""
    return "127.0.0.1" if bind in ("0.0.0.0", "") else bind


def _probe(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to ``host:port`` succeeds."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    sock.close()
    return True


def _read_state(feed_root: Path) -> dict[str, object]:
    """Return the recorded server state, or an empty mapping."""
    try:
        state = json.loads(_statefile(feed_root).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _pid_of(feed_root: Path) -> int | None:
    """Return the recorded PID, or None when absent or unparseable."""
    pid = _read_state(feed_root).get("pid")
    return pid if isinstance(pid, int) else None


def recorded_port(feed_root: Path) -> int | None:
    """Return the port the running server was started on, if it is recorded."""
    port = _read_state(feed_root).get("port")
    return port if isinstance(port, int) else None


def _alive(pid: int) -> bool:
    """True when ``pid`` names a live process this user can signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Live, owned by somebody else. Still live, which is what is asked.
        return True
    except OSError:
        return False
    return True


def is_serving(feed_root: Path) -> bool:
    """True only when the recorded PID names a live process.

    A stale or unparseable state file reads as not running, deliberately - see
    the module docstring on why trusting the file is unrecoverable.
    """
    pid = _pid_of(feed_root)
    return pid is not None and _alive(pid)


def port_available(bind: str, port: int) -> bool:
    """True when ``port`` can be bound on ``bind`` right now.

    Asked BEFORE spawning, because a probe afterwards cannot tell "our child
    bound" from "something else already answers here" - and the second case
    would otherwise record a PID for a child that died on EADDRINUSE and report
    a server that is not ours. Racy by nature, which is why the post-spawn
    liveness check below stays.
    """
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind, port))
        except OSError:
            return False
    return True


def start_serving(feed_root: Path, *, port: int = DEFAULT_PORT, bind: str = DEFAULT_BIND) -> int | None:
    """Start the server in the background and return its PID, or None.

    None means the server did not come up - almost always the port being taken.
    Confirmed rather than assumed from ``Popen`` returning, and no state is
    recorded for a server that never bound, so a failed start cannot leave
    behind a file that makes the next ``serve`` refuse.
    """
    if not port_available(bind, port):
        return None

    state = _state_dir(feed_root)
    state.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        serve_argv(feed_root, port=port, bind=bind),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    probe = _probe_host(bind)
    deadline = time.monotonic() + _STARTUP_PROBE_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        # Death is checked FIRST: a child that already exited did not bind, no
        # matter what now answers on the port.
        if process.poll() is not None:
            return None
        if _probe(probe, port):
            _statefile(feed_root).write_text(
                json.dumps({"pid": process.pid, "port": port, "bind": bind}) + "\n",
                encoding="utf-8",
            )
            return process.pid
        time.sleep(_STARTUP_PROBE_INTERVAL_SECONDS)

    # Never bound inside the deadline. Kill the child rather than leaving an
    # untracked process behind holding whatever it did manage to open.
    with suppress(OSError):
        process.kill()
    return None


def stop_serving(feed_root: Path) -> bool:
    """Stop the server, returning whether one was actually running.

    Clears the state file either way, so a stale one never blocks the next
    start. Idempotent, so a caller need not check first.
    """
    pid = _pid_of(feed_root)
    running = pid is not None and _alive(pid)
    if running and pid is not None:
        with suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    with suppress(OSError):
        _statefile(feed_root).unlink()
    return running


def feed_status(
    feed_root: Path,
    *,
    release: str = DEFAULT_RELEASE,
    channel: str = DEFAULT_CHANNEL,
    port: int = DEFAULT_PORT,
) -> dict[str, object]:
    """Report what the feed holds, without starting anything.

    Answers on an empty or malformed feed rather than raising: status is what a
    user runs to find out what is wrong, so it has to survive the feed being
    wrong.

    ``port`` is only a fallback for the not-serving case. A running server's port
    comes from what it recorded, so this cannot report a URL the server is not
    actually on.
    """
    channel_dir = channel_root(feed_root, release=release, channel=channel)
    serving = is_serving(feed_root)
    live_port = recorded_port(feed_root) if serving else None
    shown_port = live_port if live_port is not None else port

    return {
        "feed_root": feed_root,
        "channel_root": channel_dir,
        "targets": list(derive_targets(channel_dir)),
        "pool_entries": len(pool_entries(channel_dir)),
        "snapshots": sorted(pinned_snapshots(channel_dir)),
        "serving": serving,
        "url": f"http://localhost:{shown_port}",
    }
