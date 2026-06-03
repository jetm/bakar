"""Tests for the sstate summary parser and ``bakar report --show-sstate``.

The parser tests build a synthetic ``kas.log`` under ``tmp_path`` and assert
``_parse_sstate_summary`` resolves every field by name (present line), leaves
fields ``None`` on an absent line, and skips an unparseable line without
raising. The command tests drive ``bakar report`` through the Typer
``CliRunner`` with module-qualified patches on ``bakar.commands.report`` so no
real run directory or git state is needed (the recap-archived testing split).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import bakar.commands.report as report_module
from bakar.cli import app
from bakar.report import ReportSummary, _parse_sstate_summary

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_PRESENT_LINE = "Sstate summary: Wanted 100 Local 40 Mirrors 30 Missed 30 Current 0 (70% match, 100% complete)"


def test_parse_present_line_resolves_all_fields(tmp_path: Path) -> None:
    """A well-formed summary line yields all six counts and both percentages."""
    kas_log = tmp_path / "kas.log"
    kas_log.write_text("some noise\n" + _PRESENT_LINE + "\nmore noise\n")

    result = _parse_sstate_summary(kas_log)

    assert result["sstate_wanted"] == 100
    assert result["sstate_local"] == 40
    assert result["sstate_mirrors"] == 30
    assert result["sstate_missed"] == 30
    assert result["sstate_current"] == 0
    assert result["sstate_match_pct"] == 70
    assert result["sstate_complete_pct"] == 100


def test_parse_missing_file_yields_all_none(tmp_path: Path) -> None:
    """An absent kas.log leaves every field None without raising."""
    result = _parse_sstate_summary(tmp_path / "kas.log")

    assert set(result.values()) == {None}
    assert "sstate_wanted" in result


def test_parse_absent_line_yields_all_none(tmp_path: Path) -> None:
    """A kas.log with no Sstate summary line leaves every field None."""
    kas_log = tmp_path / "kas.log"
    kas_log.write_text("NOTE: Executing Tasks\nWARNING: nothing of interest\n")

    result = _parse_sstate_summary(kas_log)

    assert set(result.values()) == {None}


def test_parse_malformed_line_does_not_raise(tmp_path: Path) -> None:
    """A summary line missing fields leaves those fields None, parses the rest."""
    kas_log = tmp_path / "kas.log"
    kas_log.write_text("Sstate summary: Wanted 12 garbage Current 5\n")

    result = _parse_sstate_summary(kas_log)

    assert result["sstate_wanted"] == 12
    assert result["sstate_current"] == 5
    assert result["sstate_local"] is None
    assert result["sstate_mirrors"] is None
    assert result["sstate_missed"] is None
    assert result["sstate_match_pct"] is None
    assert result["sstate_complete_pct"] is None


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


def _summary() -> ReportSummary:
    return ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1845.0,
        deploy_dir="/work/build/tmp/deploy/images/imx8mp-var-dart",
        image_size=123456,
        peak_tmp_bytes=5000,
        layers=[],
        build_revision=None,
        sstate_wanted=100,
        sstate_local=40,
        sstate_mirrors=30,
        sstate_missed=30,
        sstate_current=0,
        sstate_match_pct=70,
        sstate_complete_pct=100,
    )


def test_show_sstate_renders_section(runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--show-sstate`` renders the sstate section with all counts and percentages."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--show-sstate", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "sstate summary" in result.output
    assert "wanted: 100" in result.output
    assert "match: 70%" in result.output
    assert "complete: 100%" in result.output


def test_without_toggle_no_sstate_section(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``--show-sstate`` and toggle false, no sstate section appears."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "sstate summary" not in result.output


def test_json_includes_sstate_fields_when_toggled(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json --show-sstate`` includes the sstate fields in the payload."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--json", "--show-sstate", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["sstate_wanted"] == 100
    assert payload["sstate_match_pct"] == 70
    assert payload["sstate_complete_pct"] == 100


def test_json_omits_sstate_fields_without_toggle(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` without the toggle omits the sstate fields."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--json", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert "sstate_wanted" not in payload
