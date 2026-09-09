"""Tests for the sstate summary parser and ``bakar report --show-sstate``.

The parser tests build a synthetic ``kas.log`` under ``tmp_path`` and assert
``_parse_sstate_summary`` resolves every field by name (present line), leaves
fields ``None`` on an absent line, and skips an unparseable line without
raising. The command tests drive ``bakar report`` through the Typer
``CliRunner`` with module-qualified patches on ``bakar.commands.report`` so no
real run directory or git state is needed (the recap-archived testing split).

The final block guards ``_render_sstate_lines``/``_SstateRender`` directly: it
renders to a captured console with a DISTINCT value per count field, so
transposing any two fields moves a number onto the wrong label and fails.
"""

from __future__ import annotations

import dataclasses
import io
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from rich.console import Console

import bakar.commands.report as report_module
from bakar.cli import app
from bakar.commands._helpers import _render_sstate_lines, _SstateRender
from bakar.report import ReportSummary, _parse_sstate_summary
from bakar.user_config import UserConfig
from tests.conftest import make_report_summary

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


def _summary() -> ReportSummary:
    return make_report_summary(
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

    with patch("bakar.commands._app._load_user_config_safe", return_value=UserConfig()):
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

    with patch("bakar.commands._app._load_user_config_safe", return_value=UserConfig()):
        result = runner.invoke(app, ["report", "--json", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert "sstate_wanted" not in payload


# ---------------------------------------------------------------------------
# _render_sstate_lines / _SstateRender value preservation
# ---------------------------------------------------------------------------

# One DISTINCT value per count field, so transposing any two of them changes
# the rendered text. Equal values would make a transposition invisible.
_COUNT_VALUES: dict[str, int] = {
    "wanted": 12,
    "local": 3,
    "mirrors": 45,
    "missed": 7,
    "current": 89,
    "match_pct": 61,
    "complete_pct": 24,
}

# The label each count field is rendered under, which is what pins a value to
# its position in the block.
_COUNT_LABELS: dict[str, str] = {
    "wanted": "wanted",
    "local": "local",
    "mirrors": "mirrors",
    "missed": "missed",
    "current": "current",
    "match_pct": "match",
    "complete_pct": "complete",
}


def _render(**overrides: object) -> str:
    """Render one ``_SstateRender`` to a captured console and return the text."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, no_color=True, highlight=False)
    fields = dict(_COUNT_VALUES)
    fields.update(overrides)
    _render_sstate_lines(console, render=_SstateRender(**fields))
    return buffer.getvalue()


def test_sstate_render_covers_every_dataclass_field() -> None:
    """The count table names every non-presentation field of ``_SstateRender``.

    Guards against a field being added or renamed without the value-preservation
    tests below growing to cover it.
    """
    declared = {f.name for f in dataclasses.fields(_SstateRender)}
    assert declared, "no fields derived from _SstateRender"
    assert declared == set(_COUNT_VALUES) | {"header_style", "highlight"}


def test_render_sstate_lines_carries_each_value_to_its_own_label() -> None:
    """Every count reaches the line bearing its own label, with all values distinct."""
    output = _render()

    for field, value in _COUNT_VALUES.items():
        label = _COUNT_LABELS[field]
        suffix = "%" if field.endswith("_pct") else ""
        assert f"{label}: {value}{suffix}" in output, f"{field} lost its position: {output!r}"


@pytest.mark.parametrize("field", list(_COUNT_VALUES))
def test_render_sstate_lines_one_field_at_a_time(field: str) -> None:
    """With every other count at 0, the one field under test still lands on its own label."""
    sentinel = 777
    baseline = dict.fromkeys(_COUNT_VALUES, 0)
    baseline[field] = sentinel

    output = _render(**baseline)

    label = _COUNT_LABELS[field]
    suffix = "%" if field.endswith("_pct") else ""
    assert f"{label}: {sentinel}{suffix}" in output, f"{field} did not reach its label: {output!r}"


def _render_with_ansi(header_style: str) -> str:
    """Render with a colour-capable console so ``header_style`` is observable."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, force_terminal=True, color_system="standard", highlight=False)
    _render_sstate_lines(console, render=_SstateRender(**_COUNT_VALUES, header_style=header_style))
    return buffer.getvalue()


def test_render_sstate_lines_header_style_styles_only_the_heading() -> None:
    """``header_style`` changes the heading line's escapes and leaves the counts alone."""
    styled = _render_with_ansi("bold").splitlines()
    plain = _render_with_ansi("").splitlines()

    assert plain[0] == "sstate summary:"
    assert styled[0] != plain[0]
    assert "sstate summary:" in styled[0]
    assert styled[1:] == plain[1:]
