"""Tests for the ``bakar hashserv`` sub-app.

Each test sets up a tmp workspace with a ``.bakar.toml`` marker so
``_workspace_from_cwd`` finds the workspace, then monkeypatches the
``bakar.hashserv`` helpers on the command module so no real daemon is
started or signaled. The command resolves the BSP root via
``_dispatch_bsp(None)`` which falls back to the NXP default - so
``cfg.bsp_root`` is ``<workspace>/nxp`` in these tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.hashserv as hashserv_cmd
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into it.

    The marker file is what ``_workspace_from_cwd`` keys off first; an
    ``nxp/`` subdirectory exists so the resolved ``cfg.bsp_root`` points at
    ``<workspace>/nxp/`` (the NXP-default dispatch path).
    """
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_status_when_not_running(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``status`` exits 0 with ``not running`` when the daemon is down."""
    monkeypatch.setattr(hashserv_cmd.hashserv, "is_running", lambda _root: False)

    result = runner.invoke(app, ["hashserv", "status"])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output


def test_status_when_running(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``status`` reads PID/port files and reports the URL when running."""
    bsp_root = workspace / "nxp"
    state_dir = bsp_root / ".bakar"
    state_dir.mkdir(parents=True)
    (state_dir / "hashserv.pid").write_text("12345\n")
    (state_dir / "hashserv.port").write_text("50000\n")
    monkeypatch.setattr(hashserv_cmd.hashserv, "is_running", lambda _root: True)

    result = runner.invoke(app, ["hashserv", "status"])

    assert result.exit_code == 0, result.output
    assert "running, pid=12345" in result.output
    assert "ws://localhost:50000" in result.output


def test_start_success(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``start`` exits 0 and prints ``started: <url>`` when ensure_running returns a URL."""
    monkeypatch.setattr(
        hashserv_cmd.hashserv,
        "ensure_running",
        lambda _root: "ws://localhost:50000",
    )

    result = runner.invoke(app, ["hashserv", "start"])

    assert result.exit_code == 0, result.output
    assert "started: ws://localhost:50000" in result.output


def test_start_failure(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``start`` exits 1 and surfaces the spec-pinned error string on None."""
    monkeypatch.setattr(hashserv_cmd.hashserv, "ensure_running", lambda _root: None)

    result = runner.invoke(app, ["hashserv", "start"])

    assert result.exit_code == 1, result.output
    assert "bitbake-hashserv not found or startup probe failed" in result.output


def test_stop_when_running(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` exits 0 and prints ``stopped`` when the helper reports True."""
    monkeypatch.setattr(hashserv_cmd.hashserv, "stop", lambda _root: True)

    result = runner.invoke(app, ["hashserv", "stop"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


def test_stop_when_not_running(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` exits 0 and prints ``not running`` when the helper reports False."""
    monkeypatch.setattr(hashserv_cmd.hashserv, "stop", lambda _root: False)

    result = runner.invoke(app, ["hashserv", "stop"])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output


def test_status_accepts_explicit_workspace(
    runner: _CliRunner,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``status --workspace WS`` resolves the workspace from the flag, not cwd."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(hashserv_cmd.hashserv, "is_running", lambda _root: False)

    result = runner.invoke(app, ["hashserv", "status", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output


def test_start_accepts_explicit_workspace(
    runner: _CliRunner,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start --workspace WS`` resolves the workspace from the flag, not cwd."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(
        hashserv_cmd.hashserv,
        "ensure_running",
        lambda _root: "ws://localhost:51847",
    )

    result = runner.invoke(app, ["hashserv", "start", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "started:" in result.output


def test_stop_accepts_explicit_workspace(
    runner: _CliRunner,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop --workspace WS`` resolves the workspace from the flag, not cwd."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(hashserv_cmd.hashserv, "stop", lambda _root: True)

    result = runner.invoke(app, ["hashserv", "stop", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


def test_status_accepts_positional_kas_yaml(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``status`` accepts a positional generic kas YAML and resolves via the YAML's parent."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # Outside any workspace

    kas_yaml = tmp_path / "generic.yml"
    kas_yaml.write_text("header: {version: 21}\n")

    fake_workspace = tmp_path / "ws"
    fake_workspace.mkdir()

    # Bypass real YAML parsing / cwd walk: route through the stubbed family/workspace.
    monkeypatch.setattr(
        "bakar.commands.hashserv._dispatch_from_yaml",
        lambda _yaml: ("generic", None),
    )
    monkeypatch.setattr(
        "bakar.commands.hashserv._resolve_workspace",
        lambda workspace, kas_yaml=None, family=None: fake_workspace,
    )
    monkeypatch.setattr(hashserv_cmd.hashserv, "is_running", lambda _root: False)

    result = runner.invoke(app, ["hashserv", "status", str(kas_yaml)])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output


def test_start_accepts_positional_kas_yaml(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start`` accepts a positional generic kas YAML and resolves via the YAML's parent."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # Outside any workspace

    kas_yaml = tmp_path / "generic.yml"
    kas_yaml.write_text("header: {version: 21}\n")

    fake_workspace = tmp_path / "ws"
    fake_workspace.mkdir()

    monkeypatch.setattr(
        "bakar.commands.hashserv._dispatch_from_yaml",
        lambda _yaml: ("generic", None),
    )
    monkeypatch.setattr(
        "bakar.commands.hashserv._resolve_workspace",
        lambda workspace, kas_yaml=None, family=None: fake_workspace,
    )
    monkeypatch.setattr(
        hashserv_cmd.hashserv,
        "ensure_running",
        lambda _root: "ws://localhost:50000",
    )

    result = runner.invoke(app, ["hashserv", "start", str(kas_yaml)])

    assert result.exit_code == 0, result.output
    assert "started:" in result.output


def test_stop_accepts_positional_kas_yaml(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop`` accepts a positional generic kas YAML and resolves via the YAML's parent."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # Outside any workspace

    kas_yaml = tmp_path / "generic.yml"
    kas_yaml.write_text("header: {version: 21}\n")

    fake_workspace = tmp_path / "ws"
    fake_workspace.mkdir()

    monkeypatch.setattr(
        "bakar.commands.hashserv._dispatch_from_yaml",
        lambda _yaml: ("generic", None),
    )
    monkeypatch.setattr(
        "bakar.commands.hashserv._resolve_workspace",
        lambda workspace, kas_yaml=None, family=None: fake_workspace,
    )
    monkeypatch.setattr(hashserv_cmd.hashserv, "stop", lambda _root: True)

    result = runner.invoke(app, ["hashserv", "stop", str(kas_yaml)])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
