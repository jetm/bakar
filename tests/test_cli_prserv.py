"""Tests for the ``bakar prserv stop`` and ``bakar prserv status`` verbs.

The ``bakar.prserv`` helpers are monkeypatched, so no daemon is started,
signaled or probed on the network. The workspace is a tmp dir with a
``.bakar.toml`` marker and an ``nxp/`` subdirectory, and the user config is a
clean default so the real ``~/.config/bakar/config.toml`` is never read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.prserv as prserv_cmd
from bakar.cli import app
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace whose BSP root is ``<workspace>/nxp``; chdir into it."""
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_stop_when_running_prints_stopped(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` prints ``stopped`` when the helper reports a daemon was stopped."""
    calls: list[dict] = []

    def fake_stop(state_key: Path, *, binary_root: Path, bind_host: str = "localhost") -> bool:
        calls.append({"state_key": state_key, "binary_root": binary_root, "bind_host": bind_host})
        return True

    monkeypatch.setattr(prserv_cmd.prserv, "stop", fake_stop)

    result = runner.invoke(app, ["prserv", "stop"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "stopped"
    assert len(calls) == 1
    assert calls[0]["binary_root"] == workspace / "nxp"
    assert calls[0]["bind_host"] == "localhost"


def test_stop_when_not_running_prints_not_running(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop`` prints ``not running`` (exit 0) when there was nothing to stop."""
    monkeypatch.setattr(prserv_cmd.prserv, "stop", lambda *_a, **_k: False)

    result = runner.invoke(app, ["prserv", "stop"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "not running"


def test_status_when_running_prints_host_and_port(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status`` prints ``running, PRSERV_HOST=<host>:<port>`` for a live daemon."""
    probes: list[str] = []

    def fake_is_running(_state_key: Path, *, bind_host: str = "localhost") -> bool:
        probes.append(bind_host)
        return True

    monkeypatch.setattr(prserv_cmd.prserv, "is_running", fake_is_running)
    monkeypatch.setattr(prserv_cmd.prserv, "_workspace_port", lambda _state_key: 4242)

    result = runner.invoke(app, ["prserv", "status"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "running, PRSERV_HOST=localhost:4242"
    assert probes == ["localhost"]


def test_status_when_not_running_prints_not_running(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status`` prints ``not running`` when the daemon is down."""
    monkeypatch.setattr(prserv_cmd.prserv, "is_running", lambda *_a, **_k: False)

    result = runner.invoke(app, ["prserv", "status"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "not running"
