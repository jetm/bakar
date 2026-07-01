"""Unit tests for the persistent sccache server lifecycle.

The fix that matters: pre-start one detached server so it survives bitbake's
per-task process-group teardown. These tests pin the two load-bearing spawn
properties (``start_new_session`` and ``SCCACHE_IDLE_TIMEOUT=0``) and the
idempotent "already running" short-circuit, without touching a real sccache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import sccache_server

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.unit
def test_ensure_running_returns_true_without_spawn_when_already_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A responding server short-circuits: no second server is ever spawned.

    Spawning ``sccache --start-server`` against an already-bound port is exactly
    the ``server.rs:135`` "Failed to bind socket" panic, so the idempotent probe
    must come first.
    """
    monkeypatch.setattr(sccache_server, "_server_responding", lambda port: True)
    spawned: list[object] = []
    monkeypatch.setattr(sccache_server.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    assert sccache_server.ensure_running(binary="/usr/bin/sccache") is True
    assert spawned == []


@pytest.mark.unit
def test_ensure_running_returns_false_when_binary_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sccache on PATH -> False, and never attempts a spawn."""
    monkeypatch.setattr(sccache_server.shutil, "which", lambda name: None)
    spawned: list[object] = []
    monkeypatch.setattr(sccache_server.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    assert sccache_server.ensure_running() is False
    assert spawned == []


@pytest.mark.unit
def test_ensure_running_spawns_detached_persistent_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no server answers, spawn one detached, persistent, with the scheduler.

    The two spawn properties are the whole point: ``start_new_session=True``
    detaches the server from bitbake's task process group, and
    ``SCCACHE_IDLE_TIMEOUT=0`` keeps it from idling out mid-build.
    """
    responses = iter([False, True])  # pre-spawn probe, then post-spawn probe
    monkeypatch.setattr(sccache_server, "_server_responding", lambda port: next(responses))
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(sccache_server.subprocess, "Popen", fake_popen)

    result = sccache_server.ensure_running("http://localhost:10600", binary="/usr/bin/sccache")

    assert result is True
    assert captured["cmd"] == ["/usr/bin/sccache", "--start-server"]
    assert captured["kwargs"]["start_new_session"] is True
    env = captured["kwargs"]["env"]
    assert env["SCCACHE_IDLE_TIMEOUT"] == "0"
    assert env["SCCACHE_DIST_SCHEDULER_URL"] == "http://localhost:10600"


@pytest.mark.unit
def test_ensure_running_returns_false_when_server_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn that never starts answering returns False before the wall-deadline.

    monotonic is advanced past the deadline so the probe loop exits at once; the
    caller treats False as "sccache unavailable" and the build proceeds.
    """
    monkeypatch.setattr(sccache_server, "_server_responding", lambda port: False)
    monkeypatch.setattr(sccache_server.subprocess, "Popen", lambda *a, **k: object())
    monkeypatch.setattr(sccache_server.time, "sleep", lambda s: None)
    times = iter([0.0, 100.0])
    monkeypatch.setattr(sccache_server.time, "monotonic", lambda: next(times))

    assert sccache_server.ensure_running(binary="/usr/bin/sccache") is False


@pytest.mark.unit
def test_server_port_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCCACHE_SERVER_PORT overrides the default; a bad value falls back."""
    monkeypatch.setenv("SCCACHE_SERVER_PORT", "5005")
    assert sccache_server._server_port() == 5005

    monkeypatch.setenv("SCCACHE_SERVER_PORT", "not-a-number")
    assert sccache_server._server_port() == 4226

    monkeypatch.delenv("SCCACHE_SERVER_PORT", raising=False)
    assert sccache_server._server_port() == 4226


@pytest.mark.unit
def test_default_uds_path_is_absolute_and_short() -> None:
    """The host-mode server socket is an absolute ``.sock`` path.

    It must be absolute (do_compile's HOME is a kas throwaway temp dir, so a
    ``~``-relative path would resolve wrong inside the task) and stay well under
    the 108-byte AF_UNIX path limit.
    """
    p = sccache_server.default_uds_path()
    assert p.is_absolute()
    assert p.name.endswith(".sock")
    assert len(str(p)) < 100


@pytest.mark.unit
def test_ensure_running_uds_spawns_on_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``uds_path`` set, spawn the server on that unix socket.

    do_compile runs in a private network namespace, so a TCP 127.0.0.1:4226
    daemon is unreachable and each task auto-starts its own config-less local
    server - the cluster sits idle. A unix-socket path is filesystem-based, so it
    crosses the namespace boundary; the daemon (host netns, real config) does the
    dist dispatch.
    """
    uds = str(tmp_path / "sccache-server.sock")
    responses = iter([False, True])  # pre-spawn UDS probe, then post-spawn probe
    monkeypatch.setattr(sccache_server, "_uds_responding", lambda p: next(responses))
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(sccache_server.subprocess, "Popen", fake_popen)

    result = sccache_server.ensure_running("http://localhost:10600", binary="/usr/bin/sccache", uds_path=uds)

    assert result is True
    assert captured["cmd"] == ["/usr/bin/sccache", "--start-server"]
    assert captured["kwargs"]["start_new_session"] is True
    env = captured["kwargs"]["env"]
    assert env["SCCACHE_SERVER_UDS"] == uds
    assert env["SCCACHE_IDLE_TIMEOUT"] == "0"
    assert env["SCCACHE_DIST_SCHEDULER_URL"] == "http://localhost:10600"


@pytest.mark.unit
def test_ensure_running_uds_short_circuits_when_socket_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A responding unix socket short-circuits: no second server is spawned."""
    uds = str(tmp_path / "sccache-server.sock")
    monkeypatch.setattr(sccache_server, "_uds_responding", lambda p: True)
    spawned: list[object] = []
    monkeypatch.setattr(sccache_server.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    assert sccache_server.ensure_running(binary="/usr/bin/sccache", uds_path=uds) is True
    assert spawned == []
