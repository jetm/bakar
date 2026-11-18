"""Unit tests for bakar.prserv daemon helpers.

Covers the salted port derivation (must not collide with the hashserv port for
the same shared state key), the workspace-pinned binary lookup, and the
probe-first ``ensure_running`` lifecycle: return the address when the daemon is
already listening, otherwise spawn ``bitbake-prserv --start`` bound to the given
host and return once the TCP probe succeeds.
"""

from __future__ import annotations

import os
import socket
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import bakar.mounts as mounts_module
from bakar import central_service, prserv
from bakar.central_service import OwnedDaemon
from bakar.hashserv import NetworkStateDirError
from bakar.prserv import _find_binary, _workspace_port

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.unit


def _stamp(daemon: OwnedDaemon, database: Path, host: str = "localhost") -> OwnedDaemon:
    """Give ``daemon`` the ``--host``/``--port`` argv a real bitbake-prserv carries."""
    port = _workspace_port(database.parent.parent)
    return OwnedDaemon(pid=daemon.pid, argv=(*daemon.argv, "--host", host, "--port", str(port)))


def _own(monkeypatch: pytest.MonkeyPatch, daemons: list[OwnedDaemon], host: str = "localhost") -> None:
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _tool, db: [_stamp(d, db, host) for d in daemons])


def _listening(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(central_service, "is_listening", lambda _h, _p, **_k: value)


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def _must_not_spawn(*_a: object, **_k: object) -> object:
        raise AssertionError("must not spawn")

    monkeypatch.setattr(prserv.subprocess, "Popen", _must_not_spawn)


_DAEMON = OwnedDaemon(pid=4242, argv=("python3", "bitbake-prserv", "--start"))
_REAL_BIND_CONFLICT = central_service.bind_conflict


@pytest.fixture(autouse=True)
def _bindable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report every address bindable; tests of the refusal override it."""
    monkeypatch.setattr(central_service, "bind_conflict", lambda _h, _p: None)


class _FakeSocket:
    def close(self) -> None:
        pass


def _create_binary(root: Path) -> Path:
    binary = root / "sources" / "poky" / "bitbake" / "bin" / "bitbake-prserv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def _write_stub_realpath_wedged(tmp_path: Path) -> Path:
    """Executable ``realpath`` stub that never exits - mirrors
    ``tests/test_mounts_probe.py``'s helper of the same name."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "realpath"
    stub.write_text("#!/bin/sh\nsleep 60\n")
    stub.chmod(0o755)
    return bin_dir


def test_port_deterministic_and_in_range(tmp_path: Path) -> None:
    port = _workspace_port(tmp_path)
    assert port == _workspace_port(tmp_path)
    assert 49152 <= port < 65535


def test_port_differs_from_hashserv_for_same_state_key(tmp_path: Path) -> None:
    """prserv and hashserv keyed to the same SSTATE_DIR must not share a port."""
    from bakar.hashserv import _workspace_port as hashserv_port

    assert _workspace_port(tmp_path) != hashserv_port(tmp_path)


def test_workspace_port_does_not_hang_on_wedged_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A wedged ``realpath`` on PATH must not make ``_workspace_port`` hang.

    Regression test: this used to call ``state_key.resolve()``, which shells
    out to ``realpath`` and could hang on a wedged NFS automount. It now uses
    ``mounts._lexical_normalize``, which touches no filesystem.
    """
    bin_dir = _write_stub_realpath_wedged(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    state_key = tmp_path / "sstate"

    start = time.monotonic()
    port = _workspace_port(state_key)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    assert 49152 <= port < 65535


def test_find_binary_workspace_hit(tmp_path: Path) -> None:
    binary = _create_binary(tmp_path)
    assert _find_binary(tmp_path) == binary


def test_find_binary_returns_none_when_absent(tmp_path: Path) -> None:
    assert _find_binary(tmp_path) is None


def test_ensure_running_returns_addr_when_already_listening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable daemon short-circuits: return the address, never spawn."""
    monkeypatch.setattr(central_service.socket, "create_connection", lambda _addr, timeout: _FakeSocket())
    _own(monkeypatch, [_DAEMON], host="10.42.0.1")
    # network_state_reason's is_path_on_nfs classification now spawns a real
    # `realpath` child (bug 1's bounded, symlink-following resolution) -
    # stub it so the global subprocess.Popen patch below, meant only to
    # guard the prserv daemon spawn itself, is not also asked to stand in
    # for that unrelated subprocess call.
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)

    def _must_not_spawn(*_a: object, **_k: object) -> object:
        raise AssertionError("ensure_running must not spawn when already reachable")

    monkeypatch.setattr(prserv.subprocess, "Popen", _must_not_spawn)

    port = _workspace_port(tmp_path)
    assert prserv.ensure_running(tmp_path, binary_root=tmp_path, bind_host="10.42.0.1") == f"10.42.0.1:{port}"


def test_ensure_running_spawns_and_binds_cluster_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not yet up: spawn --start bound to the cluster host, return once probed."""
    _create_binary(tmp_path)
    _own(monkeypatch, [])
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    captured: dict[str, object] = {}
    calls = {"n": 0}

    def _probe(addr: tuple[str, int], timeout: float) -> _FakeSocket:
        del timeout
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("not listening yet")
        captured["probe_addr"] = addr
        return _FakeSocket()

    monkeypatch.setattr(central_service.socket, "create_connection", _probe)

    def _fake_popen(args: list[str], **_k: object) -> object:
        captured["args"] = args
        return object()

    monkeypatch.setattr(prserv.subprocess, "Popen", _fake_popen)

    port = _workspace_port(tmp_path)
    addr = prserv.ensure_running(tmp_path, binary_root=tmp_path, bind_host="10.42.0.1")

    assert addr == f"10.42.0.1:{port}"
    args = captured["args"]
    assert isinstance(args, list)
    assert "--start" in args
    assert "--host" in args
    assert "10.42.0.1" in args
    assert str(port) in args
    # a specific (non-0.0.0.0) bind host is also the probe target
    assert captured["probe_addr"] == ("10.42.0.1", port)


def test_ensure_running_returns_none_when_binary_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not reachable and no workspace binary: return None, do not spawn."""
    _own(monkeypatch, [])

    def _always_fail(_addr: tuple[str, int], timeout: float) -> _FakeSocket:
        del timeout
        raise OSError("down")

    monkeypatch.setattr(central_service.socket, "create_connection", _always_fail)
    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None


def test_is_running_probes_loopback_for_bind_only_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.0.0.0 is a bind-only address: probing it directly would report down."""
    probed: list[tuple[str, int]] = []

    def _probe(addr: tuple[str, int], timeout: float) -> _FakeSocket:
        del timeout
        probed.append(addr)
        return _FakeSocket()

    monkeypatch.setattr(central_service.socket, "create_connection", _probe)
    _own(monkeypatch, [_DAEMON], host="0.0.0.0")

    assert prserv.is_running(tmp_path, bind_host="0.0.0.0") is True
    assert probed == [("127.0.0.1", _workspace_port(tmp_path))]


def test_port_matches_pre_refactor_salted_derivation(tmp_path: Path) -> None:
    """The shared derivation with prserv's salt yields the historical port."""
    from hashlib import sha256

    digest = sha256(b"prserv:" + str(mounts_module._lexical_normalize(tmp_path)).encode()).hexdigest()
    assert _workspace_port(tmp_path) == 49152 + int(digest[:8], 16) % 16383


def test_ensure_running_closes_stderr_handle_when_popen_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed spawn must not leak the prserv.stderr handle."""
    _create_binary(tmp_path)
    _own(monkeypatch, [])
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)

    def _down(_addr: tuple[str, int], timeout: float) -> _FakeSocket:
        del timeout
        raise OSError("down")

    monkeypatch.setattr(central_service.socket, "create_connection", _down)
    seen: dict[str, object] = {}

    def _boom(_args: list[str], **kwargs: object) -> object:
        seen["fh"] = kwargs["stderr"]
        raise OSError("fork failed")

    monkeypatch.setattr(prserv.subprocess, "Popen", _boom)

    with pytest.raises(OSError, match="fork failed"):
        prserv.ensure_running(tmp_path, binary_root=tmp_path)

    fh = seen["fh"]
    assert getattr(fh, "closed", False) is True
    assert (tmp_path / ".bakar" / "prserv.stderr").exists()


def _recorded_pidfile_cleans(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(prserv, "_clean_stale_pidfiles", calls.append)
    return calls


def test_foreign_listener_refused_and_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Listening, no daemon of the state key: no address, no spawn, reason on disk."""
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _own(monkeypatch, [])
    _listening(monkeypatch, True)
    _no_spawn(monkeypatch)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path, bind_host="10.42.0.1") is None

    text = (tmp_path / ".bakar" / "prserv.stderr").read_text()
    port = _workspace_port(tmp_path)
    assert f"10.42.0.1:{port} is held by a process that is not this state key's bitbake-prserv" in text
    assert "bitbake-prserv was not started" in text
    assert cleans == []


def test_is_running_false_for_foreign_listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _listening(monkeypatch, True)
    _own(monkeypatch, [])
    assert prserv.is_running(tmp_path) is False


def test_is_running_false_when_not_listening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _listening(monkeypatch, False)
    _own(monkeypatch, [_DAEMON])
    assert prserv.is_running(tmp_path) is False


def test_owned_not_listening_adopts_when_it_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own daemon (bitbake pidfile gone) that answers within the wait is returned."""
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _own(monkeypatch, [_DAEMON])
    _listening(monkeypatch, False)
    waited: list[tuple[str, int, float]] = []

    def _wait(host: str, port: int, deadline: float) -> bool:
        waited.append((host, port, deadline))
        return True

    monkeypatch.setattr(central_service, "wait_listening", _wait)
    _no_spawn(monkeypatch)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    port = _workspace_port(tmp_path)
    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) == f"localhost:{port}"
    assert waited == [("localhost", port, prserv._STARTUP_PROBE_DEADLINE_SECONDS)]
    assert cleans == []


def test_owned_not_answering_refused_and_pidfile_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _own(monkeypatch, [_DAEMON, OwnedDaemon(pid=9, argv=())])
    _listening(monkeypatch, False)
    monkeypatch.setattr(central_service, "wait_listening", lambda _h, _p, _d: False)
    _no_spawn(monkeypatch)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None

    text = (tmp_path / ".bakar" / "prserv.stderr").read_text()
    db = tmp_path / ".bakar" / "prserv.sqlite3"
    port = _workspace_port(tmp_path)
    assert f"bitbake-prserv pid 4242 already serves {db} but does not answer on localhost:{port}" in text
    assert "run 'bakar prserv stop'" in text
    assert cleans == []


def test_neither_owned_nor_listening_spawns_and_cleans_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _own(monkeypatch, [])
    state = {"up": False}
    monkeypatch.setattr(central_service, "is_listening", lambda _h, _p, **_k: state["up"])
    cleans = _recorded_pidfile_cleans(monkeypatch)
    spawned: list[list[str]] = []

    def _popen(args: list[str], **_k: object) -> object:
        spawned.append(args)
        state["up"] = True
        return object()

    monkeypatch.setattr(prserv.subprocess, "Popen", _popen)

    port = _workspace_port(tmp_path)
    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) == f"localhost:{port}"
    assert len(spawned) == 1
    assert cleans == [port]


_CONFLICT = "10.42.0.1:1 cannot be bound: address already in use"


def _conflicting(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, int]] | None = None) -> None:
    def _conflict(host: str, port: int) -> str:
        if calls is not None:
            calls.append((host, port))
        return _CONFLICT

    monkeypatch.setattr(central_service, "bind_conflict", _conflict)


def test_bind_conflict_refuses_without_spawn_or_pidfile_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _own(monkeypatch, [])
    _listening(monkeypatch, False)
    _no_spawn(monkeypatch)
    cleans = _recorded_pidfile_cleans(monkeypatch)
    probed: list[tuple[str, int]] = []
    _conflicting(monkeypatch, probed)

    start = time.monotonic()
    result = prserv.ensure_running(tmp_path, binary_root=tmp_path, bind_host="10.42.0.1")
    elapsed = time.monotonic() - start

    port = _workspace_port(tmp_path)
    assert result is None
    assert elapsed < 1.0
    assert probed == [("10.42.0.1", port)]
    assert cleans == []
    text = (tmp_path / ".bakar" / "prserv.stderr").read_text()
    assert f"{_CONFLICT}; bitbake-prserv was not started" in text


def test_bind_conflict_log_names_the_derived_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real helper's reason carries the port, so the log names it."""
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _own(monkeypatch, [])
    _listening(monkeypatch, False)
    _no_spawn(monkeypatch)
    port = _workspace_port(tmp_path)
    monkeypatch.setattr(central_service, "bind_conflict", lambda _h, p: f"127.0.0.1:{p} cannot be bound: in use")

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path, bind_host="127.0.0.1") is None

    assert f":{port} " in (tmp_path / ".bakar" / "prserv.stderr").read_text()


def test_real_socket_holding_the_port_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real socket bound (without SO_REUSEADDR) to the derived port, through the real helper."""
    import socket

    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _own(monkeypatch, [])
    _no_spawn(monkeypatch)
    monkeypatch.setattr(central_service, "bind_conflict", _REAL_BIND_CONFLICT)
    port = _workspace_port(tmp_path)
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            holder.bind(("127.0.0.1", port))
        except OSError:
            pytest.skip("derived port is already in use on this host")
        assert prserv.ensure_running(tmp_path, binary_root=tmp_path, bind_host="127.0.0.1") is None
    finally:
        holder.close()

    text = (tmp_path / ".bakar" / "prserv.stderr").read_text()
    assert f"127.0.0.1:{port} cannot be bound" in text
    assert "bitbake-prserv was not started" in text


def test_network_state_refusal_precedes_bind_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: True)
    probed: list[tuple[str, int]] = []
    _conflicting(monkeypatch, probed)

    with pytest.raises(NetworkStateDirError):
        prserv.ensure_running(tmp_path, binary_root=tmp_path)

    assert probed == []
    assert not (tmp_path / ".bakar").exists()


def test_probe_timeout_keeps_pidfile_when_a_daemon_is_starting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _listening(monkeypatch, False)
    # Nothing owned at the gating, a daemon of the state key visible after the probe timed out.
    answers = iter([[], [], [_DAEMON]])
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, db: [_stamp(d, db) for d in next(answers)])
    monkeypatch.setattr(prserv, "_STARTUP_PROBE_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(prserv.subprocess, "Popen", lambda *_a, **_k: object())
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None
    # the only clean allowed is the one before the spawn
    assert cleans == [_workspace_port(tmp_path)]


def test_probe_timeout_cleans_pidfile_when_no_daemon_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _listening(monkeypatch, False)
    _own(monkeypatch, [])
    monkeypatch.setattr(prserv, "_STARTUP_PROBE_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(prserv.subprocess, "Popen", lambda *_a, **_k: object())
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None
    port = _workspace_port(tmp_path)
    assert cleans == [port, port]


def test_binary_available_removed() -> None:
    assert not hasattr(prserv, "binary_available")


# --- stop -------------------------------------------------------------------


def _daemon_world(monkeypatch: pytest.MonkeyPatch, pids: list[int]) -> list[tuple[int, int]]:
    """Model live daemons: ``os.kill`` removes a pid, the finder and port reflect it."""
    alive = set(pids)
    kills: list[tuple[int, int]] = []

    def _kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(prserv.os, "kill", _kill)
    monkeypatch.setattr(
        central_service,
        "find_owned_daemons",
        lambda _t, db: [_stamp(OwnedDaemon(pid=p, argv=("bitbake-prserv",)), db) for p in sorted(alive)],
    )
    monkeypatch.setattr(central_service, "is_listening", lambda _h, _p, **_k: bool(alive))
    return kills


@pytest.fixture
def _pidfile(tmp_path: Path) -> Iterator[Path]:
    path = type(tmp_path)("/tmp") / f"PRServer_{socket.gethostbyname('localhost')}_{_workspace_port(tmp_path)}.pid"
    path.write_text("11\n")
    yield path
    path.unlink(missing_ok=True)


def test_stop_terminates_real_pidfileless_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import signal
    import subprocess
    import sys

    script = tmp_path / "bitbake-prserv"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    script.chmod(0o755)
    database = tmp_path / ".bakar" / "prserv.sqlite3"
    port = str(_workspace_port(tmp_path))
    child = subprocess.Popen([str(script), "--host", "localhost", "--port", port, "-f", str(database)])
    try:
        deadline = time.monotonic() + 5
        while not central_service.find_owned_daemons("bitbake-prserv", database):
            assert time.monotonic() < deadline, "child never visible in the process table"
            time.sleep(0.05)
        monkeypatch.setattr(central_service, "is_listening", lambda _h, _p, **_k: False)
        monkeypatch.setattr(prserv, "_STOP_DEADLINE_SECONDS", 0.2)
        # Reaping the child lets the finder see it gone (a zombie reads an empty cmdline).
        reaper_calls: list[int] = []
        real_find = central_service.find_owned_daemons

        def _find(tool: str, db: Path) -> list[OwnedDaemon]:
            if child.poll() is None and reaper_calls:
                child.wait(timeout=5)
            reaper_calls.append(1)
            return real_find(tool, db)

        monkeypatch.setattr(central_service, "find_owned_daemons", _find)

        assert prserv.stop(tmp_path, binary_root=tmp_path) is True
        assert child.wait(timeout=5) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_stop_signals_every_found_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kills = _daemon_world(monkeypatch, [11, 12])

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert sorted(kills) == [(11, 15), (12, 15)]


def test_stop_uses_bitbake_stop_without_signal_when_pidfile_and_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _pidfile: Path
) -> None:
    _create_binary(tmp_path)
    kills = _daemon_world(monkeypatch, [11])
    runs: list[list[str]] = []

    def _run(argv: list[str], **_k: object) -> None:
        runs.append(argv)
        kills_before_stop.append(len(kills))
        kills.clear()
        # bitbake's --stop ends the daemon: drop it from the world.
        monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: [])
        monkeypatch.setattr(central_service, "is_listening", lambda _h, _p, **_k: False)

    kills_before_stop: list[int] = []
    monkeypatch.setattr(prserv.subprocess, "run", _run)

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert len(runs) == 1
    assert "--stop" in runs[0]
    assert kills_before_stop == [0]
    assert kills == []


def test_stop_terminates_second_daemon_left_after_bitbake_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _pidfile: Path
) -> None:
    _create_binary(tmp_path)
    kills = _daemon_world(monkeypatch, [11, 12])
    monkeypatch.setattr(prserv, "_STOP_DEADLINE_SECONDS", 0.2)
    order: list[str] = []

    def _run(*_a: object, **_k: object) -> None:
        order.append("stop")

    monkeypatch.setattr(prserv.subprocess, "run", _run)
    real_kill = prserv.os.kill
    monkeypatch.setattr(prserv.os, "kill", lambda p, s: (order.append("kill"), real_kill(p, s))[1])

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert order[0] == "stop"
    assert sorted(kills) == [(11, 15), (12, 15)]


def test_stop_pidfile_but_no_binary_sends_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _pidfile: Path
) -> None:
    kills = _daemon_world(monkeypatch, [11])
    _no_run = pytest.fail
    monkeypatch.setattr(prserv.subprocess, "run", lambda *_a, **_k: _no_run("must not run --stop"))

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert kills == [(11, 15)]


def test_stop_foreign_listener_returns_false_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _own(monkeypatch, [])
    _listening(monkeypatch, True)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.stop(tmp_path, binary_root=tmp_path) is False
    assert cleans == []


def test_stop_nothing_found_and_port_closed_cleans_pidfiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _own(monkeypatch, [])
    _listening(monkeypatch, False)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.stop(tmp_path, binary_root=tmp_path) is False
    assert cleans == [_workspace_port(tmp_path)]


def test_stop_survivor_keeps_pidfile_returns_true_and_never_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(prserv.os, "kill", lambda p, s: kills.append((p, s)))
    _own(monkeypatch, [_DAEMON])
    _listening(monkeypatch, True)
    monkeypatch.setattr(prserv, "_STOP_DEADLINE_SECONDS", 0.2)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert cleans == []
    assert kills == [(4242, 15)]


def test_stop_ignores_vanished_and_foreign_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _kill(pid: int, _sig: int) -> None:
        raise ProcessLookupError if pid == 11 else PermissionError

    monkeypatch.setattr(prserv.os, "kill", _kill)
    _own(monkeypatch, [OwnedDaemon(pid=11, argv=()), OwnedDaemon(pid=12, argv=())])
    _listening(monkeypatch, False)
    monkeypatch.setattr(prserv, "_STOP_DEADLINE_SECONDS", 0.2)

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True


# --- ownership requires the derived port and host ----------------------------


def _other_port_daemon(monkeypatch: pytest.MonkeyPatch, state_key: Path, *, host: str = "localhost") -> None:
    """A daemon on this state key's database that serves a different port."""
    other = str(_workspace_port(state_key) + 1)
    daemon = OwnedDaemon(pid=4242, argv=("python3", "bitbake-prserv", "--start", "--host", host, "--port", other))
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: [daemon])


def test_daemon_on_other_port_refused_naming_pid_without_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _other_port_daemon(monkeypatch, tmp_path)
    _listening(monkeypatch, True)
    _no_spawn(monkeypatch)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None
    assert "bitbake-prserv pid 4242 already serves" in (tmp_path / ".bakar" / "prserv.stderr").read_text()
    assert cleans == []
    assert prserv.is_running(tmp_path) is False


def test_foreign_listener_without_state_key_daemon_gets_foreign_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _own(monkeypatch, [])
    _listening(monkeypatch, True)
    _no_spawn(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None
    assert "is held by a process that is not this state key's bitbake-prserv" in (
        (tmp_path / ".bakar" / "prserv.stderr").read_text()
    )


def test_daemon_on_other_host_refused_not_respawned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    daemon = OwnedDaemon(
        pid=4242, argv=("bitbake-prserv", "--host", "10.0.0.9", "--port", str(_workspace_port(tmp_path)))
    )
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: [daemon])
    _listening(monkeypatch, True)
    _no_spawn(monkeypatch)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None
    stderr = (tmp_path / ".bakar" / "prserv.stderr").read_text()
    assert "pid 4242" in stderr
    assert "on 10.0.0.9:" in stderr
    assert "run 'bakar prserv stop'" in stderr
    assert cleans == []
    assert prserv.is_running(tmp_path) is False


def test_stop_signals_daemon_bound_to_other_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = OwnedDaemon(
        pid=4242, argv=("bitbake-prserv", "--host", "10.0.0.9", "--port", str(_workspace_port(tmp_path)))
    )
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(prserv.os, "kill", lambda p, s: kills.append((p, s)))
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: [daemon])
    _listening(monkeypatch, False)
    monkeypatch.setattr(prserv, "_STOP_DEADLINE_SECONDS", 0.2)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert kills == [(4242, 15)]
    assert cleans == []


def test_wait_stopped_waits_while_other_host_daemon_lives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = OwnedDaemon(pid=4242, argv=("bitbake-prserv", "--host", "10.0.0.9", "--port", "1"))
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: [other])
    _listening(monkeypatch, False)
    monkeypatch.setattr(prserv, "_STOP_DEADLINE_SECONDS", 0.2)

    assert prserv._wait_stopped("localhost", 1, tmp_path / "db") is False
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: [])
    assert prserv._wait_stopped("localhost", 1, tmp_path / "db") is True


def test_probe_timeout_keeps_pidfile_with_live_other_host_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _listening(monkeypatch, False)
    other = OwnedDaemon(
        pid=4242, argv=("bitbake-prserv", "--host", "10.0.0.9", "--port", str(_workspace_port(tmp_path)))
    )
    answers = iter([[], [], [other]])
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: next(answers))
    monkeypatch.setattr(prserv, "_STARTUP_PROBE_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(prserv.subprocess, "Popen", lambda *_a, **_k: object())
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None
    assert cleans == [_workspace_port(tmp_path)]


def test_pidfile_check_requires_the_exact_bitbake_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _workspace_port(tmp_path)
    ip = socket.gethostbyname("localhost")
    exact = type(tmp_path)("/tmp") / f"PRServer_{ip}_{port}.pid"
    elsewhere = type(tmp_path)("/tmp") / f"PRServer_10.99.99.99_{port}.pid"
    daemons = [OwnedDaemon(pid=11, argv=())]
    elsewhere.write_text("11\n")
    try:
        assert prserv._own_pidfile_matches("localhost", port, daemons) is False
        exact.write_text("11\n")
        assert prserv._own_pidfile_matches("localhost", port, daemons) is True
        monkeypatch.setattr(prserv.socket, "gethostbyname", lambda _h: (_ for _ in ()).throw(OSError("no dns")))
        assert prserv._own_pidfile_matches("localhost", port, daemons) is False
    finally:
        exact.unlink(missing_ok=True)
        elsewhere.unlink(missing_ok=True)


def test_daemon_argv_without_port_is_not_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = OwnedDaemon(pid=4242, argv=("bitbake-prserv", "--start", "--host", "localhost", "--port"))
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: [daemon])
    _listening(monkeypatch, True)
    assert prserv.is_running(tmp_path) is False


def test_matching_port_and_host_is_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _own(monkeypatch, [_DAEMON])
    _listening(monkeypatch, True)
    assert prserv.is_running(tmp_path) is True


def test_stop_signals_daemon_on_other_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _other_port_daemon(monkeypatch, tmp_path)
    _listening(monkeypatch, False)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(prserv.os, "kill", lambda p, s: kills.append((p, s)))
    monkeypatch.setattr(prserv, "_STOP_DEADLINE_SECONDS", 0.2)
    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert kills == [(4242, 15)]


# --- stop: bitbake --stop only for a trusted pidfile --------------------------


def test_stop_ignores_pidfile_naming_other_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _pidfile: Path) -> None:
    _create_binary(tmp_path)
    _pidfile.write_text("999\n")
    kills = _daemon_world(monkeypatch, [11])
    monkeypatch.setattr(prserv.subprocess, "run", lambda *_a, **_k: pytest.fail("must not run --stop"))

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert kills == [(11, 15)]


def test_stop_ignores_pidfile_owned_by_other_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _pidfile: Path
) -> None:
    _create_binary(tmp_path)
    kills = _daemon_world(monkeypatch, [11])
    real_stat = type(_pidfile).stat

    def _stat(self: Path, **kw: object) -> object:
        result = real_stat(self, **kw)
        if self == _pidfile:
            return SimpleNamespace(st_uid=result.st_uid + 1)
        return result

    monkeypatch.setattr(type(_pidfile), "stat", _stat)
    monkeypatch.setattr(prserv.subprocess, "run", lambda *_a, **_k: pytest.fail("must not run --stop"))

    assert prserv.stop(tmp_path, binary_root=tmp_path) is True
    assert kills == [(11, 15)]


# --- spawn branch re-checks for a daemon that raced in ------------------------


def test_daemon_started_during_spawn_prep_keeps_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _listening(monkeypatch, False)
    answers = iter([[], [_DAEMON]])
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, db: [_stamp(d, db) for d in next(answers)])
    monkeypatch.setattr(central_service, "wait_listening", lambda _h, _p, _d: True)
    _no_spawn(monkeypatch)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    port = _workspace_port(tmp_path)
    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) == f"localhost:{port}"
    assert cleans == []


def test_daemon_on_other_host_started_during_spawn_prep_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_binary(tmp_path)
    monkeypatch.setattr(mounts_module, "is_path_on_nfs", lambda _p: False)
    _listening(monkeypatch, False)
    port = _workspace_port(tmp_path)
    raced = OwnedDaemon(pid=4242, argv=("bitbake-prserv", "--host", "127.0.0.2", "--port", str(port)))
    answers = iter([[], [raced]])
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _t, _d: next(answers))
    _no_spawn(monkeypatch)
    cleans = _recorded_pidfile_cleans(monkeypatch)

    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None
    stderr = (tmp_path / ".bakar" / "prserv.stderr").read_text()
    assert "pid 4242" in stderr
    assert "run 'bakar prserv stop'" in stderr
    assert cleans == []
