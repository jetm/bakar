"""hashserv doctor check and status probe the daemon on its bound address."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import bakar.commands.hashserv as hashserv_cmd
from bakar import hashserv
from bakar.cli import app
from bakar.diagnostics import Status, check_hashserv

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


def _cfg(state_key: Path, bind_host: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        use_hashequiv=True,
        hashserv_state_key=state_key,
        cluster_bind_host=bind_host,
        bsp_root=state_key,
    )


def _write_state(state_key: Path, port: int) -> None:
    state_dir = state_key / ".bakar"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "hashserv.pid").write_text("4242\n")
    (state_dir / "hashserv.port").write_text(f"{port}\n")


def _listener(host: str) -> socket.socket:
    sock = socket.socket()
    try:
        sock.bind((host, 0))
    except OSError:
        sock.close()
        pytest.skip(f"cannot bind {host}")
    sock.listen(1)
    return sock


def test_pass_for_listener_only_on_bind_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _listener("127.0.0.2")
    try:
        port = sock.getsockname()[1]
        _write_state(tmp_path, port)
        monkeypatch.setattr(hashserv, "is_running", lambda _key: True)

        result = check_hashserv(_cfg(tmp_path, "127.0.0.2"))
    finally:
        sock.close()

    assert result.status == Status.PASS
    assert f"ws://127.0.0.2:{port}" in result.message


def test_fail_when_daemon_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _listener("127.0.0.2")
    port = sock.getsockname()[1]
    sock.close()
    _write_state(tmp_path, port)
    monkeypatch.setattr(hashserv, "is_running", lambda _key: True)

    result = check_hashserv(_cfg(tmp_path, "127.0.0.2"))

    assert result.status == Status.FAIL
    assert f"ws://127.0.0.2:{port}" in result.message


def test_fail_when_state_vanishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashserv, "is_running", lambda _key: True)

    result = check_hashserv(_cfg(tmp_path, None))

    assert result.status == Status.FAIL
    assert "vanished" in result.message
    assert result.fix_hint == "bakar hashserv start"


def test_status_names_bound_host(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_state(tmp_path, 50000)
    monkeypatch.setattr(hashserv_cmd, "_resolve_daemon_cfg", lambda _ws, _yaml: _cfg(tmp_path, "10.1.2.3"))
    monkeypatch.setattr(hashserv, "is_running", lambda _key: True)

    result = runner.invoke(app, ["hashserv", "status"])

    assert result.exit_code == 0, result.output
    assert "running, pid=4242, url=ws://10.1.2.3:50000" in result.output
