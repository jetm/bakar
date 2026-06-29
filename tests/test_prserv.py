"""Unit tests for bakar.prserv daemon helpers.

Covers the salted port derivation (must not collide with the hashserv port for
the same shared state key), the workspace-pinned binary lookup, and the
probe-first ``ensure_running`` lifecycle: return the address when the daemon is
already listening, otherwise spawn ``bitbake-prserv --start`` bound to the given
host and return once the TCP probe succeeds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar import prserv
from bakar.prserv import _find_binary, _workspace_port

pytestmark = pytest.mark.unit


class _FakeSocket:
    def close(self) -> None:
        pass


def _create_binary(root: Path) -> Path:
    binary = root / "sources" / "poky" / "bitbake" / "bin" / "bitbake-prserv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_port_deterministic_and_in_range(tmp_path: Path) -> None:
    port = _workspace_port(tmp_path)
    assert port == _workspace_port(tmp_path)
    assert 49152 <= port < 65535


def test_port_differs_from_hashserv_for_same_state_key(tmp_path: Path) -> None:
    """prserv and hashserv keyed to the same SSTATE_DIR must not share a port."""
    from bakar.hashserv import _workspace_port as hashserv_port

    assert _workspace_port(tmp_path) != hashserv_port(tmp_path)


def test_find_binary_workspace_hit(tmp_path: Path) -> None:
    binary = _create_binary(tmp_path)
    assert _find_binary(tmp_path) == binary


def test_find_binary_returns_none_when_absent(tmp_path: Path) -> None:
    assert _find_binary(tmp_path) is None


def test_ensure_running_returns_addr_when_already_listening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable daemon short-circuits: return the address, never spawn."""
    monkeypatch.setattr(prserv.socket, "create_connection", lambda _addr, timeout: _FakeSocket())

    def _must_not_spawn(*_a: object, **_k: object) -> object:
        raise AssertionError("ensure_running must not spawn when already reachable")

    monkeypatch.setattr(prserv.subprocess, "Popen", _must_not_spawn)

    port = _workspace_port(tmp_path)
    assert prserv.ensure_running(tmp_path, binary_root=tmp_path, bind_host="10.42.0.1") == f"10.42.0.1:{port}"


def test_ensure_running_spawns_and_binds_cluster_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not yet up: spawn --start bound to the cluster host, return once probed."""
    _create_binary(tmp_path)
    captured: dict[str, object] = {}
    calls = {"n": 0}

    def _probe(addr: tuple[str, int], timeout: float) -> _FakeSocket:
        del timeout
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("not listening yet")
        captured["probe_addr"] = addr
        return _FakeSocket()

    monkeypatch.setattr(prserv.socket, "create_connection", _probe)

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

    def _always_fail(_addr: tuple[str, int], timeout: float) -> _FakeSocket:
        del timeout
        raise OSError("down")

    monkeypatch.setattr(prserv.socket, "create_connection", _always_fail)
    assert prserv.ensure_running(tmp_path, binary_root=tmp_path) is None
