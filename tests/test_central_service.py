"""Unit tests for the shared central hashserv/prserv daemon spawn logic.

The fix that matters: ``ensure_running`` redirects a spawned daemon's stderr
to a state-dir log file instead of ``subprocess.DEVNULL``, so a daemon that
starts but crashes or never listens leaves its diagnostic output on disk.
These tests monkeypatch ``central_service._STATE_DIR`` to ``tmp_path`` so no
test run touches the real ``~/.local/state/bakar`` directory.
"""

from __future__ import annotations

import pathlib
import socket
from typing import TYPE_CHECKING

import pytest

from bakar import central_service

if TYPE_CHECKING:
    from pathlib import Path


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` result that never exits on its own."""

    def poll(self) -> int | None:
        return None


@pytest.mark.unit
def test_ensure_running_captures_daemon_stderr_to_state_dir_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawned daemon's stderr goes to a file under the state dir, not DEVNULL.

    Before this fix ``ensure_running`` passed ``stderr=DEVNULL``, discarding
    whatever diagnostic output would have explained why a daemon started but
    never began listening.
    """
    monkeypatch.setattr(central_service, "_STATE_DIR", tmp_path)
    responses = iter([False, True])  # pre-spawn probe, then post-spawn probe
    monkeypatch.setattr(central_service, "is_listening", lambda host, port, **_: next(responses))
    monkeypatch.setattr(central_service.shutil, "which", lambda name: name)

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        kwargs["stderr"].write(b"daemon startup error\n")
        return _FakeProc()

    monkeypatch.setattr(central_service.subprocess, "Popen", fake_popen)

    endpoint = central_service.ensure_running(
        binary="/usr/bin/bb-hashserv",
        bind_host="127.0.0.1",
        database=str(tmp_path / "hashserv.db"),
        port=9000,
    )

    assert endpoint == "127.0.0.1:9000"
    assert captured["kwargs"]["stdout"] == central_service.subprocess.DEVNULL
    assert captured["kwargs"]["start_new_session"] is True

    log_path = tmp_path / "bb-hashserv-central.stderr"
    assert log_path.exists()
    assert log_path.read_bytes() == b"daemon startup error\n"


@pytest.mark.unit
def test_ensure_running_short_circuits_without_writing_stderr_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-listening service never spawns, so no stderr log is created."""
    monkeypatch.setattr(central_service, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(central_service, "is_listening", lambda host, port, **_: True)
    spawned: list[object] = []
    monkeypatch.setattr(central_service.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    endpoint = central_service.ensure_running(
        binary="/usr/bin/bb-hashserv",
        bind_host="127.0.0.1",
        database=str(tmp_path / "hashserv.db"),
        port=9000,
    )

    assert endpoint == "127.0.0.1:9000"
    assert spawned == []
    assert not (tmp_path / "bb-hashserv-central.stderr").exists()


@pytest.mark.unit
def test_ensure_running_returns_none_when_binary_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No resolvable binary -> None, and never attempts a spawn or log write."""
    monkeypatch.setattr(central_service, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(central_service, "is_listening", lambda host, port, **_: False)
    monkeypatch.setattr(central_service.shutil, "which", lambda name: None)
    spawned: list[object] = []
    monkeypatch.setattr(central_service.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    endpoint = central_service.ensure_running(
        binary="nonexistent-hashserv-binary",
        bind_host="127.0.0.1",
        database=str(tmp_path / "hashserv.db"),
        port=9000,
    )

    assert endpoint is None
    assert spawned == []


@pytest.mark.unit
def test_ensure_running_closes_stderr_fh_when_popen_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``Popen`` failure (vanished binary, fork failure) still closes the fh.

    Before the ``daemon_log.stderr_target`` contextmanager, the ``close()``
    call sat on the line after ``Popen()`` returned - an exception raised by
    ``Popen`` itself skipped it and leaked the fd.
    """
    import pathlib

    monkeypatch.setattr(central_service, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(central_service, "is_listening", lambda host, port, **_: False)
    monkeypatch.setattr(central_service.shutil, "which", lambda name: name)

    opened_files: list = []
    original_open = pathlib.Path.open

    def tracking_open(self, mode="r", *args, **kwargs):
        fh = original_open(self, mode, *args, **kwargs)
        opened_files.append(fh)
        return fh

    monkeypatch.setattr(pathlib.Path, "open", tracking_open)

    def fake_popen(*_a, **_k):
        raise OSError("fork failed")

    monkeypatch.setattr(central_service.subprocess, "Popen", fake_popen)

    with pytest.raises(OSError, match="fork failed"):
        central_service.ensure_running(
            binary="/usr/bin/bb-hashserv",
            bind_host="127.0.0.1",
            database=str(tmp_path / "hashserv.db"),
            port=9000,
        )

    assert len(opened_files) == 1
    assert opened_files[0].closed is True


@pytest.mark.unit
def test_workspace_port_matches_hashserv_and_prserv(tmp_path: Path) -> None:
    from bakar import hashserv, prserv

    for key in (tmp_path / "a", tmp_path / "sstate" / "x", tmp_path / "a" / ".." / "b"):
        assert central_service.workspace_port(key) == hashserv._workspace_port(key)
        assert central_service.workspace_port(key, b"prserv:") == prserv._workspace_port(key)


@pytest.mark.unit
@pytest.mark.parametrize(
    "rel",
    [
        ("root", "sources", "poky", "bitbake", "bin"),
        ("root", "sources", "bitbake", "bin"),
        ("root", "bitbake", "bin"),
        ("bitbake", "bin"),
    ],
)
def test_find_workspace_tool_finds_each_candidate(tmp_path: Path, rel: tuple[str, ...]) -> None:
    from bakar import hashserv, prserv

    root = tmp_path / "root"
    d = tmp_path.joinpath(*rel)
    d.mkdir(parents=True)
    root.mkdir(exist_ok=True)
    for name in ("bitbake-hashserv", "bitbake-prserv"):
        (d / name).write_text("")
    assert central_service.find_workspace_tool(root, "bitbake-hashserv") == d / "bitbake-hashserv"
    assert central_service.find_workspace_tool(root, "bitbake-hashserv") == hashserv._find_binary(root)
    assert central_service.find_workspace_tool(root, "bitbake-prserv") == prserv._find_binary(root)


@pytest.mark.unit
def test_find_workspace_tool_absent(tmp_path: Path) -> None:
    assert central_service.find_workspace_tool(tmp_path, "bitbake-hashserv") is None


@pytest.mark.unit
def test_stderr_target_honours_explicit_path_and_closes_on_raise(tmp_path: Path) -> None:
    from bakar import daemon_log

    log = tmp_path / "state" / "hashserv.stderr"
    held = None
    with pytest.raises(RuntimeError), daemon_log.stderr_target("bitbake-hashserv", tmp_path / "state", log) as fh:
        held = fh
        assert fh is not None
        assert not fh.closed
        raise RuntimeError("spawn failed")
    assert held is not None
    assert held.closed
    assert log.exists()
    assert not (tmp_path / "state" / "bitbake-hashserv-central.stderr").exists()


# bind_conflict: real loopback sockets on kernel-allocated ports (bind(0)), so
# no test guesses a port number.


def _listener(host: str = "127.0.0.1", *, reuse: bool = False, family: int = socket.AF_INET) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    if reuse:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen()
    return sock


def _addrinfo(*addrs: tuple[int, tuple]) -> list[tuple]:
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sa) for family, sa in addrs]


@pytest.mark.unit
def test_bind_conflict_free_port_is_none() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert central_service.bind_conflict("127.0.0.1", port) is None


@pytest.mark.unit
def test_bind_conflict_established_client_connection() -> None:
    with _listener() as server, socket.create_connection(server.getsockname()) as client:
        accepted, _ = server.accept()
        with accepted:
            port = client.getsockname()[1]
            reason = central_service.bind_conflict("127.0.0.1", port)
    assert reason is not None
    assert str(port) in reason
    assert "cannot be bound" in reason


@pytest.mark.unit
def test_bind_conflict_client_closed_connection() -> None:
    with _listener() as server:
        client = socket.create_connection(server.getsockname())
        accepted, _ = server.accept()
        port = client.getsockname()[1]
        client.close()
        accepted.close()
        reason = central_service.bind_conflict("127.0.0.1", port)
    assert reason is not None
    assert str(port) in reason


@pytest.mark.unit
def test_bind_conflict_foreign_listener() -> None:
    with _listener() as server:
        port = server.getsockname()[1]
        reason = central_service.bind_conflict("127.0.0.1", port)
    assert reason == f"127.0.0.1:{port} cannot be bound: address already in use"


@pytest.mark.unit
def test_bind_conflict_daemon_restart_state_is_none() -> None:
    """A stopped daemon's own closed connections do not refuse its restart."""
    server = _listener(reuse=True)
    port = server.getsockname()[1]
    client = socket.create_connection(server.getsockname())
    accepted, _ = server.accept()
    accepted.close()
    client.close()
    server.close()
    assert central_service.bind_conflict("127.0.0.1", port) is None


@pytest.mark.unit
def test_bind_conflict_checks_every_resolved_address(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        held = _listener("::1", family=socket.AF_INET6)
    except OSError:
        pytest.skip("::1 cannot be bound on this host")
    with held:
        port = held.getsockname()[1]
        infos = _addrinfo((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port, 0, 0)))
        monkeypatch.setattr(central_service.socket, "getaddrinfo", lambda *_a, **_k: infos)
        reason = central_service.bind_conflict("localhost", port)
    assert reason is not None
    assert reason.startswith(f"[::1]:{port} cannot be bound")


def _skip_when_nonlocal_bind() -> None:
    """192.0.2.1 only counts as an unavailable address while ip_nonlocal_bind is 0."""
    try:
        value = pathlib.Path("/proc/sys/net/ipv4/ip_nonlocal_bind").read_text().strip()
    except OSError:
        return
    if value == "1":
        pytest.skip("net.ipv4.ip_nonlocal_bind=1 lets bind(192.0.2.1) succeed")


@pytest.mark.unit
def test_bind_conflict_unavailable_address_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_when_nonlocal_bind()
    infos = _addrinfo((socket.AF_INET, ("192.0.2.1", 0)))
    monkeypatch.setattr(central_service.socket, "getaddrinfo", lambda *_a, **_k: infos)
    reason = central_service.bind_conflict("192.0.2.1", 50123)
    assert reason is not None
    assert reason.startswith("no address for 192.0.2.1 could be bound")


@pytest.mark.unit
def test_bind_conflict_skips_unavailable_beside_free_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_when_nonlocal_bind()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    infos = _addrinfo((socket.AF_INET, ("192.0.2.1", port)), (socket.AF_INET, ("127.0.0.1", port)))
    monkeypatch.setattr(central_service.socket, "getaddrinfo", lambda *_a, **_k: infos)
    assert central_service.bind_conflict("example", port) is None


@pytest.mark.unit
def test_bind_conflict_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> list[tuple]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(central_service.socket, "getaddrinfo", _raise)
    reason = central_service.bind_conflict("nowhere.invalid", 50123)
    assert reason is not None
    assert "nowhere.invalid" in reason
    assert "50123" in reason
