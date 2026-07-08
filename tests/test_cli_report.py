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
from bakar.report import LangCacheStat, ReportSummary
from bakar.task_rollup import FamilyStat

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


def _summary(build_revision: str | None = None) -> ReportSummary:
    return ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1845.0,
        deploy_dir="/work/build/tmp/deploy/images/imx8mp-var-dart",
        image_size=123456,
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


def _summary_with_stats() -> ReportSummary:
    return ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1845.0,
        deploy_dir="/work/build/tmp/deploy/images/imx8mp-var-dart",
        image_size=123456,
        layers=[],
        build_revision="abc123def456",
        cache_by_language={
            "C/C++": LangCacheStat(hits=52186, misses=4263, hit_rate=92.4),
            "Rust": LangCacheStat(hits=511, misses=70, hit_rate=87.9),
        },
        dist_by_node={"192.168.8.172": 4649, "10.42.0.2": 5107},
        task_family_rollup={
            "do_compile": FamilyStat(seconds=40.0, count=2),
            "do_configure": FamilyStat(seconds=10.0, count=1),
            "do_install": FamilyStat(seconds=0.0, count=0),
            "do_fetch": FamilyStat(seconds=0.0, count=0),
            "other": FamilyStat(seconds=0.0, count=0),
        },
        go_compile_seconds=20.0,
    )


@pytest.mark.unit
def test_json_output_carries_new_measurement_keys(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` adds the new measurement keys while retaining the pre-existing ones."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary_with_stats())

    result = runner.invoke(app, ["report", "--json", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    # Pre-existing keys are retained.
    for key in ("run_id", "status", "duration_s", "layers", "build_revision"):
        assert key in payload, key
    # New measurement keys are present.
    for key in ("cache_by_language", "dist_by_node", "task_family_rollup", "go_compile_seconds"):
        assert key in payload, key

    # Nested dataclass values serialize to plain dicts.
    assert payload["cache_by_language"]["C/C++"]["hits"] == 52186
    assert payload["cache_by_language"]["C/C++"]["misses"] == 4263
    assert payload["cache_by_language"]["C/C++"]["hit_rate"] == 92.4
    assert payload["dist_by_node"]["10.42.0.2"] == 5107
    assert payload["task_family_rollup"]["do_compile"]["seconds"] == 40.0
    assert payload["task_family_rollup"]["do_compile"]["count"] == 2
    assert payload["go_compile_seconds"] == 20.0


@pytest.mark.unit
def test_human_output_shows_per_language_and_family_share(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human block shows a per-language row and a task-family share."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary_with_stats())

    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    # Per-language row: language name and its hit-rate percentage.
    assert "C/C++" in result.output
    assert "92.4" in result.output
    # Task-family share: family name and a percentage of summed family wall-time.
    assert "do_compile" in result.output
    assert "80.0" in result.output  # 40s of 50s summed family wall-time
    assert "%" in result.output


@pytest.mark.unit
def test_human_output_omits_new_sections_when_empty(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run without per-language/rollup data is unchanged - no new sections."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "cache by language" not in result.output
    assert "task families" not in result.output
