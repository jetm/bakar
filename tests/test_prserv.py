"""Unit tests for bakar.prserv daemon helpers.

Covers the salted port derivation (must not collide with the hashserv port for
the same shared state key), the workspace-pinned binary lookup, and the
probe-first ``ensure_running`` lifecycle: return the address when the daemon is
already listening, otherwise spawn ``bitbake-prserv --start`` bound to the given
host and return once the TCP probe succeeds.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

import bakar.mounts as mounts_module
from bakar import central_service, prserv
from bakar.central_service import OwnedDaemon
from bakar.prserv import _find_binary, _workspace_port

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _own(monkeypatch: pytest.MonkeyPatch, daemons: list[OwnedDaemon]) -> None:
    monkeypatch.setattr(central_service, "find_owned_daemons", lambda _tool, _db: daemons)


def _listening(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(central_service, "is_listening", lambda _h, _p, **_k: value)


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def _must_not_spawn(*_a: object, **_k: object) -> object:
        raise AssertionError("must not spawn")

    monkeypatch.setattr(prserv.subprocess, "Popen", _must_not_spawn)


_DAEMON = OwnedDaemon(pid=4242, argv=("python3", "bitbake-prserv", "--start"))


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
    _own(monkeypatch, [_DAEMON])
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
    _own(monkeypatch, [_DAEMON])

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


def test_binary_available_removed() -> None:
    assert not hasattr(prserv, "binary_available")
