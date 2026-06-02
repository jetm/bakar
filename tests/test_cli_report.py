"""Tests for the ``bakar report`` command.

Drives the command through the Typer ``CliRunner`` (pattern from
``tests/test_cli_layers.py``), monkeypatching ``_find_run`` and
``assemble_report`` on ``bakar.commands.report`` - where the ``report``
function looks them up - so no real run directory or git state is needed.

Importing ``bakar.commands.report`` registers the command on the shared
``app``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import bakar.commands.report as report_module
from bakar.cli import app
from bakar.report import ReportSummary

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


def _summary(build_revision: str | None = None) -> ReportSummary:
    return ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1845.0,
        deploy_dir="/work/build/tmp/deploy/images/imx8mp-var-dart",
        image_size=123456,
        peak_tmp_bytes=5000,
        layers=[],
        build_revision=build_revision,
    )


@pytest.mark.unit
def test_no_matching_run_exits_nonzero(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``_find_run`` returns None the command exits non-zero."""
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: None)
    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code != 0, result.output


@pytest.mark.unit
def test_json_output_is_parseable(runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` prints a single JSON object containing run_id, status, and build_revision."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary(build_revision="abc123def456"))

    result = runner.invoke(app, ["report", "--json", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["run_id"] == "20260527-100000"
    assert payload["status"] == "success"
    assert "build_revision" in payload
    assert payload["build_revision"] == "abc123def456"


@pytest.mark.unit
def test_json_output_build_revision_null_when_none(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` includes ``build_revision: null`` when layers are empty."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--json", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert "build_revision" in payload
    assert payload["build_revision"] is None


@pytest.mark.unit
def test_default_prints_human_block(runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default (non-JSON) path prints the human-readable block."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "20260527-100000" in result.output
    assert "success" in result.output


@pytest.mark.unit
def test_default_shows_build_revision_when_non_none(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text output includes the ``build_revision`` line when it is non-None."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary(build_revision="abc123def456"))

    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "build_revision" in result.output
    assert "abc123def456" in result.output


@pytest.mark.unit
def test_default_omits_build_revision_when_none(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text output omits the ``build_revision`` line when it is None."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "build_revision" not in result.output
