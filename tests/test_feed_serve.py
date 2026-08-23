"""Tests for serving the feed and reporting its state.

The server is a static file server over the feed root and nothing more - the
render output needs no application in front of it. What needs care is the
lifecycle: a state file that outlives its process must not read as "running", or
``serve`` refuses to start against a server that is not there.

``start_serving`` and ``stop_serving`` are exercised against a REAL child
process, not a mock. Both were previously only mocked, which left the one
function that spawns something with no test at all - and a mocked start cannot
show that the spawn was confirmed, that the port was recorded, or that stop
actually signals.
"""

from __future__ import annotations

import json
import socket
import time
from typing import TYPE_CHECKING

import pytest

from bakar.feed_serve import (
    DEFAULT_BIND,
    feed_status,
    is_serving,
    recorded_port,
    serve_argv,
    start_serving,
    stop_serving,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _feed(tmp_path: Path, *, machines: tuple[str, ...] = (), pool: int = 0) -> Path:
    """A rendered feed with ``machines`` and ``pool`` pooled packages."""
    channel = tmp_path / "feed" / "dev" / "local"
    for machine in machines:
        (channel / "target" / machine / "repodata").mkdir(parents=True)
        (channel / "target" / machine / "repodata" / "repomd.xml").write_text("<repomd/>")
    for index in range(pool):
        entry = channel / "_pkgs" / f"{index:02d}" / f"{index:064d}.rpm"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b"x" * 16)
    return tmp_path / "feed"


def _free_port() -> int:
    """Return a port that was free a moment ago."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- argv ------------------------------------------------------------------


def test_serve_argv_serves_the_feed_root_on_the_requested_port(tmp_path) -> None:
    """Asserted by POSITION, not membership.

    `"8080" in argv` is satisfied by `--bind 8080`, which would serve every
    interface on the default port while the CLI printed the requested one.
    """
    argv = serve_argv(tmp_path / "feed", port=8080)

    assert argv[argv.index("http.server") + 1] == "8080"
    assert argv[argv.index("--directory") + 1] == str(tmp_path / "feed")


def test_serve_argv_binds_loopback_by_default(tmp_path) -> None:
    """http.server binds every interface when given no --bind.

    Measured: the whole feed root, including .bakar/, reachable from the LAN
    while the CLI printed http://localhost. A feed is build output, not a
    service, so a wider bind has to be asked for.
    """
    argv = serve_argv(tmp_path / "feed", port=8080)

    assert "--bind" in argv
    assert argv[argv.index("--bind") + 1] == "127.0.0.1"
    assert DEFAULT_BIND == "127.0.0.1"


def test_serve_argv_honours_an_explicit_wider_bind(tmp_path) -> None:
    argv = serve_argv(tmp_path / "feed", port=8080, bind="0.0.0.0")

    assert argv[argv.index("--bind") + 1] == "0.0.0.0"


# --- liveness --------------------------------------------------------------


def test_is_serving_is_false_with_no_statefile(tmp_path) -> None:
    """A feed that was never served is not serving."""
    assert is_serving(tmp_path / "feed") is False


def test_a_statefile_naming_a_dead_process_does_not_read_as_running(tmp_path) -> None:
    """A stale state file is not a running server.

    Trusting it would make ``serve`` refuse to start because something it
    believes is running is not, which is unrecoverable without deleting a file
    the user does not know about.
    """
    feed = _feed(tmp_path)
    state = feed / ".bakar"
    state.mkdir(parents=True)
    # PID 2^22 is above the default pid_max, so it cannot be live.
    (state / "serve.json").write_text(json.dumps({"pid": 4194304, "port": 8080}))

    assert is_serving(feed) is False


def test_a_statefile_with_garbage_does_not_read_as_running(tmp_path) -> None:
    """An unparseable state file is treated as not running, not as an error."""
    feed = _feed(tmp_path)
    state = feed / ".bakar"
    state.mkdir(parents=True)
    (state / "serve.json").write_text("not-json-at-all")

    assert is_serving(feed) is False


# --- the real lifecycle ----------------------------------------------------


def test_starting_records_a_live_pid_and_the_bound_port(tmp_path) -> None:
    """The one function that spawns a process, against a real process.

    The recorded port is what ``status`` reports, so a server started on a
    non-default port must not be reported on the default one.
    """
    feed = _feed(tmp_path)
    feed.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    pid = start_serving(feed, port=port)
    try:
        assert pid is not None
        assert is_serving(feed) is True
        assert recorded_port(feed) == port
        assert feed_status(feed, release="dev", channel="local")["url"].endswith(f":{port}")
    finally:
        stop_serving(feed)


def test_stopping_actually_signals_the_server(tmp_path) -> None:
    """Deleting the state file is not stopping.

    Without the signal, ``stop`` prints "stopped", drops the state file, and
    leaves a live server no bakar command can reach again.
    """
    feed = _feed(tmp_path)
    feed.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    pid = start_serving(feed, port=port)
    assert pid is not None

    assert stop_serving(feed) is True

    # Wait on the PORT, not on is_serving: stop clears the state file first, so
    # is_serving answers False immediately and would pass without any signal
    # having been delivered - which is exactly the bug this test exists for.
    deadline = time.monotonic() + 5.0
    closed = False
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                closed = True
                break
        time.sleep(0.05)

    assert closed, "the server is still accepting connections, so no signal reached it"


def test_a_start_that_cannot_bind_reports_failure(tmp_path) -> None:
    """Popen returning is not the server binding.

    With output on DEVNULL a port collision is invisible, so a start that was
    not confirmed would record a dead PID and print success.
    """
    feed = _feed(tmp_path)
    feed.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        assert start_serving(feed, port=port) is None
        # No state recorded, so the next serve is not blocked by this failure.
        assert is_serving(feed) is False
    finally:
        blocker.close()


def test_stopping_something_that_is_not_running_is_not_an_error(tmp_path) -> None:
    """Stop is idempotent, so it can be called without checking first."""
    assert stop_serving(tmp_path / "feed") is False


def test_stopping_clears_a_stale_statefile(tmp_path) -> None:
    """A stale state file is removed by stop, so serve can start again."""
    feed = _feed(tmp_path)
    state = feed / ".bakar"
    state.mkdir(parents=True)
    statefile = state / "serve.json"
    statefile.write_text(json.dumps({"pid": 4194304, "port": 8080}))

    stop_serving(feed)

    assert not statefile.exists()


# --- status ----------------------------------------------------------------


def test_status_reports_the_feed_shape(tmp_path) -> None:
    """Status names the root, targets, pool size and URL."""
    feed = _feed(tmp_path, machines=("qemux86-64", "imx93-frdm"), pool=3)

    status = feed_status(feed, release="dev", channel="local", port=8080)

    assert status["feed_root"] == feed
    assert status["targets"] == ["imx93-frdm", "qemux86-64"]
    assert status["pool_entries"] == 3
    assert status["url"].endswith(":8080")
    assert status["serving"] is False


def test_status_on_a_never_synced_feed_reports_empty_rather_than_failing(tmp_path) -> None:
    """An unsynced feed has a status, so a caller can say "nothing here yet".

    Raising would make the first thing a user runs after configuring a feed look
    like a broken tool.
    """
    status = feed_status(tmp_path / "feed", release="dev", channel="local", port=8080)

    assert status["targets"] == []
    assert status["pool_entries"] == 0
    assert status["snapshots"] == []


def test_status_reports_the_announced_snapshot(tmp_path) -> None:
    """The snapshot a client would pin is the one the pointer names."""
    feed = _feed(tmp_path, machines=("qemux86-64",))
    pointer = feed / "dev" / "local" / "target" / "qemux86-64" / "snapshots-latest.json"
    pointer.write_text(json.dumps({"id": "20260822T210520Z", "created": "2026-08-22T21:05:20Z"}))

    status = feed_status(feed, release="dev", channel="local", port=8080)

    assert status["snapshots"] == ["20260822T210520Z"]


def test_status_tolerates_an_unreadable_pointer(tmp_path) -> None:
    """A corrupt pointer reports no snapshot rather than raising.

    Status is what a user runs to find out what is wrong, so it has to survive
    the feed being wrong.
    """
    feed = _feed(tmp_path, machines=("qemux86-64",))
    (feed / "dev" / "local" / "target" / "qemux86-64" / "snapshots-latest.json").write_text("{not json")

    assert feed_status(feed, release="dev", channel="local", port=8080)["snapshots"] == []
