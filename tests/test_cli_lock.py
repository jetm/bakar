"""Tests for the ``bakar lock`` command.

Drives the command through the Typer ``CliRunner``. The NXP path wraps
``repo manifest -r`` via ``subprocess.run`` (referenced as a module attribute,
so it is patched on ``lock_module.subprocess.run``). The BYO path wraps
``kas lock`` via ``run_kas_subcommand`` (imported directly into the command
module, so it is patched on ``lock_module.run_kas_subcommand``).

The NXP path is reached with the default manifest (no positional YAML, no
``--manifest``); the BYO path with a positional ``.yml`` whose ``machine:``
classifies as generic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.lock as lock_module
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


class _Completed:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


def _byo_yaml(tmp_path: Path) -> Path:
    """A generic (non-NXP/TI) kas YAML so dispatch takes the BYO/kas-lock path."""
    p = tmp_path / "kas.yml"
    p.write_text("machine: qemuarm64\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# NXP path - repo manifest -r
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_nxp_path_invokes_repo_manifest_r(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NXP workspace path invokes ``repo manifest -r`` with cwd ending in nxp."""
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return _Completed(0)

    monkeypatch.setattr(lock_module.subprocess, "run", fake_run)
    result = runner.invoke(app, ["lock", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "repo"
    assert "manifest" in argv
    assert "-r" in argv
    cwd = calls[0]["kwargs"]["cwd"]
    assert str(cwd).endswith("nxp"), cwd


@pytest.mark.unit
def test_nxp_default_output_path(runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--output`` the pinned manifest targets cfg.bsp_root / pinned-manifest.xml."""
    from bakar.config import resolve

    cfg = resolve(workspace=nxp_workspace, bsp_family="nxp")
    expected = str(cfg.bsp_root / "pinned-manifest.xml")

    captured: list[list[str]] = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _Completed(0)

    monkeypatch.setattr(lock_module.subprocess, "run", fake_run)
    result = runner.invoke(app, ["lock", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    argv = captured[0]
    assert "-o" in argv
    out_value = argv[argv.index("-o") + 1]
    assert out_value == expected


@pytest.mark.unit
def test_nxp_output_override(runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--output pinned.xml`` makes the NXP path target that path instead of the default."""
    from bakar.config import resolve

    cfg = resolve(workspace=nxp_workspace, bsp_family="nxp")
    default = str(cfg.bsp_root / "pinned-manifest.xml")
    custom = nxp_workspace / "pinned.xml"

    captured: list[list[str]] = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _Completed(0)

    monkeypatch.setattr(lock_module.subprocess, "run", fake_run)
    result = runner.invoke(app, ["lock", "--workspace", str(nxp_workspace), "--output", str(custom)])

    assert result.exit_code == 0, result.output
    argv = captured[0]
    out_value = argv[argv.index("-o") + 1]
    assert out_value == str(custom)
    assert out_value != default


@pytest.mark.unit
def test_nxp_nonzero_return_propagates(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero ``repo`` return propagates to a non-zero command exit."""

    def fake_run(argv, **kwargs):
        return _Completed(7)

    monkeypatch.setattr(lock_module.subprocess, "run", fake_run)
    result = runner.invoke(app, ["lock", "--workspace", str(nxp_workspace)])

    assert result.exit_code != 0, result.output


# ---------------------------------------------------------------------------
# BYO path - kas lock via run_kas_subcommand
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_byo_path_calls_run_kas_subcommand_lock(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A positional generic kas YAML calls run_kas_subcommand with subcommand 'lock'."""
    yaml_path = _byo_yaml(tmp_path)
    calls: list[dict] = []

    def fake_kas(ctx, subcommand, extra_args, **kwargs):
        calls.append({"subcommand": subcommand, "extra_args": list(extra_args), "kwargs": kwargs})
        return 0

    monkeypatch.setattr(lock_module.step_kas, "run_kas_subcommand", fake_kas)
    result = runner.invoke(app, ["lock", str(yaml_path), "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["subcommand"] == "lock"


@pytest.mark.unit
def test_byo_nonzero_return_propagates(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero run_kas_subcommand return propagates to a non-zero command exit."""
    yaml_path = _byo_yaml(tmp_path)

    monkeypatch.setattr(lock_module.step_kas, "run_kas_subcommand", lambda *a, **k: 3)
    result = runner.invoke(app, ["lock", str(yaml_path), "--workspace", str(tmp_path)])

    assert result.exit_code != 0, result.output
