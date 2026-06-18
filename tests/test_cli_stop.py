"""Tests for the ``bakar stop`` command.

Each test sets up a tmp workspace with a ``.bakar.toml`` marker so
``_workspace_from_cwd`` finds the workspace, then monkeypatches
``build_stop.stop_build`` on the command module with a recording function
so no real build is signaled. The ``stop`` command resolves the BSP family
via ``_bsp_from_cwd``, which keys off cwd being inside ``workspace/nxp/`` -
so the fixture chdirs into ``<workspace>/nxp/`` and ``cfg.bsp_root`` is
``<workspace>/nxp``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.stop as stop_cmd
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
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into ``nxp/``.

    The marker file is what ``_workspace_from_cwd`` keys off first (walking up
    from cwd). Chdir-ing into the ``nxp/`` subdirectory makes ``_bsp_from_cwd``
    auto-detect the NXP family, so the resolved ``cfg.bsp_root`` points at
    ``<workspace>/nxp/``.
    """
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    (tmp_path / "nxp" / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "nxp")
    return tmp_path


def test_stop_no_args(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` with no args exits 0 and calls stop_build once with force=False."""
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", lambda bsp_root, force: calls.append((bsp_root, force)))

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (workspace / "nxp", False)


def test_stop_force(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop --force`` calls stop_build with force=True."""
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", lambda bsp_root, force: calls.append((bsp_root, force)))

    result = runner.invoke(app, ["stop", "--force"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (workspace / "nxp", True)


def test_stop_explicit_workspace(
    runner: _CliRunner,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop --workspace WS`` resolves the bsp_root from the flag, not cwd.

    Cwd sits outside any workspace; an NXP ``--manifest`` drives the family so
    ``_bsp_from_cwd`` is bypassed, and the resolved ``cfg.bsp_root`` must be
    derived from the explicit ``--workspace`` (``<workspace>/nxp``).
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", lambda bsp_root, force: calls.append((bsp_root, force)))

    result = runner.invoke(
        app,
        ["stop", "--workspace", str(workspace), "--manifest", "imx-6.6.52-2.2.2.xml"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (workspace / "nxp", False)
