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

    def _rec(bsp_root: Path, force: bool) -> bool:
        calls.append((bsp_root, force))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (workspace / "nxp", False)


def test_stop_force(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop --force`` calls stop_build with force=True."""
    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, force: bool) -> bool:
        calls.append((bsp_root, force))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

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

    def _rec(bsp_root: Path, force: bool) -> bool:
        calls.append((bsp_root, force))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(
        app,
        ["stop", "--workspace", str(workspace), "--manifest", "imx-6.6.52-2.2.2.xml"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (workspace / "nxp", False)


def test_stop_byo_positional_yaml(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bakar stop my.yml`` resolves bsp_root from the YAML's dir (BYO/generic).

    A generic kas YAML (no NXP/TI markers) is dispatched via
    ``_dispatch_from_yaml`` -> ``generic``, and the workspace is the YAML's
    parent dir - cwd is irrelevant, so this runs from outside any workspace.
    """
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n")
    (tmp_path / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, force: bool) -> bool:
        calls.append((bsp_root, force))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop", str(yaml)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (tmp_path, False)


def test_stop_yaml_and_manifest_conflict(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing both a positional YAML and ``--manifest`` is rejected with exit 2."""
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\n")

    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", lambda bsp_root, force: calls.append((bsp_root, force)))

    result = runner.invoke(app, ["stop", str(yaml), "--manifest", "imx-6.6.52-2.2.2.xml"])

    assert result.exit_code == 2
    assert calls == []


def test_stop_returns_false_exits_nonzero(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``stop_build`` returns False (nothing to stop), the command exits 1."""
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n")
    (tmp_path / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", lambda bsp_root, force: False)

    result = runner.invoke(app, ["stop", str(yaml)])

    assert result.exit_code == 1, result.output


def test_stop_returns_true_exits_zero(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``stop_build`` returns True (a build was signaled), the command exits 0."""
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n")
    (tmp_path / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", lambda bsp_root, force: True)

    result = runner.invoke(app, ["stop", str(yaml)])

    assert result.exit_code == 0, result.output


def test_stop_force_returns_false_exits_nonzero(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop --force`` still exits 1 when ``stop_build`` finds nothing to stop.

    ``--force`` skips the grace wait but does not manufacture a target: when no
    live/targetable build exists ``stop_build`` returns False and the command
    must still exit 1. Also proves ``force=True`` is threaded through to
    ``stop_build``.
    """
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n")
    (tmp_path / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, force: bool) -> bool:
        calls.append((bsp_root, force))
        return False

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop", "--force", str(yaml)])

    assert result.exit_code == 1, result.output
    assert calls == [(tmp_path, True)]
